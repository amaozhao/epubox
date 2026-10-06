from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import engine.agents.workflow as workflow_module
from engine.agents.runtime import ATOMIC_PROMPT_VERSION, ModelRuntime, ProviderError, request_messages
from engine.agents.workflow import run_workflow
from engine.epub.preparation import PreparationConfig
from engine.item.atoms import ADAPTER_VERSION, EXTRACTOR_VERSION
from engine.item.members import MemberIndex, pack_members
from engine.schemas.contracts import ItemRecord, ItemStatus, canonical_hash
from engine.schemas.members import MemberBatch
from engine.schemas.ready import AtomicPreparedInput
from engine.services.preparation import prepare_translation
from engine.services.ready import ReadySession, limits_for
from engine.services.store import RunStore
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker

MODEL = "gpt-3.5-turbo"


@dataclass(frozen=True)
class ReadyCase:
    prepared: AtomicPreparedInput
    batch: MemberBatch
    index: MemberIndex
    session: ReadySession


def prepare_case(root: Path, body: str, projections: tuple[str, ...]) -> ReadyCase:
    source = make_epub(root / "source.epub", {"chapter.xhtml": body})
    result = asyncio.run(
        prepare_translation(
            source,
            root / "work",
            PreparationConfig(
                run_id="workflow",
                auto_extract=False,
                adapter_version=ADAPTER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                translation_config={
                    "model": MODEL,
                    "max_source_tokens": 5000,
                    "max_input_tokens": 50_000,
                    "max_output_tokens": 10_000,
                    "context_tokens": 60_000,
                },
            ),
            StubChecker(),
        )
    )
    assert result.status == "ready" and result.prepared is not None
    session = ReadySession(RunStore(result.work_dir))
    batches = [
        MemberBatch.model_validate_json(path.read_bytes()) for path in (result.work_dir / "batches").glob("*.json")
    ]
    batch = next(value for value in batches if tuple(item.source_projection for item in value.items) == projections)
    return ReadyCase(session.prepared, batch, session.index, session)


@pytest.fixture(scope="module")
def case(tmp_path_factory: pytest.TempPathFactory) -> ReadyCase:
    return prepare_case(tmp_path_factory.mktemp("workflow"), "<p>Hello world.</p>", ("Hello world.",))


@pytest.fixture(scope="module")
def multi(tmp_path_factory: pytest.TempPathFactory) -> ReadyCase:
    return prepare_case(
        tmp_path_factory.mktemp("resume"),
        "<p>First sentence.</p><p>Second sentence.</p><p>Third sentence.</p>",
        ("First sentence.", "Second sentence.", "Third sentence."),
    )


def runtime(transport, *, model: str = MODEL) -> ModelRuntime:
    return ModelRuntime(
        model=SimpleNamespace(id=model),
        transport=transport,
        model_max_output_tokens=10_000,
        input_budget_version=2,
        max_transport_retries=0,
    )


def review_item(item: dict[str, Any], *, decision: str, target: str | None = None) -> dict[str, Any]:
    value = {
        "item_id": item["item_id"],
        "base_revision": item["base_revision"],
        "decision": decision,
        "checks": {
            "accuracy": "pass",
            "fluency": "pass",
            "terminology": "pass" if item["applicability"]["terminology"] else "not_applicable",
            "bindings": "pass" if item["applicability"]["bindings"] else "not_applicable",
            "script": "pass",
        },
        "issues": [],
    }
    if target is not None:
        value["target"] = target
    return value


