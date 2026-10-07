from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from engine.agents.runtime import RequestError, RuntimePaused
from engine.agents.workflow import run_workflow
from engine.epub.preparation import PreparationConfig
from engine.execution.repair import run_translation
from engine.item.atoms import ADAPTER_VERSION, EXTRACTOR_VERSION
from engine.schemas.contracts import ItemRecord, ItemStatus, canonical_hash, canonical_json_bytes
from engine.schemas.members import MemberBatch
from engine.services import state
from engine.services.atomic import IdentityMismatch, StaleWrite
from engine.services.coherence import add_http_budget
from engine.services.custody import retry_checks
from engine.services.journal import BodyJournal
from engine.services.preparation import prepare_translation
from engine.services.ready import ReadySession
from engine.services.resume import plan_resume
from engine.services.store import RunStore
from tests.engine.agents.workflow import MODEL, ReadyCase, prepare_case, review_item
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker


def _answer(kind, payload):
    if kind == "translate":
        items = [
            {"item_id": item["item_id"], "target": f"译文{index + 1}。"} for index, item in enumerate(payload["items"])
        ]
        protocol = "epubox-text-1"
    else:
        items = [review_item(item, decision="no_change") for item in payload["items"]]
        protocol = "epubox-review-2"
    return {
        "raw": json.dumps({"protocol": protocol, "request_id": payload["request_id"], "items": items}),
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


def _limited_case(root: Path) -> ReadyCase:
    source = make_epub(root / "source.epub", {"chapter.xhtml": "<p>First.</p>"})
    result = asyncio.run(
        prepare_translation(
            source,
            root / "work",
            PreparationConfig(
                run_id="limited",
                auto_extract=False,
                adapter_version=ADAPTER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                translation_config={
                    "model": MODEL,
                    "max_source_tokens": 5000,
                    "max_input_tokens": 50_000,
                    "max_output_tokens": 10_000,
                    "context_tokens": 60_000,
                    "run_http_limit": 1,
                },
            ),
            StubChecker(),
        )
    )
    assert result.prepared is not None
    session = ReadySession(RunStore(result.work_dir))
    batch = next(
        MemberBatch.model_validate_json(path.read_bytes())
        for path in (result.work_dir / "batches").glob("*.json")
        if b"First." in path.read_bytes()
    )
    return ReadyCase(session.prepared, batch, session.index, session)


def _tree(root: Path) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def _compact_case(tmp_path: Path, text: str = "<p>First.</p><p>Second.</p>") -> ReadyCase:
    case = prepare_case(tmp_path / "legacy", text, ("First.", "Second."))
    source = case.session.store.root / "source.epub"
    root = tmp_path / "book"
    preparation = case.prepared.preparation
    state.initialize(root, source, preparation.source_hash, preparation.run_id)
    state.import_records(root, case.session.store.root)
    session = ReadySession(RunStore(root))
    return ReadyCase(session.prepared, case.batch, session.index, session)


@pytest.mark.parametrize("invalid", (True, -1, "1"))
def test_retry_checks_omit_zero_preserve_positive_and_reject_malformed_epoch(invalid) -> None:
    record = ItemRecord(
        item_id="item-1",
        segment_id="item-1",
        terms_hash="terms",
        context_hash="context",
        checks={"translation_frame": {"request_id": "r1", "member_ids": ["item-1"], "batch_hash": "hash"}},
    )
    assert retry_checks(record.model_copy(update={"checks": record.checks | {"translation_epoch": 0}}), 2) == {
        "translation_frame": record.checks["translation_frame"],
        "review_epoch": 2,
    }
    assert (
        retry_checks(record.model_copy(update={"checks": record.checks | {"translation_epoch": 1}}), 2)[
            "translation_epoch"
        ]
        == 1
    )
    with pytest.raises(IdentityMismatch, match="translation epoch"):
        retry_checks(record.model_copy(update={"checks": record.checks | {"translation_epoch": invalid}}), 2)


def test_workflow_results_attempts_usage_and_parent_targets_survive_restart(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p>", ("First.", "Second."))
    journal = BodyJournal(case.session.store, case.session)
    calls: list[str] = []
    events: list[dict] = []

    async def transport(kind, payload):
        calls.append(kind)
        return _answer(kind, payload)

    result = asyncio.run(
        run_workflow(
            case.prepared,
            case.batch,
            case.index,
            journal.runtime(transport=transport, progress=events.append),
            session=journal.session,
            save=journal.save,
            records=journal.records(case.batch.manifest.item_ids),
        )
    )

    resumed = BodyJournal(case.session.store)
    saved = resumed.records(case.batch.manifest.item_ids)
    progress = resumed.progress_snapshot()
    assert result.status == "completed" and calls == ["translate", "review"]
    assert all(record.status == ItemStatus.REVIEWED for record in saved.values())
    assert {"译文1。", "译文2。"}.issubset(set(resumed.parent_targets(require_complete=False).values()))
    assert progress["accepted_units"] == 2 and progress["http_attempts"] == 2
    assert (progress["input_tokens"], progress["output_tokens"]) == (22, 14)
    assert [(event["event"], event["stage"]) for event in events] == [
        ("request", "translate"),
        ("response", "translate"),
        ("request", "review"),
        ("response", "review"),
    ]
    assert all(event["item_ids"] for event in events if event["event"] == "request")
    assert all(event["actual_input_tokens"] == 11 for event in events if event["event"] == "response")


def test_persisted_response_replays_before_transport_after_finish_crash(tmp_path, monkeypatch) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store, case.session)
    original = case.session.store.finish_attempt
    calls: list[str] = []

    def interrupted(*args, **kwargs):
        if kwargs["state"] == "succeeded":
            raise RuntimeError("crash after response")
        return original(*args, **kwargs)

    monkeypatch.setattr(case.session.store, "finish_attempt", interrupted)

    async def first(kind, payload):
        calls.append(kind)
        return _answer(kind, payload)

    with pytest.raises(RuntimeError, match="crash after response"):
        asyncio.run(
            run_workflow(
                case.prepared,
                case.batch,
                case.index,
                journal.runtime(transport=first),
                session=journal.session,
                save=journal.save,
                records=journal.records(case.batch.manifest.item_ids),
            )
        )
    after_response = journal.progress_snapshot()
    assert (after_response["input_tokens"], after_response["output_tokens"]) == (11, 7)
    assert after_response["required_units"] == case.prepared.plan.required_unit_count

    monkeypatch.setattr(case.session.store, "finish_attempt", original)
    resumed = BodyJournal(case.session.store)
    assert resumed.progress_snapshot()["input_tokens"] == 11
    assert resumed.progress_snapshot()["output_tokens"] == 7
    resumed.recover_results()
    translated_request = next(request for request in resumed._requests.values() if request.stage == "translate")
    assert translated_request.attempts[-1].state == "succeeded"
    assert translated_request.attempts[-1].usage is not None
    assert translated_request.attempts[-1].usage.input_tokens == 11
    assert resumed.progress_snapshot()["input_tokens"] == 11
    assert resumed.progress_snapshot()["output_tokens"] == 7

    async def second(kind, payload):
        calls.append(kind)
        assert kind == "review"
        return _answer(kind, payload)

    result = asyncio.run(
        run_workflow(
            resumed.session.prepared,
            case.batch,
            resumed.session.index,
            resumed.runtime(transport=second),
            session=resumed.session,
            save=resumed.save,
            records=resumed.records(case.batch.manifest.item_ids),
        )
    )

    assert result.status == "completed" and calls == ["translate", "review"]
    assert resumed.progress_snapshot()["http_attempts"] == 2
    assert resumed.progress_snapshot()["input_tokens"] == 22
    assert resumed.progress_snapshot()["output_tokens"] == 14


def test_reviewed_result_is_idempotent_and_cannot_roll_back(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store, case.session)

    async def transport(kind, payload):
        return _answer(kind, payload)

    result = asyncio.run(
        run_workflow(
            case.prepared,
            case.batch,
            case.index,
            journal.runtime(transport=transport),
            session=journal.session,
            save=journal.save,
            records=journal.records(case.batch.manifest.item_ids),
        )
    )
    record = next(iter(result.results.values()))
    journal.save(record)
    with pytest.raises(StaleWrite, match="terminal"):
        journal.save(record.model_copy(update={"stage": "proofread", "status": ItemStatus.LOCAL_VALID}))


def test_runtime_rejects_a_model_outside_the_frozen_ready_identity(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    with pytest.raises(IdentityMismatch, match="model"):
        BodyJournal(case.session.store, case.session).runtime(
            model=type("Model", (), {"id": "changed-model"})(), transport=lambda *_args: None
        )


def test_partial_saved_batch_replans_pending_members_without_rejecting_their_new_frame(tmp_path) -> None:
    case = prepare_case(
        tmp_path,
        "<p>First.</p><p>Second.</p><p>Third.</p>",
        ("First.", "Second.", "Third."),
    )
    journal = BodyJournal(case.session.store, case.session)
    calls: list[str] = []

    async def first(kind, payload):
        calls.append(kind)
        assert kind == "translate"
        return _answer(kind, payload)

    def stop(record):
        journal.save(record)
        raise RuntimeError("stop after first durable member")

    with pytest.raises(RuntimeError, match="first durable"):
        asyncio.run(
            run_workflow(
                case.prepared,
                case.batch,
                case.index,
                journal.runtime(transport=first),
                session=journal.session,
                save=stop,
                records=journal.records(case.batch.manifest.item_ids),
            )
        )

    resumed = BodyJournal(case.session.store)
    resumed.recover_results()

    async def second(kind, payload):
        calls.append(kind)
        return _answer(kind, payload)

    result = asyncio.run(
        run_workflow(
            resumed.session.prepared,
            case.batch,
            resumed.session.index,
            resumed.runtime(transport=second),
            session=resumed.session,
            save=resumed.save,
            records=resumed.records(case.batch.manifest.item_ids),
        )
    )
    assert result.status == "completed" and calls == ["translate", "review"]


def test_fabricated_reviewed_result_is_rejected_even_with_a_self_consistent_hash(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store, case.session)

    async def transport(kind, payload):
        return _answer(kind, payload)

    asyncio.run(
        run_workflow(
            case.prepared,
            case.batch,
            case.index,
            journal.runtime(transport=transport),
            session=journal.session,
            save=journal.save,
            records=journal.records(case.batch.manifest.item_ids),
        )
    )
    item_id = case.batch.items[0].item_id
    record = journal.records((item_id,))[item_id]
    forged = record.model_copy(update={"target_projection": "伪造。", "target_hash": canonical_hash("伪造。")})
    case.session.store._base.atomic_write_bytes(
        case.session.store.root / "results" / f"{item_id}.json", canonical_json_bytes(forged)
    )
    with pytest.raises(IdentityMismatch, match="persisted responses"):
        BodyJournal(case.session.store)
    preview = plan_resume(case.session.store.root)
    assert preview.status == "needs_attention" and preview.actions == ("repair_shared_identity",)


@pytest.mark.parametrize("outcome", ("sent", "unknown"))
def test_dispatched_attempt_without_response_is_local_error_without_duplicate_transport(tmp_path, outcome) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store, case.session)
    calls = 0

    class HardCrash(BaseException):
        pass

    async def first(_kind, _payload):
        nonlocal calls
        calls += 1
        if outcome == "sent":
            raise HardCrash
        raise OSError("provider outcome unknown")

    runtime = journal.runtime(transport=first)

    async def no_sleep(_seconds):
        return None

    runtime._sleep = no_sleep
    manifest = case.batch.manifest.model_dump(mode="python") | {"output_tokens": case.batch.budget.output_tokens}
    expected = HardCrash if outcome == "sent" else RequestError
    with pytest.raises(expected):
        asyncio.run(runtime.invoke("translate", case.batch.payload, manifest))
    before = BodyJournal(case.session.store).progress_snapshot()

    async def forbidden(*_args):
        raise AssertionError("ambiguous provider outcome must not be resent")

    resumed = BodyJournal(case.session.store)
    with pytest.raises(RequestError, match="unknown provider outcome"):
        asyncio.run(resumed.runtime(transport=forbidden).invoke("translate", case.batch.payload, manifest))
    after = resumed.progress_snapshot()
    assert calls == 1
    assert before["http_attempts"] == after["http_attempts"] == 1
    assert before["body_http_attempts"] == after["body_http_attempts"] == 1


def test_explicit_retry_reopens_all_pending_members_of_a_shared_unknown_translation(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p><p>Third.</p>", ("First.", "Second.", "Third."))
    journal = BodyJournal(case.session.store, case.session)
    calls: list[str] = []

    async def unknown(kind, _payload):
        calls.append(kind)
        raise OSError("provider outcome unknown")

    runtime = journal.runtime(transport=unknown)

    async def no_sleep(_seconds):
        return None

    runtime._sleep = no_sleep
    manifest = case.batch.manifest.model_dump(mode="python") | {"output_tokens": case.batch.budget.output_tokens}
    with pytest.raises(RequestError, match="unknown provider outcome"):
        asyncio.run(runtime.invoke("translate", case.batch.payload, manifest))

    units = tuple(dict.fromkeys(item.unit_id for item in case.batch.items))
    assert set(journal.validate_retry_units(units)) == set(case.batch.manifest.item_ids)
    assert set(journal.retry_units(units)) == set(case.batch.manifest.item_ids)
    assert journal.progress_snapshot()["body_http_attempts"] == 1

    async def valid(kind, payload):
        calls.append(kind)
        if kind == "review":
            items = [review_item(item, decision="needs_attention") for item in payload["items"]]
            return {
                "raw": json.dumps({"protocol": "epubox-review-2", "request_id": payload["request_id"], "items": items})
            }
        return _answer(kind, payload)

    attention = asyncio.run(
        run_workflow(
            journal.session.prepared,
            case.batch,
            journal.session.index,
            journal.runtime(transport=valid),
            session=journal.session,
            save=journal.save,
            records=journal.records(case.batch.manifest.item_ids),
        )
    )
    assert attention.status == "needs_attention" and calls == ["translate", "translate", "review"]
    assert all(
        record.checks.get("translation_epoch") == 1
        for record in journal.records(case.batch.manifest.item_ids).values()
    )
    assert set(journal.retry_units(units)) == set(case.batch.manifest.item_ids)
    restarted = BodyJournal(case.session.store)
    restarted.recover_results()

    async def reviewed(kind, payload):
        calls.append(kind)
        assert kind == "review"
        return _answer(kind, payload)

    completed = asyncio.run(
        run_workflow(
            restarted.session.prepared,
            case.batch,
            restarted.session.index,
            restarted.runtime(transport=reviewed),
            session=restarted.session,
            save=restarted.save,
            records=restarted.records(case.batch.manifest.item_ids),
        )
    )
    assert completed.status == "completed" and calls == ["translate", "translate", "review", "review"]
    translation_requests = [request for request in journal._requests.values() if request.stage == "translate"]
    assert len(translation_requests) == 2
    assert sorted(attempt.state for request in translation_requests for attempt in request.attempts) == [
        "failed",
        "succeeded",
    ]


def test_authorized_body_quota_addition_survives_restart_without_resetting_attempts(tmp_path) -> None:
    case = _limited_case(tmp_path)
    journal = BodyJournal(case.session.store, case.session)
    calls: list[str] = []

    async def transport(kind, payload):
        calls.append(kind)
        return _answer(kind, payload)

    with pytest.raises(RuntimePaused, match="limit is exhausted"):
        asyncio.run(
            run_workflow(
                case.prepared,
                case.batch,
                case.index,
                journal.runtime(model=SimpleNamespace(id=MODEL), transport=transport),
                session=journal.session,
                save=journal.save,
                records=journal.records(case.batch.manifest.item_ids),
            )
        )
    assert calls == ["translate"] and journal.progress_snapshot()["body_http_attempts"] == 1

    add_http_budget(case.session.store, authorization_id="allow-review", add_run_http=1)
    resumed = BodyJournal(case.session.store)
    result = asyncio.run(
        run_workflow(
            resumed.session.prepared,
            case.batch,
            resumed.session.index,
            resumed.runtime(model=SimpleNamespace(id=MODEL), transport=transport),
            session=resumed.session,
            save=resumed.save,
            records=resumed.records(case.batch.manifest.item_ids),
        )
    )
    restarted = BodyJournal(case.session.store)
    assert result.status == "completed" and calls == ["translate", "review"]
    assert restarted.progress_snapshot()["body_http_attempts"] == 2


@pytest.mark.parametrize("damage", ("terms", "context", "frame"))
def test_read_only_resume_rejects_changed_saved_frame_without_mutating_disk(tmp_path, damage) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store, case.session)

    async def transport(kind, payload):
        return _answer(kind, payload)

    asyncio.run(
        run_workflow(
            case.prepared,
            case.batch,
            case.index,
            journal.runtime(transport=transport),
            session=journal.session,
            save=journal.save,
            records=journal.records(case.batch.manifest.item_ids),
        )
    )
    item_id = case.batch.items[0].item_id
    record = journal.records((item_id,))[item_id]
    if damage == "terms":
        forged = record.model_copy(update={"terms_hash": "forged-terms"})
    elif damage == "context":
        forged = record.model_copy(update={"context_hash": "forged-context"})
    else:
        checks = dict(record.checks)
        frame = cast(dict[str, Any], checks["translation_frame"])
        checks["translation_frame"] = dict(frame) | {"batch_hash": "forged-frame"}
        forged = record.model_copy(update={"checks": checks})
    case.session.store._base.atomic_write_bytes(
        case.session.store.root / "results" / f"{item_id}.json", canonical_json_bytes(forged)
    )
    before = _tree(case.session.store.root)
    preview = plan_resume(case.session.store.root)
    assert preview.status == "needs_attention" and preview.actions == ("repair_shared_identity",)
    assert before == _tree(case.session.store.root)


def test_malformed_translation_is_saved_as_structured_attention_with_its_request_frame(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store, case.session)

    async def malformed(_kind, payload):
        return {"raw": json.dumps({"protocol": "epubox-text-1", "request_id": payload["request_id"], "items": []})}

    result = asyncio.run(
        run_workflow(
            case.prepared,
            case.batch,
            case.index,
            journal.runtime(transport=malformed),
            session=journal.session,
            save=journal.save,
            records=journal.records(case.batch.manifest.item_ids),
        )
    )
    record = journal.records(case.batch.manifest.item_ids)[case.batch.items[0].item_id]
    assert result.status == "needs_attention" and record.status == ItemStatus.NEEDS_ATTENTION
    assert record.request_id == case.batch.manifest.request_id
    frame = cast(dict[str, Any], record.checks["translation_frame"])
    assert frame["batch_hash"] == canonical_hash(case.batch)
    assert record.failure == {
        "stage": "translate",
        "code": "translate_failed",
        "message": "translation item missing",
    }

    async def forbidden(*_args):
        raise AssertionError("attention must not retry implicitly")

    repeated = asyncio.run(
        run_workflow(
            journal.session.prepared,
            case.batch,
            journal.session.index,
            journal.runtime(transport=forbidden),
            session=journal.session,
            save=journal.save,
            records=journal.records(case.batch.manifest.item_ids),
        )
    )
    assert repeated.status == "needs_attention"
    unit_id = case.batch.items[0].unit_id
    assert journal.validate_retry_units((unit_id,)) == (record.item_id,)
    assert journal.retry_units((unit_id,)) == (record.item_id,)
    restarted = BodyJournal(case.session.store)
    before_attempts = restarted.progress_snapshot()["body_http_attempts"]
    restarted.recover_results()
    assert restarted.records((record.item_id,))[record.item_id].status == ItemStatus.PENDING
    assert restarted.progress_snapshot()["body_http_attempts"] == before_attempts

    async def valid(kind, payload):
        return _answer(kind, payload)

    resumed = asyncio.run(
        run_workflow(
            restarted.session.prepared,
            case.batch,
            restarted.session.index,
            restarted.runtime(transport=valid),
            session=restarted.session,
            save=restarted.save,
            records=restarted.records(case.batch.manifest.item_ids),
        )
    )
    translation_requests = [request for request in restarted._requests.values() if request.stage == "translate"]
    assert resumed.status == "completed" and len(translation_requests) == 2
    assert sum(len(request.attempts) for request in translation_requests) == 2
    preview = plan_resume(case.session.store.root)
    assert preview.phase == "translation" and preview.status == "ready"
    assert preview.actions == ("translate",) and "repair_shared_identity" not in preview.actions


def test_explicit_review_retry_uses_new_revision_without_retranslating(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store, case.session)
    calls: list[str] = []

    async def first(kind, payload):
        calls.append(kind)
        if kind == "translate":
            return _answer(kind, payload)
        items = [review_item(item, decision="needs_attention") for item in payload["items"]]
        return {
            "raw": json.dumps({"protocol": "epubox-review-2", "request_id": payload["request_id"], "items": items})
        }

    result = asyncio.run(
        run_workflow(
            case.prepared,
            case.batch,
            case.index,
            journal.runtime(transport=first),
            session=journal.session,
            save=journal.save,
            records=journal.records(case.batch.manifest.item_ids),
        )
    )
    assert result.status == "needs_attention"
    assert journal.retry_units((case.batch.items[0].unit_id,)) == (case.batch.items[0].item_id,)

    async def second(kind, payload):
        calls.append(kind)
        assert kind == "review" and payload["items"][0]["base_revision"] == 1
        return _answer(kind, payload)

    resumed = asyncio.run(
        run_workflow(
            journal.session.prepared,
            case.batch,
            journal.session.index,
            journal.runtime(transport=second),
            session=journal.session,
            save=journal.save,
            records=journal.records(case.batch.manifest.item_ids),
        )
    )
    assert resumed.status == "completed" and calls == ["translate", "review", "review"]


def test_shared_review_retry_reconstructs_old_response_from_its_frozen_epoch(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p><p>Third.</p>", ("First.", "Second.", "Third."))
    journal = BodyJournal(case.session.store, case.session)
    calls: list[str] = []

    async def first(kind, payload):
        calls.append(kind)
        if kind == "translate":
            return _answer(kind, payload)
        items = [review_item(item, decision="needs_attention") for item in payload["items"]]
        return {
            "raw": json.dumps({"protocol": "epubox-review-2", "request_id": payload["request_id"], "items": items})
        }

    result = asyncio.run(
        run_workflow(
            case.prepared,
            case.batch,
            case.index,
            journal.runtime(transport=first),
            session=journal.session,
            save=journal.save,
            records=journal.records(case.batch.manifest.item_ids),
        )
    )
    assert result.status == "needs_attention"
    units = tuple(dict.fromkeys(item.unit_id for item in case.batch.items))
    journal.retry_units(units)

    resumed = BodyJournal(case.session.store)
    resumed.recover_results()
    assert all(
        record.status == ItemStatus.LOCAL_VALID for record in resumed.records(case.batch.manifest.item_ids).values()
    )

    async def second(kind, payload):
        calls.append(kind)
        assert kind == "review"
        return _answer(kind, payload)

    completed = asyncio.run(
        run_workflow(
            resumed.session.prepared,
            case.batch,
            resumed.session.index,
            resumed.runtime(transport=second),
            session=resumed.session,
            save=resumed.save,
            records=resumed.records(case.batch.manifest.item_ids),
        )
    )
    assert completed.status == "completed" and calls == ["translate", "review", "review"]


def test_saved_draft_tamper_is_rejected_before_review_and_read_only_preview_is_unchanged(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store, case.session)

    class HardCrash(BaseException):
        pass

    async def transport(kind, payload):
        if kind == "review":
            raise HardCrash
        return _answer(kind, payload)

    with pytest.raises(HardCrash):
        asyncio.run(
            run_workflow(
                case.prepared,
                case.batch,
                case.index,
                journal.runtime(transport=transport),
                session=journal.session,
                save=journal.save,
                records=journal.records(case.batch.manifest.item_ids),
            )
        )
    item_id = case.batch.items[0].item_id
    record = journal.records((item_id,))[item_id]
    forged = record.model_copy(update={"target_projection": "伪造。", "target_hash": canonical_hash("伪造。")})
    case.session.store._base.atomic_write_bytes(
        case.session.store.root / "results" / f"{item_id}.json", canonical_json_bytes(forged)
    )
    before = _tree(case.session.store.root)
    with pytest.raises(IdentityMismatch, match="persisted translation response"):
        BodyJournal(case.session.store)
    assert plan_resume(case.session.store.root).status == "needs_attention"
    assert before == _tree(case.session.store.root)


def test_partial_unit_selection_cannot_unlock_a_shared_unknown_review(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p>", ("First.", "Second."))
    journal = BodyJournal(case.session.store, case.session)

    class HardCrash(BaseException):
        pass

    async def transport(kind, payload):
        if kind == "review":
            raise HardCrash
        return _answer(kind, payload)

    with pytest.raises(HardCrash):
        asyncio.run(
            run_workflow(
                case.prepared,
                case.batch,
                case.index,
                journal.runtime(transport=transport),
                session=journal.session,
                save=journal.save,
                records=journal.records(case.batch.manifest.item_ids),
            )
        )
    before = _tree(case.session.store.root)
    counters = journal.progress_snapshot()
    with pytest.raises(IdentityMismatch, match="every Unit"):
        journal.validate_retry_units((case.batch.items[0].unit_id,))
    assert before == _tree(case.session.store.root)
    assert counters == journal.progress_snapshot()


def test_local_attention_does_not_stop_later_ready_batches(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p><p>Third.</p>", ("First.", "Second.", "Third."))
    assert len(case.prepared.plan.batch_hashes) >= 3
    calls: list[tuple[str, str]] = []

    async def transport(kind, payload):
        calls.append((kind, payload["request_id"]))
        if len(calls) == 1:
            return {"raw": json.dumps({"protocol": "epubox-text-1", "request_id": payload["request_id"], "items": []})}
        return _answer(kind, payload)

    result = asyncio.run(
        run_translation(
            case.session.store.root,
            model=SimpleNamespace(id=MODEL),
            transport=transport,
        )
    )
    assert result.status == "needs_attention" and result.reason is not None
    assert len({request_id for _, request_id in calls}) >= 3


def test_selective_retry_skips_old_shared_translation_response_for_the_new_epoch(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p><p>Third.</p>", ("First.", "Second.", "Third."))
    journal = BodyJournal(case.session.store, case.session)

    async def missing(_kind, payload):
        return {"raw": json.dumps({"protocol": "epubox-text-1", "request_id": payload["request_id"], "items": []})}

    result = asyncio.run(
        run_workflow(
            case.prepared,
            case.batch,
            case.index,
            journal.runtime(transport=missing),
            session=journal.session,
            save=journal.save,
            records=journal.records(case.batch.manifest.item_ids),
        )
    )
    assert result.status == "needs_attention"
    first = case.batch.items[0]
    journal.retry_units((first.unit_id,))

    restarted = BodyJournal(case.session.store)
    restarted.recover_results()
    records = restarted.records(case.batch.manifest.item_ids)
    assert records[first.item_id].status == ItemStatus.PENDING
    assert all(records[item.item_id].status == ItemStatus.NEEDS_ATTENTION for item in case.batch.items[1:])


def test_compact_response_siblings_commit_once_per_stage(tmp_path, monkeypatch) -> None:
    case = _compact_case(tmp_path)
    journal = BodyJournal(case.session.store, case.session)
    commits: list[int] = []
    previous = {path.name: state.read(path) for path in state.glob(case.session.store.root / "results", "*.json")}
    original = state._commit

    def counted(root, value):
        current = {
            Path(key).name: record["data"].encode()
            for key, record in value["records"].items()
            if key.startswith("results/")
        }
        changed = sum(previous.get(key) != data for key, data in current.items())
        if changed:
            commits.append(changed)
        previous.clear()
        previous.update(current)
        return original(root, value)

    monkeypatch.setattr(state, "_commit", counted)

    async def transport(kind, payload):
        return _answer(kind, payload)

    result = asyncio.run(
        run_workflow(
            case.prepared,
            case.batch,
            case.index,
            journal.runtime(transport=transport),
            session=journal.session,
            save=journal.save,
            records=journal.records(case.batch.manifest.item_ids),
        )
    )

    assert result.status == "completed"
    assert commits == [2, 2]


def test_compact_group_rejects_one_malformed_sibling_without_partial_state(tmp_path) -> None:
    case = _compact_case(tmp_path)
    journal = BodyJournal(case.session.store, case.session)
    saved: list[ItemRecord] = []

    async def transport(kind, payload):
        return _answer(kind, payload)

    asyncio.run(
        run_workflow(
            case.prepared,
            case.batch,
            case.index,
            journal.runtime(transport=transport),
            session=journal.session,
            save=saved.append,
            records=journal.records(case.batch.manifest.item_ids),
        )
    )
    drafts = [record for record in saved if record.status == ItemStatus.LOCAL_VALID]
    malformed = drafts[1].model_copy(update={"terms_hash": "changed"})
    before_state = (case.session.store.root / "state.json").read_bytes()
    before_records = journal.records()
    before_progress = journal.progress_snapshot()

    with pytest.raises((IdentityMismatch, ValueError), match="identity changed|frozen source|saved result"):
        journal.save_many((drafts[0], malformed))

    assert (case.session.store.root / "state.json").read_bytes() == before_state
    assert journal.records() == before_records
    assert journal.progress_snapshot() == before_progress


def test_compact_saved_multi_item_response_replays_without_translate_http(tmp_path, monkeypatch) -> None:
    case = _compact_case(tmp_path)
    journal = BodyJournal(case.session.store, case.session)
    first_calls: list[str] = []
    original = journal.save_many

    def interrupted(records):
        raise RuntimeError("crash before grouped result commit")

    monkeypatch.setattr(journal, "save_many", interrupted)

    async def first(kind, payload):
        first_calls.append(kind)
        return _answer(kind, payload)

    with pytest.raises(RuntimeError, match="grouped result"):
        asyncio.run(
            run_workflow(
                case.prepared,
                case.batch,
                case.index,
                journal.runtime(transport=first),
                session=journal.session,
                save=journal.save,
                records=journal.records(case.batch.manifest.item_ids),
            )
        )
    monkeypatch.setattr(journal, "save_many", original)
    second_calls: list[str] = []
    resumed = BodyJournal(case.session.store)

    async def second(kind, payload):
        second_calls.append(kind)
        assert kind == "review"
        return _answer(kind, payload)

    result = asyncio.run(
        run_workflow(
            resumed.session.prepared,
            case.batch,
            resumed.session.index,
            resumed.runtime(transport=second),
            session=resumed.session,
            save=resumed.save,
            records=resumed.records(case.batch.manifest.item_ids),
        )
    )

    assert result.status == "completed"
    assert first_calls == ["translate"]
    assert second_calls == ["review"]
