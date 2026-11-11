from __future__ import annotations

import asyncio
import json

import pytest

from engine.agents.workflow import _failed, run_workflow
from engine.schemas.contracts import ItemRecord, ItemStatus, canonical_hash, canonical_json_bytes
from engine.services import state
from engine.services.atomic import IdentityMismatch, StaleWrite
from engine.services.journal import BodyJournal
from engine.services.manual import import_codex_translation
from engine.services.partition import build_plan, plan_path
from engine.services.parts import PartJournal
from engine.services.resume import plan_resume
from tests.engine.agents.test_workflow import prepare_case
from tests.engine.services.test_journal import _answer, _compact_case
from tests.engine.services.test_partition import _case


def test_manual_import_restarts_and_rejects_target_or_artifact_tampering(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store, case.session)
    member = case.batch.items[0]
    initial = journal.records((member.item_id,))[member.item_id]

    with pytest.raises(PermissionError, match="explicit user authorization"):
        import_codex_translation(journal, member, initial, "第一。", user_authorized=False)
    result = import_codex_translation(journal, member, initial, "第一。", user_authorized=True)
    assert result.status == ItemStatus.REVIEWED
    assert result.request_id is None
    assert "review_request_id" not in result.checks
    assert all(not request.attempts for request in journal._requests.values())

    restarted = BodyJournal(case.session.store)
    assert restarted.records((member.item_id,))[member.item_id] == result
    assert member.parent_item_id in restarted.parent_targets(require_complete=False)
    assert plan_resume(case.session.store.root).phase == "translation"
    result_path = restarted._result_path(member.item_id)
    pointer = result.checks["manual"]
    assert isinstance(pointer, dict)
    digest = pointer["artifact_hash"]
    assert isinstance(digest, str)
    artifact_path = restarted.store.root / "manual" / f"{digest}.json"
    original_result, original_artifact = state.read(result_path), state.read(artifact_path)

    state.write(
        result_path,
        canonical_json_bytes(
            result.model_copy(update={"target_projection": "篡改。", "target_hash": canonical_hash("篡改。")})
        ),
    )
    with pytest.raises(IdentityMismatch, match="immutable artifact"):
        BodyJournal(case.session.store)
    state.write(result_path, original_result)

    artifact = json.loads(original_artifact)
    artifact["target_projection"] = "篡改。"
    artifact["target_hash"] = canonical_hash("篡改。")
    state.write(artifact_path, canonical_json_bytes(artifact))
    with pytest.raises(IdentityMismatch, match="artifact hash changed"):
        BodyJournal(case.session.store)
    state.write(artifact_path, original_artifact)
    assert BodyJournal(case.session.store).records((member.item_id,))[member.item_id] == result


def test_manual_part_import_preserves_completed_siblings_and_composes(tmp_path) -> None:
    session, parent = _case(tmp_path)
    root = BodyJournal(session.store, session)
    plan = build_plan(session, parent, root.records((parent.item_id,))[parent.item_id])
    assert plan is not None
    state.write(plan_path(session.store.root, parent.item_id), canonical_json_bytes(plan))
    parts = PartJournal.initialize(root, plan)
    completed: dict[str, ItemRecord] = {}

    for child in plan.children:
        existing = parts.records((child.item_id,))[child.item_id]
        target = child.source_projection.replace("Entry", "条目").replace("word", "文字")
        completed[child.item_id] = import_codex_translation(parts, child, existing, target, user_authorized=True)
        assert all(parts.records((item_id,))[item_id] == record for item_id, record in completed.items())

    composed = parts.compose()
    assert composed is not None and composed.status == ItemStatus.REVIEWED
    restarted = BodyJournal(session.store)
    assert restarted.records((parent.item_id,))[parent.item_id] == composed
    assert restarted.parts(parent.item_id).records() == completed
    assert restarted.parent_targets(require_complete=False)[parent.item_id] == composed.target_projection
    assert all(not request.attempts for request in restarted._requests.values())


def test_manual_import_uses_compact_state_backend(tmp_path) -> None:
    case = _compact_case(tmp_path)
    journal = BodyJournal(case.session.store, case.session)
    member = next(item for item in case.batch.items if item.source_projection == "First.")
    existing = journal.records((member.item_id,))[member.item_id]

    result = import_codex_translation(journal, member, existing, "第一。", user_authorized=True)

    assert state.compact(case.session.store.root)
    assert BodyJournal(case.session.store).records((member.item_id,))[member.item_id] == result


