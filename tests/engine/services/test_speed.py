from __future__ import annotations

import asyncio
import json

import pytest

from engine.agents import wire, workflow
from engine.agents.workflow import run_workflow
from engine.execution.atomic import _pending_batches
from engine.schemas.contracts import ItemStatus, canonical_json_bytes
from engine.services import journal as journal_module
from engine.services import state
from engine.services.atomic import CorruptRecord, IdentityMismatch
from engine.services.journal import BodyJournal
from engine.services.ready import ReadySession
from engine.services.store import RunStore
from tests.engine.agents.test_workflow import prepare_case
from tests.engine.services.test_journal import _answer


def _attention_case(tmp_path):
    case = prepare_case(tmp_path / "legacy", "<p>First.</p><p>Second.</p>", ("First.", "Second."))
    source = case.session.store.root / "source.epub"
    root = tmp_path / "book"
    preparation = case.prepared.preparation
    state.initialize(root, source, preparation.source_hash, preparation.run_id)
    state.import_records(root, case.session.store.root)
    journal = BodyJournal(RunStore(root))

    async def missing(_stage, payload):
        return {"raw": json.dumps({"protocol": "epubox-text-1", "request_id": payload["request_id"], "items": []})}

    result = asyncio.run(
        run_workflow(
            journal.session.prepared,
            case.batch,
            journal.session.index,
            journal.runtime(transport=missing),
            session=journal.session,
            save=journal.save,
            records=journal.records(case.batch.manifest.item_ids),
        )
    )
    assert result.status == "needs_attention"
    return journal, tuple(dict.fromkeys(item.unit_id for item in case.batch.items))


def test_saved_initial_translation_frame_reuses_ready_verification(tmp_path, monkeypatch) -> None:
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p>", ("First.", "Second."))
    journal = BodyJournal(case.session.store, case.session)

    async def translated(_stage, payload):
        items = [{"item_id": item["item_id"], "target": "译文。"} for item in payload["items"]]
        return {"raw": json.dumps({"protocol": "epubox-text-1", "request_id": payload["request_id"], "items": items})}

    asyncio.run(
        run_workflow(
            case.prepared,
            case.batch,
            case.index,
            journal.runtime(transport=translated),
            session=journal.session,
            save=journal.save,
            records=journal.records(case.batch.manifest.item_ids),
        )
    )
    calls = 0
    original = workflow.pack_members

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(workflow, "pack_members", counted)
    BodyJournal(case.session.store)
    assert calls == 0


def test_initial_translation_rebinds_coordinated_physical_wire_metadata(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store, case.session)

    async def accepted(kind, payload):
        return _answer(kind, payload)

    assert (
        asyncio.run(
            run_workflow(
                case.prepared,
                case.batch,
                case.index,
                journal.runtime(transport=accepted),
                session=journal.session,
                save=journal.save,
                records=journal.records(case.batch.manifest.item_ids),
            )
        ).status
        == "completed"
    )
    request = journal._requests[case.batch.manifest.request_id]
    attempt = next(value for value in request.attempts if value.state == "succeeded")
    forged_hash = "0" * 64
    attempts = tuple(
        value.model_copy(
            update={"metadata": value.metadata | {"wire_version": wire.VERSION, "wire_hash": forged_hash}}
        )
        if value.attempt_id == attempt.attempt_id
        else value
        for value in request.attempts
    )
    state.write(
        journal.store._path("requests", request.request_id),
        canonical_json_bytes(request.model_copy(update={"attempts": attempts})),
    )
    response_path = journal.store._model_response_path("translate", request.request_id, attempt.attempt_id)
    response = json.loads(state.read(response_path))
    response["wire_hash"] = forged_hash
    state.write(response_path, canonical_json_bytes(response))

    with pytest.raises((IdentityMismatch, ValueError), match="physical wire"):
        BodyJournal(case.session.store)


def test_cold_proofs_prepare_each_shared_request_once(tmp_path, monkeypatch) -> None:
    from engine.item import budget as budget_module
    from engine.services import proofs
    from tests.engine.services.test_journal import _compact_case

    case = _compact_case(tmp_path)
    journal = BodyJournal(case.session.store, case.session)

    async def accepted(kind, payload):
        return _answer(kind, payload)

    result = asyncio.run(
        run_workflow(
            case.prepared,
            case.batch,
            case.index,
            journal.runtime(transport=accepted),
            session=journal.session,
            save=journal.save,
            records=journal.records(case.batch.manifest.item_ids),
        )
    )
    assert result.status == "completed"

    wire_calls = 0
    review_calls = 0
    original_wire_items = proofs._wire_items
    original_review = proofs.validate_review_response

    def counted_wires(*args, **kwargs):
        nonlocal wire_calls
        wire_calls += 1
        return original_wire_items(*args, **kwargs)

    def counted_review(*args, **kwargs):
        nonlocal review_calls
        review_calls += 1
        return original_review(*args, **kwargs)

    monkeypatch.setattr(proofs, "_wire_items", counted_wires)
    monkeypatch.setattr(proofs, "validate_review_response", counted_review)
    token_calls = 0
    original_count = budget_module._count

    def counted_tokens(*args, **kwargs):
        nonlocal token_calls
        token_calls += 1
        return original_count(*args, **kwargs)

    monkeypatch.setattr(budget_module, "_count", counted_tokens)
    resumed = BodyJournal(case.session.store)

    assert wire_calls == 1
    assert review_calls == 1
    assert resumed._proof_metrics["review_requests"] == 1
    assert resumed._proof_metrics["item_bindings"] == 2
    assert token_calls > 0


