from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.agents.workflow import run_workflow
from engine.epub.preparation import PreparationConfig
from engine.execution.parts import ensure_part_plans
from engine.orchestrator import run_translation
from engine.schemas.contracts import RequestManifest, canonical_json_bytes
from engine.services import state
from engine.services.journal import BodyJournal
from engine.services.partition import build_plan, plan_path
from engine.services.parts import PartJournal
from engine.services.preparation import prepare_translation
from engine.services.resume import plan_resume
from engine.services.terms.planning import TERM_PLANNER_VERSION
from tests.engine.agents.test_workflow import prepare_case, review_item
from tests.engine.epub.test_factory import make_epub
from tests.engine.epub.test_preparation import StubChecker
from tests.engine.execution.test_atomic import answer
from tests.engine.services.test_partition import _adaptive_case, _case


def _tree(root: Path) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def _parts_case(tmp_path: Path, *, complete: bool):
    session, _parent = _case(tmp_path)
    root = BodyJournal(session.store, session)
    plans = ensure_part_plans(root)
    assert plans
    parts = tuple(PartJournal.initialize(root, plan) for plan in plans)
    part = parts[0]
    parent = session.index.members_by_id[part.plan.parent_id]
    if not complete:
        return root, part, parent

    async def transport(kind, payload):
        if kind == "translate":
            items = [
                {
                    "item_id": item["item_id"],
                    "target": (
                        item["source"]
                        .replace("Entry", "条目")
                        .replace("word", "文字")
                        .replace("Reliable systems", "可靠系统")
                        .replace("Contents", "目录")
                        .replace("Chapter", "章节")
                    ),
                }
                for item in payload["items"]
            ]
            protocol = "epubox-text-1"
        else:
            items = [review_item(item, decision="no_change") for item in payload["items"]]
            protocol = "epubox-review-2"
        return {"raw": json.dumps({"protocol": protocol, "request_id": payload["request_id"], "items": items})}

    async def execute() -> None:
        runtime = root.runtime(transport=transport)
        part_parents = {plan.parent_id for plan in plans}
        for batch in session._prepared_batches.values():
            if part_parents.intersection(batch.manifest.item_ids):
                continue
            result = await run_workflow(
                session.prepared,
                batch,
                session.index,
                runtime,
                session=session,
                save=root.save,
                records=root.records(batch.manifest.item_ids),
            )
            assert result.status == "completed"
        for current in parts:
            for batch in current.pending_batches():
                with root.activate(current):
                    result = await run_workflow(
                        session.prepared,
                        batch,
                        current.session.index,
                        runtime,
                        session=current.session,
                        save=current.save,
                        records=current.records(batch.manifest.item_ids),
                    )
                assert result.status == "completed"
            assert current.compose() is not None

    asyncio.run(execute())
    return root, part, parent


def test_read_only_resume_plan_is_stable_and_sends_no_requests(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    prepared = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            PreparationConfig(
                run_id="plan-preview",
                auto_extract=False,
                extraction_config={"strategy": TERM_PLANNER_VERSION},
            ),
            StubChecker(),
        )
    )
    before = _tree(prepared.work_dir)
    first = plan_resume(prepared.work_dir)
    second = plan_resume(prepared.work_dir)

    assert first == second
    assert first.phase == "translation" and first.actions == ("translate",)
    assert before == _tree(prepared.work_dir)
    assert not list((prepared.work_dir / "requests").glob("*.json"))


def test_old_bookplan_is_located_without_running_legacy_code(tmp_path: Path) -> None:
    (tmp_path / "bookplan.json").write_text('{"format":"epubox-book-1"}')

    preview = plan_resume(tmp_path)

    assert preview.status == "unsupported_format"
    assert preview.actions == ("start_new_run",)


def test_preview_does_not_claim_publication_when_frozen_source_changes(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    prepared = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            PreparationConfig(
                run_id="identity-check",
                auto_extract=False,
                extraction_config={"strategy": TERM_PLANNER_VERSION},
            ),
            StubChecker(),
        )
    )
    snapshot = prepared.work_dir / "source.epub"
    snapshot.chmod(0o644)
    snapshot.write_bytes(b"tampered")

    preview = plan_resume(prepared.work_dir)

    assert preview.status == "needs_attention"
    assert preview.actions == ("repair_shared_identity",)
    assert "source.epub" in preview.reasons