def test_manual_import_explicitly_replaces_needs_attention_without_http(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store, case.session)
    member = case.batch.items[0]
    pending = journal.records((member.item_id,))[member.item_id]
    attention = _failed(member, pending, "translate", "provider unavailable", case.batch)
    journal.save(attention)

    result = import_codex_translation(journal, member, attention, "第一。", user_authorized=True)

    assert result.status == ItemStatus.REVIEWED
    assert result.checks["translation_frame"] == attention.checks["translation_frame"]
    assert "review_request_id" not in result.checks
    assert all(not request.attempts for request in journal._requests.values())
    assert BodyJournal(case.session.store).records((member.item_id,))[member.item_id] == result


def test_reviewed_correction_requires_opt_in_and_retains_provider_history(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p>", ("First.", "Second."))
    journal = BodyJournal(case.session.store, case.session)
    calls = []

    async def transport(stage, payload):
        calls.append(stage)
        return _answer(stage, payload)

    completed = asyncio.run(
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
    assert completed.status == "completed"
    member, sibling = case.batch.items
    existing = journal.records((member.item_id,))[member.item_id]
    sibling_before = journal.records((sibling.item_id,))[sibling.item_id]
    requests_before = dict(journal._requests)
    request_bytes_before = {
        path.name: state.read(path) for path in state.glob(journal.store.root / "requests", "*.json")
    }
    call_count = len(calls)
    with pytest.raises(StaleWrite, match="manual translation"):
        import_codex_translation(journal, member, existing, "修正译文。", user_authorized=True)
    with pytest.raises(PermissionError, match="explicit user authorization"):
        import_codex_translation(journal, member, existing, "修正译文。", user_authorized=False, replace_reviewed=True)

    result = import_codex_translation(
        journal, member, existing, "修正译文。", user_authorized=True, replace_reviewed=True
    )

    assert result.status == ItemStatus.REVIEWED and result.target_projection == "修正译文。"
    assert "review_request_id" not in result.checks
    pointer = result.checks["manual"]
    assert isinstance(pointer, dict)
    artifact_path = journal.store.root / "manual" / f"{pointer['artifact_hash']}.json"
    artifact = json.loads(state.read(artifact_path))
    assert artifact["replaces_reviewed"] is True
    assert artifact["prior"] == json.loads(existing.model_dump_json())
    restarted = BodyJournal(journal.store)
    assert restarted.records((member.item_id,))[member.item_id] == result
    assert restarted.records((sibling.item_id,))[sibling.item_id] == sibling_before
    assert canonical_hash(restarted._requests) == canonical_hash(requests_before)
    assert {
        path.name: state.read(path) for path in state.glob(journal.store.root / "requests", "*.json")
    } == request_bytes_before
    assert len(calls) == call_count

    from engine.schemas.manual import ManualArtifact
    from engine.services.manual import manual_proof

    unauthorized = ManualArtifact.model_validate(artifact).model_copy(update={"replaces_reviewed": False})
    forged = result.model_copy(
        update={"checks": result.checks | {"manual": {"artifact_hash": canonical_hash(unauthorized)}}}
    )
    with pytest.raises(IdentityMismatch, match="reviewed"):
        manual_proof(restarted.session, forged, artifact=unauthorized)

    second = import_codex_translation(
        restarted, member, result, "再次修正。", user_authorized=True, replace_reviewed=True
    )
    assert BodyJournal(journal.store).records((member.item_id,))[member.item_id] == second
    original_artifact = state.read(artifact_path)
    altered = dict(artifact, target_projection="篡改。", target_hash=canonical_hash("篡改。"))
    state.write(artifact_path, canonical_json_bytes(altered))
    with pytest.raises(IdentityMismatch, match="artifact hash changed"):
        BodyJournal(journal.store)
    state.write(artifact_path, original_artifact)


def test_manual_import_accepts_canonical_pending_retry_metadata(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store, case.session)
    member = case.batch.items[0]
    initial = journal.records((member.item_id,))[member.item_id]
    retried = initial.model_copy(
        update={"checks": {"translation_epoch": 2, "retry_feedback": "transport unavailable"}}
    )
    journal.save(retried)

    result = import_codex_translation(journal, member, retried, "第一。", user_authorized=True)

    assert result.checks == {
        "translation_epoch": 2,
        "manual": result.checks["manual"],
    }
    assert BodyJournal(case.session.store).records((member.item_id,))[member.item_id] == result
    assert plan_resume(case.session.store.root).phase == "translation"