def saved_draft(case: ReadyCase, target: str) -> ItemRecord:
    member = case.batch.items[0]
    wire_items = case.batch.payload["items"]
    assert isinstance(wire_items, list) and isinstance(wire_items[0], dict)
    assert wire_items[0].get("terms") == []
    return ItemRecord(
        item_id=member.item_id,
        segment_id=member.item_id,
        selected_term_ids=case.batch.manifest.term_ids_by_item[member.item_id],
        term_applicability={},
        terms_hash=case.batch.manifest.terms_hashes[member.item_id],
        context_hash=case.batch.manifest.context_hashes[member.item_id],
        stage="proofread",
        status=ItemStatus.LOCAL_VALID,
        target_projection=target,
        target_hash=canonical_hash(target),
        request_id=case.batch.manifest.request_id,
        checks={
            "translation_frame": {
                "request_id": case.batch.manifest.request_id,
                "member_ids": list(case.batch.manifest.item_ids),
                "batch_hash": canonical_hash(case.batch),
            }
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("decision", "final"), (("no_change", "你好，世界。"), ("replace", "您好，世界。")))
async def test_three_steps_save_initial_then_apply_current_review(case: ReadyCase, decision: str, final: str) -> None:
    calls: list[str] = []
    saved: list[ItemRecord] = []

    async def transport(kind, payload):
        calls.append(kind)
        if kind == "translate":
            items = [{"item_id": item["item_id"], "target": "你好，世界。"} for item in payload["items"]]
            protocol = "epubox-text-1"
        else:
            assert all(item["target"] == "你好，世界。" for item in payload["items"])
            items = [
                review_item(item, decision=decision, target=final if decision == "replace" else None)
                for item in payload["items"]
            ]
            protocol = "epubox-review-2"
        return {"raw": json.dumps({"protocol": protocol, "request_id": payload["request_id"], "items": items})}

    result = await run_workflow(
        case.prepared, case.batch, case.index, runtime(transport), session=case.session, save=saved.append
    )

    item_id = case.batch.items[0].item_id
    assert result.status == "completed" and result.results[item_id].target_projection == final
    assert calls == ["translate", "review"]
    assert [(record.stage, record.target_projection) for record in saved] == [
        ("proofread", "你好，世界。"),
        ("reviewed", final),
    ]


@pytest.mark.asyncio
async def test_invalid_replacement_retains_saved_draft_and_never_marks_it_reviewed(case: ReadyCase) -> None:
    saved: list[ItemRecord] = []

    async def transport(kind, payload):
        items = (
            [{"item_id": item["item_id"], "target": "你好，世界。"} for item in payload["items"]]
            if kind == "translate"
            else [
                review_item(
                    item,
                    decision="replace",
                    target="This replacement remains entirely untranslated and contains a complete English sentence.",
                )
                for item in payload["items"]
            ]
        )
        protocol = "epubox-text-1" if kind == "translate" else "epubox-review-2"
        return {"raw": json.dumps({"protocol": protocol, "request_id": payload["request_id"], "items": items})}

    result = await run_workflow(
        case.prepared, case.batch, case.index, runtime(transport), session=case.session, save=saved.append
    )

    record = result.results[case.batch.items[0].item_id]
    assert result.status == "needs_attention"
    assert record.status == ItemStatus.NEEDS_ATTENTION and record.target_projection == "你好，世界。"
    assert record.checks["accepted"] is False


@pytest.mark.asyncio
async def test_saved_draft_skips_translate_but_is_reviewed_against_its_current_text(case: ReadyCase) -> None:
    member = case.batch.items[0]
    draft = saved_draft(case, "已保存译文。")
    calls: list[str] = []

    async def transport(kind, payload):
        calls.append(kind)
        assert kind == "review" and payload["items"][0]["target"] == "已保存译文。"
        items = [review_item(payload["items"][0], decision="no_change")]
        return {
            "raw": json.dumps({"protocol": "epubox-review-2", "request_id": payload["request_id"], "items": items})
        }

    result = await run_workflow(
        case.prepared,
        case.batch,
        case.index,
        runtime(transport),
        session=case.session,
        save=lambda _record: None,
        records={member.item_id: draft},
    )
    assert result.status == "completed" and calls == ["review"]


@pytest.mark.asyncio
async def test_direct_translate_step_rejects_wrong_runtime_before_http(case: ReadyCase) -> None:
    calls: list[str] = []
    with pytest.raises(ValueError, match="model differs"):
        await workflow_module._translate_step(
            case.batch,
            runtime(lambda *args: calls.append(str(args)), model="wrong"),
            session=case.session,
            initial=False,
            save=lambda _record: None,
        )
    assert calls == []


@pytest.mark.asyncio
async def test_apply_rejects_a_record_changed_after_review_batch_creation(case: ReadyCase) -> None:
    member = case.batch.items[0]
    reviewed = saved_draft(case, "甲")
    packed = pack_members(
        "review",
        (member,),
        case.prepared.glossary,
        case.index,
        limits_for(case.prepared.preparation),
        targets={member.item_id: reviewed},
        revisions={member.unit_id: case.batch.manifest.revisions[member.unit_id]},
        tokenizer_model=MODEL,
    )
    review_batch = packed.batches[0]
    changed = reviewed.model_copy(update={"target_projection": "乙", "target_hash": canonical_hash("乙")})
    review_items = review_batch.payload.get("items")
    assert isinstance(review_items, list) and isinstance(review_items[0], dict)
    decision = review_item(review_items[0], decision="no_change")
    parsed = SimpleNamespace(accepted={member.item_id: decision}, errors={})

    with pytest.raises(ValueError, match="current saved target"):
        await workflow_module._apply_corrections_step(
            review_batch,
            parsed,
            {member.item_id: changed},
            save=lambda _record: None,
        )


@pytest.mark.asyncio
async def test_review_over_capacity_keeps_the_saved_translation_without_http(case: ReadyCase) -> None:
    member = case.batch.items[0]
    draft = saved_draft(case, "".join(chr(0x4E00 + (index * 7919) % 20_000) for index in range(50_000)))
    calls: list[str] = []
    saved: list[ItemRecord] = []

    result = await run_workflow(
        case.prepared,
        case.batch,
        case.index,
        runtime(lambda *args: calls.append(str(args))),
        session=case.session,
        save=saved.append,
        records={member.item_id: draft},
    )

    record = result.results[member.item_id]
    assert result.status == "needs_attention" and calls == []
    assert record.target_projection == draft.target_projection and record.status == ItemStatus.NEEDS_ATTENTION
    assert saved == [record]


@pytest.mark.asyncio
async def test_truncated_translation_and_ready_mismatch_dispatch_no_review_or_http(case: ReadyCase) -> None:
    calls: list[str] = []

    async def truncated(kind, payload):
        calls.append(kind)
        return {
            "raw": json.dumps({"protocol": "epubox-text-1", "request_id": payload["request_id"], "items": []}),
            "finish_reason": "length",
        }

    result = await run_workflow(
        case.prepared,
        case.batch,
        case.index,
        runtime(truncated),
        session=case.session,
        save=lambda _record: None,
    )
    assert result.status == "needs_attention" and calls == ["translate"]
    assert result.results[case.batch.items[0].item_id].target_projection is None

    blocked_calls: list[str] = []
    with pytest.raises(ValueError, match="model differs"):
        await run_workflow(
            case.prepared,
            case.batch,
            case.index,
            runtime(lambda *args: blocked_calls.append(str(args)), model="wrong"),
            session=case.session,
            save=lambda _record: None,
        )
    assert blocked_calls == []


@pytest.mark.asyncio
async def test_unknown_response_item_rejects_the_batch_without_review(case: ReadyCase) -> None:
    calls: list[str] = []

    async def transport(kind, payload):
        calls.append(kind)
        items = [
            {"item_id": payload["items"][0]["item_id"], "target": "你好，世界。"},
            {"item_id": "unknown", "target": "未知"},
        ]
        return {"raw": json.dumps({"protocol": "epubox-text-1", "request_id": payload["request_id"], "items": items})}

    result = await run_workflow(
        case.prepared,
        case.batch,
        case.index,
        runtime(transport),
        session=case.session,
        save=lambda _record: None,
    )
    assert result.status == "needs_attention" and calls == ["translate"]
    assert result.results[case.batch.items[0].item_id].target_projection is None


@pytest.mark.asyncio
async def test_partial_translation_resume_reuses_original_and_repacked_frames(multi: ReadyCase) -> None:
    saved: dict[str, ItemRecord] = {}

    class StopAfterFirstSave(RuntimeError):
        pass

    async def first_transport(kind, payload):
        assert kind == "translate"
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-text-1",
                    "request_id": payload["request_id"],
                    "items": [
                        {"item_id": item["item_id"], "target": f"第{index + 1}句。"}
                        for index, item in enumerate(payload["items"])
                    ],
                }
            )
        }

    def save_first(record: ItemRecord) -> None:
        saved[record.item_id] = record
        raise StopAfterFirstSave

    with pytest.raises(StopAfterFirstSave):
        await run_workflow(
            multi.prepared,
            multi.batch,
            multi.index,
            runtime(first_transport),
            session=multi.session,
            save=save_first,
        )
    assert set(saved) == {multi.batch.items[0].item_id}

    calls: list[str] = []

    async def resumed_transport(kind, payload):
        calls.append(kind)
        if kind == "translate":
            assert [item["item_id"] for item in payload["items"]] == [item.item_id for item in multi.batch.items[1:]]
            assert payload["context"][-1] == "First sentence."
            items = [
                {"item_id": item["item_id"], "target": f"续译{index + 2}。"}
                for index, item in enumerate(payload["items"])
            ]
            protocol = "epubox-text-1"
        else:
            items = [review_item(item, decision="no_change") for item in payload["items"]]
            protocol = "epubox-review-2"
        return {"raw": json.dumps({"protocol": protocol, "request_id": payload["request_id"], "items": items})}

    second = await run_workflow(
        multi.prepared,
        multi.batch,
        multi.index,
        runtime(resumed_transport),
        session=multi.session,
        save=lambda record: saved.__setitem__(record.item_id, record),
        records=saved,
    )
    assert second.status == "completed" and calls == ["translate", "review"]
    assert set(saved) == set(multi.batch.manifest.item_ids)

    repeated: list[str] = []
    third = await run_workflow(
        multi.prepared,
        multi.batch,
        multi.index,
        runtime(lambda *args: repeated.append(str(args))),
        session=multi.session,
        save=lambda _record: None,
        records=saved,
    )
    assert third.status == "completed" and repeated == []


