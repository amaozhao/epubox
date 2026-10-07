from __future__ import annotations

import asyncio
import json

import pytest

from engine.agents import workflow
from engine.agents.workflow import run_workflow
from engine.schemas.contracts import ItemStatus
from engine.services import state
from engine.services.journal import BodyJournal
from engine.services.store import RunStore
from tests.engine.agents.workflow import prepare_case


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