def test_unknown_prepared_marker_is_read_only_unsupported_without_conversion(tmp_path: Path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    marker = case.session.store.root / "prepared.json"
    marker.write_text('{"format":"epubox-prepared-1"}')
    before = _tree(case.session.store.root)

    preview = plan_resume(case.session.store.root)

    assert preview.status == "unsupported_format" and preview.actions == ("start_new_run",)
    assert before == _tree(case.session.store.root)


def test_preview_accepts_proven_part_composite_without_writing(tmp_path: Path) -> None:
    root, _part, _parent = _parts_case(tmp_path, complete=True)
    before = _tree(root.store.root)

    preview = plan_resume(root.store.root)

    assert preview.phase == "publication"
    assert preview.status == "ready" and preview.actions == ("verify_and_publish",)
    assert before == _tree(root.store.root)


def test_preview_reports_inflight_parts_as_translation_without_writing(tmp_path: Path) -> None:
    root, _part, parent = _parts_case(tmp_path, complete=False)
    terms = RequestManifest(
        request_id="terms-preview-history",
        stage="terms",
        owner_kind="extraction_item",
        owner_id="te-preview-history",
        item_ids=("te-preview-history",),
        input_hashes={"te-preview-history": "input-hash"},
        wire_hash="wire-hash",
    )
    state.write(root.store._path("requests", terms.request_id), canonical_json_bytes(terms))
    before = _tree(root.store.root)

    preview = plan_resume(root.store.root)

    assert preview.phase == "translation"
    assert preview.status == "ready" and preview.actions == ("translate",)
    assert parent.unit_id in preview.pending_unit_ids
    assert before == _tree(root.store.root)


def test_preview_accepts_plan_before_child_initialization_without_writing(tmp_path: Path) -> None:
    session, _parent = _case(tmp_path)
    root = BodyJournal(session.store, session)
    plans = ensure_part_plans(root)
    assert plans and not list(state.glob(root.store.root / "parts", "*/results/*.json"))
    before = _tree(root.store.root)

    preview = plan_resume(root.store.root)

    assert preview.phase == "translation"
    assert preview.status == "ready" and preview.actions == ("translate",)
    assert before == _tree(root.store.root)
    initialized = PartJournal.initialize(root, plans[0])
    assert initialized.records()


def test_preview_rebuilds_mixed_epoch_part_review_before_and_after_recovery(tmp_path: Path) -> None:
    root, part, parent = _parts_case(tmp_path, complete=False)
    batch = part.pending_batches()[0]
    fresh = {item.item_id for item in batch.items[:3]}
    retained = batch.items[3].item_id

    async def first_pass(kind, payload):
        if kind == "translate":
            items = [
                {
                    "item_id": item["item_id"],
                    "target": item["source"].replace("Entry", "条目").replace("word", "文字"),
                }
                for item in payload["items"]
                if item["item_id"] == retained
            ]
            protocol = "epubox-text-1"
        else:
            decision = review_item(payload["items"][0], decision="needs_attention")
            decision["issues"] = ["retry retained draft"]
            items, protocol = [decision], "epubox-review-2"
        return {"raw": json.dumps({"protocol": protocol, "request_id": payload["request_id"], "items": items})}

    async def run_first() -> None:
        with root.activate(part):
            result = await run_workflow(
                root.session.prepared,
                batch,
                part.session.index,
                root.runtime(transport=first_pass),
                session=part.session,
                save=part.save,
                records=part.records(batch.manifest.item_ids),
            )
        assert result.status == "needs_attention"

    asyncio.run(run_first())
    assert set(part.retry_units((parent.unit_id,))) == set(batch.manifest.item_ids)

    class StopAfterReviewResponse(BaseException):
        pass

    replacement = ""
    replaced = batch.items[0].item_id

    async def second_pass(kind, payload):
        nonlocal replacement
        if kind == "translate":
            items = [
                {
                    "item_id": item["item_id"],
                    "target": item["source"].replace("Entry", "条目").replace("word", "文字"),
                }
                for item in payload["items"]
                if item["item_id"] in fresh
            ]
            protocol = "epubox-text-1"
        else:
            items = []
            for item in payload["items"]:
                if item["item_id"] == replaced:
                    replacement = item["target"].replace("文字", "文本", 1)
                    items.append(review_item(item, decision="replace", target=replacement))
                else:
                    items.append(review_item(item, decision="no_change"))
            protocol = "epubox-review-2"
        return {"raw": json.dumps({"protocol": protocol, "request_id": payload["request_id"], "items": items})}

    def interrupted_save(_record) -> None:
        raise AssertionError("group save should be used")

    def interrupted_save_many(records) -> None:
        if any(record.status.value == "reviewed" for record in records):
            raise StopAfterReviewResponse
        part.save_many(records)

    interrupted_save.save_many = interrupted_save_many  # type: ignore[attr-defined]

    async def run_second() -> None:
        with root.activate(part), pytest.raises(StopAfterReviewResponse):
            await run_workflow(
                root.session.prepared,
                batch,
                part.session.index,
                root.runtime(transport=second_pass),
                session=part.session,
                save=interrupted_save,
                records=part.records(batch.manifest.item_ids),
            )

    asyncio.run(run_second())
    before = _tree(root.store.root)
    awaiting = plan_resume(root.store.root)
    assert awaiting.status == "ready" and not awaiting.reasons
    assert before == _tree(root.store.root)

    reopened = BodyJournal(root.store).parts(parent.item_id)
    reopened.recover_results()
    recovered = reopened.records((replaced,))[replaced]
    assert recovered.status.value == "reviewed" and recovered.target_projection == replacement
    before = _tree(root.store.root)
    reviewed = plan_resume(root.store.root)
    assert reviewed.status == "ready" and not reviewed.reasons
    assert before == _tree(root.store.root)


@pytest.mark.parametrize("damage", ["child", "input", "proof"])
def test_preview_rejects_corrupt_part_child_or_response_proof(tmp_path: Path, damage: str) -> None:
    root, part, _parent = _parts_case(tmp_path, complete=True)
    child = next(iter(part.records().values()))
    if damage == "child":
        state.write(
            part.session.result_path(child.item_id),
            canonical_json_bytes(child.model_copy(update={"target_hash": "0" * 64})),
        )
    else:
        review_id = child.checks["review_request_id"]
        assert isinstance(review_id, str)
        request = root.store.read_request(review_id)
        if damage == "input":
            state.write(
                root.store._path("requests", review_id),
                canonical_json_bytes(
                    request.model_copy(update={"input_hashes": request.input_hashes | {child.item_id: "changed"}})
                ),
            )
        else:
            attempt = request.attempts[-1]
            state.unlink(root.store.root / "responses" / "review" / review_id / f"{attempt.attempt_id}.json")
    before = _tree(root.store.root)

    preview = plan_resume(root.store.root)

    assert preview.status == "needs_attention"
    assert preview.actions == ("repair_shared_identity",)
    assert before == _tree(root.store.root)


def test_preview_accepts_completed_adaptive_review_parts_without_retranslation(tmp_path: Path) -> None:
    case = prepare_case(
        tmp_path,
        "<p>First sentence. Second sentence! Third sentence?</p>",
        ("First sentence. Second sentence! Third sentence?",),
    )
    parent = case.batch.items[0]

    async def transport(stage, payload):
        response = answer(stage, payload)
        body = json.loads(response["raw"])
        item_ids = {item["item_id"] for item in payload["items"]}
        if stage == "translate" and parent.item_id in item_ids:
            next(item for item in body["items"] if item["item_id"] == parent.item_id)["target"] = (
                "第一句。第二句！第三句？"
            )
        if stage == "review" and parent.item_id in item_ids:
            decision = next(item for item in body["items"] if item["item_id"] == parent.item_id)
            decision.update(
                decision="needs_attention",
                issues=[{"code": "accuracy", "severity": "major", "message": "split review"}],
            )
        response["raw"] = json.dumps(body, ensure_ascii=False)
        return response

    result = asyncio.run(
        run_translation(
            case.session.store.root,
            model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
            transport=transport,
        )
    )
    assert result.status == "translated"
    before = _tree(case.session.store.root)

    preview = plan_resume(case.session.store.root)

    assert preview.phase == "publication" and preview.status == "ready"
    assert before == _tree(case.session.store.root)


def test_preview_accepts_nested_adaptive_plan_inventory_without_writing(tmp_path: Path) -> None:
    session, parent = _adaptive_case(
        tmp_path,
        "<table><tr><td>First sentence. Second sentence.</td><td>Other cell.</td></tr></table>",
        "table",
    )
    root = BodyJournal(session.store, session)
    top_plan = build_plan(session, parent, trigger_stage="translate", trigger_count=10)
    assert top_plan is not None and top_plan.strategy == "table_cells"
    state.write(plan_path(session.store.root, parent.item_id), canonical_json_bytes(top_plan))
    top = PartJournal.initialize(root, top_plan)
    stubborn = next(child for child in top_plan.children if "First sentence" in child.source_projection)
    nested_plan = build_plan(
        top.session,
        stubborn,
        top.records((stubborn.item_id,))[stubborn.item_id],
        trigger_stage="translate",
        trigger_count=10,
    )
    assert nested_plan is not None and nested_plan.depth == 2
    state.write(plan_path(session.store.root, stubborn.item_id), canonical_json_bytes(nested_plan))
    PartJournal.initialize(top, nested_plan)
    before = _tree(session.store.root)

    preview = plan_resume(session.store.root)

    assert preview.phase == "translation" and preview.status == "ready"
    assert not preview.reasons
    assert before == _tree(session.store.root)

    async def transport(stage, payload):
        return answer(stage, payload)

    finished = asyncio.run(
        run_translation(
            session.store.root,
            model=SimpleNamespace(id=session.prepared.plan.translation_config["model"]),
            transport=transport,
            ready_session=session,
        )
    )
    assert finished.status == "translated", finished.reason
    complete = plan_resume(session.store.root)
    assert complete.phase == "publication" and complete.status == "ready", complete