def test_atomic_prompt_is_explicit_and_versioned_without_changing_legacy_prompt() -> None:
    legacy = {"protocol": "epubox-text-1", "request_id": "r1", "items": []}
    atomic = legacy | {"prompt_version": ATOMIC_PROMPT_VERSION}
    legacy_prompt = request_messages("translate", legacy)[0]["content"]
    atomic_prompt = request_messages("translate", atomic)[0]["content"]

    assert legacy_prompt != atomic_prompt
    assert "Context is read-only" in atomic_prompt
    assert "mode required" in atomic_prompt and "keep_source" in atomic_prompt
    with pytest.raises(ValueError, match="unsupported atomic"):
        request_messages("translate", atomic | {"prompt_version": "epubox-members-99"})


def test_only_complete_workflow_is_a_supported_public_entry() -> None:
    assert workflow_module.__all__ == ["WorkflowResult", "run_workflow"]
    assert not hasattr(workflow_module, "translate_step")
    assert not hasattr(workflow_module, "proofread_step")
    assert not hasattr(workflow_module, "apply_corrections_step")


@pytest.mark.asyncio
async def test_runtime_rechecks_dispatch_guard_before_every_transport_retry() -> None:
    attempts = 0
    guarded = 0

    async def transport(_kind, _payload):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProviderError("retry", status_code=500)
        return {"raw": "{}"}

    async def sleep(_seconds):
        return None

    def guard():
        nonlocal guarded
        guarded += 1

    model = ModelRuntime(
        model=SimpleNamespace(id=MODEL),
        transport=transport,
        model_max_output_tokens=10,
        input_budget_version=2,
        max_transport_retries=1,
        sleep=sleep,
    )
    payload = {
        "protocol": "epubox-text-1",
        "prompt_version": ATOMIC_PROMPT_VERSION,
        "request_id": "r1",
        "items": [],
        "context": [],
    }
    await model.invoke("translate", payload, {"request_id": "r1", "output_tokens": 10}, dispatch_guard=guard)
    assert attempts == guarded == 2