def test_cold_composites_share_one_joint_child_validation(tmp_path, monkeypatch) -> None:
    from engine.services import proofs
    from engine.services.parts import PartJournal, verify_composites
    from tests.engine.services.test_parts import _manual_composites

    root, plans, composed = _manual_composites(tmp_path)
    calls: list[int] = []
    original = proofs.validate_records

    def counted(journal, records, progress=None):
        calls.append(len(records))
        return original(journal, records, progress)

    monkeypatch.setattr(proofs, "validate_records", counted)
    monkeypatch.setattr(
        PartJournal,
        "__init__",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cold composite proof must not construct one journal per parent")
        ),
    )

    verify_composites(root, composed)

    assert calls == [sum(len(plan.children) for plan in plans)]
    assert root._proof_metrics["part_snapshots"] == len(plans)


def test_sparse_restart_reuses_saved_request_frames(tmp_path, monkeypatch) -> None:
    journal, units = _attention_case(tmp_path)
    journal.retry_units(units)
    batch = _pending_batches(journal, tuple(journal.session._prepared_batches.values()))[0]

    async def accepted(kind, payload):
        return _answer(kind, payload)

    result = asyncio.run(
        run_workflow(
            journal.session.prepared,
            batch,
            journal.session.index,
            journal.runtime(transport=accepted),
            session=journal.session,
            save=journal.save,
            records=journal.records(batch.manifest.item_ids),
        )
    )
    assert result.status == "completed" and batch.manifest.sparse

    session = ReadySession(journal.store)
    calls = 0
    original = journal_module.pack_members

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(journal_module, "pack_members", counted)
    monkeypatch.setattr(workflow, "pack_members", counted)
    resumed = BodyJournal(journal.store, session)
    assert calls == 0
    monkeypatch.setattr(
        resumed,
        "_translation_batch",
        lambda _request: (_ for _ in ()).throw(AssertionError("terminal translation rebuilt")),
    )
    monkeypatch.setattr(
        resumed,
        "_review_batch",
        lambda _request: (_ for _ in ()).throw(AssertionError("terminal review rebuilt")),
    )
    resumed.recover_results()


def test_sparse_restart_rejects_changed_request_frame(tmp_path) -> None:
    journal, units = _attention_case(tmp_path)
    journal.retry_units(units)
    batch = _pending_batches(journal, tuple(journal.session._prepared_batches.values()))[0]

    async def accepted(kind, payload):
        return _answer(kind, payload)

    assert (
        asyncio.run(
            run_workflow(
                journal.session.prepared,
                batch,
                journal.session.index,
                journal.runtime(transport=accepted),
                session=journal.session,
                save=journal.save,
                records=journal.records(batch.manifest.item_ids),
            )
        ).status
        == "completed"
    )
    frame = next(state.glob(journal.store.root / "frames", "*.json"))
    value = json.loads(state.text(frame))
    value["payload"]["items"][0]["source"] = "changed"
    state.write(frame, json.dumps(value).encode())
    session = ReadySession(journal.store)
    with pytest.raises((CorruptRecord, IdentityMismatch)):
        BodyJournal(journal.store, session)


def test_request_progress_reports_physical_wire_budget(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    events = []
    journal = BodyJournal(case.session.store, case.session)
    journal.runtime(transport=lambda *_args: None, progress=events.append)
    journal._prepare(
        "translate",
        case.batch.payload,
        case.batch.manifest.model_dump(mode="python")
        | {
            "output_tokens": case.batch.budget.output_tokens,
            "physical_budget": {"cl100k_tokens": 321, "estimated_input_tokens": 738},
        },
    )
    assert events[-1]["estimated_input_tokens"] == 321
    assert events[-1]["reserved_input_tokens"] == 738


def test_compact_retry_commits_once_and_rolls_back_memory_on_commit_failure(tmp_path, monkeypatch) -> None:
    journal, units = _attention_case(tmp_path)
    original = state._commit
    commits = 0

    def counted(*args, **kwargs):
        nonlocal commits
        commits += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(state, "_commit", counted)
    reopened = journal.retry_units(units)
    assert len(reopened) == 2 and commits == 1
    assert all(record.status == ItemStatus.PENDING for record in journal.records(reopened).values())

    rollback, rollback_units = _attention_case(tmp_path / "rollback")
    before = rollback.records()
    requests = dict(rollback._requests)
    attempts = rollback.progress_snapshot()["http_attempts"]

    def interrupted(*_args, **_kwargs):
        raise OSError("commit interrupted")

    monkeypatch.setattr(state, "_commit", interrupted)
    with pytest.raises(OSError, match="commit interrupted"):
        rollback.retry_units(rollback_units)
    assert rollback.records() == before
    assert rollback._requests == requests
    assert rollback.progress_snapshot()["http_attempts"] == attempts
    assert BodyJournal(rollback.store).records() == before
