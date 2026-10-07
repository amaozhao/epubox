from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from engine.agents.workflow import run_workflow
from engine.schemas.contracts import ItemStatus
from engine.services import state
from engine.services.journal import BodyJournal
from tests.engine.services.journal import _answer, _compact_case


def test_recovery_rolls_back_then_commits_all_response_siblings_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _compact_case(tmp_path)
    journal = BodyJournal(case.session.store, case.session)
    first_calls: list[str] = []

    def crash_before_results(_records) -> None:
        raise RuntimeError("crash before result commit")

    monkeypatch.setattr(journal, "save_many", crash_before_results)

    async def first(kind, payload):
        first_calls.append(kind)
        return _answer(kind, payload)

    with pytest.raises(RuntimeError, match="before result commit"):
        asyncio.run(
            run_workflow(
                case.prepared,
                case.batch,
                case.index,
                journal.runtime(transport=first),
                session=case.session,
                save=journal.save,
                records=journal.records(case.batch.manifest.item_ids),
            )
        )
    assert first_calls == ["translate"]

    resumed = BodyJournal(case.session.store)
    before_disk = (case.session.store.root / "state.json").read_bytes()
    before_records = resumed.records()
    before_progress = resumed.progress_snapshot()
    original_commit = state._commit

    def fail_commit(_root, _value) -> None:
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(state, "_commit", fail_commit)
    with pytest.raises(RuntimeError, match="injected commit failure"):
        resumed.recover_results()
    assert (case.session.store.root / "state.json").read_bytes() == before_disk
    assert resumed.records() == before_records
    assert resumed.progress_snapshot() == before_progress

    previous = {
        key: value["data"] for key, value in json.loads(before_disk)["records"].items() if key.startswith("results/")
    }
    commits: list[int] = []

    def count_commit(root, value) -> None:
        current = {key: record["data"] for key, record in value["records"].items() if key.startswith("results/")}
        commits.append(sum(previous.get(key) != data for key, data in current.items()))
        original_commit(root, value)

    monkeypatch.setattr(state, "_commit", count_commit)
    recovered = resumed.recover_results()
    assert commits == [len(case.batch.items)]
    assert all(recovered[item.item_id].status == ItemStatus.LOCAL_VALID for item in case.batch.items)

    replay_calls: list[str] = []

    async def review_only(kind, payload):
        replay_calls.append(kind)
        assert kind == "review"
        return _answer(kind, payload)

    monkeypatch.setattr(resumed, "save_many", crash_before_results)
    with pytest.raises(RuntimeError, match="before result commit"):
        asyncio.run(
            run_workflow(
                resumed.session.prepared,
                case.batch,
                resumed.session.index,
                resumed.runtime(transport=review_only),
                session=resumed.session,
                save=resumed.save,
                records=resumed.records(case.batch.manifest.item_ids),
            )
        )
    assert replay_calls == ["review"]

    commits.clear()
    reviewed = BodyJournal(case.session.store)
    recovered = reviewed.recover_results()
    assert commits == [len(case.batch.items)]
    assert all(recovered[item.item_id].status == ItemStatus.REVIEWED for item in case.batch.items)

    async def forbidden(*_args):
        raise AssertionError("durable responses must not trigger another transport call")

    result = asyncio.run(
        run_workflow(
            reviewed.session.prepared,
            case.batch,
            reviewed.session.index,
            reviewed.runtime(transport=forbidden),
            session=reviewed.session,
            save=reviewed.save,
            records=reviewed.records(case.batch.manifest.item_ids),
        )
    )
    assert result.status == "completed"
