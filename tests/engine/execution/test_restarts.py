"""An explicit command restart differs from another automatic round."""

import asyncio
import json
from collections import Counter
from types import SimpleNamespace

import pytest

from engine.orchestrator import run_translation
from engine.services.failures import failure_count
from engine.services.journal import BodyJournal
from engine.services.retry import claim_restart, restart_command, restart_scope
from tests.engine.agents.test_workflow import prepare_case
from tests.engine.execution.test_atomic import answer
from tests.engine.execution.test_support import AlwaysTruncatedTransport, ScriptedTransport, ready_store


def test_restart_scope_is_shared_by_nested_entrypoints_and_isolated_between_commands(tmp_path):
    @restart_command
    def entry():
        return claim_restart(tmp_path)

    with restart_scope():
        assert entry()
        assert not entry()
        assert claim_restart(tmp_path, "part-a")
        assert not claim_restart(tmp_path, "part-a")
        assert claim_restart(tmp_path / "another-book")
    assert entry() and entry()


def test_explicit_resume_targets_do_not_grant_unrelated_failures(tmp_path):
    @restart_command
    def entry(**_options):
        return claim_restart(tmp_path)

    with restart_scope():
        assert not entry(retry_units=("selected-unit",))
        assert not entry(retry_checks=("selected-check",))
        assert not entry(repair_file=tmp_path / "repair.json")
        assert entry()
        assert not entry()


@pytest.mark.asyncio
async def test_new_command_can_continue_an_exhausted_legacy_checkpoint(tmp_path):
    store, _unit, _ = ready_store(tmp_path)
    with restart_scope():
        assert (await run_translation(store.root, transport=AlwaysTruncatedTransport())).status == "needs_attention"
        idle = ScriptedTransport()
        assert (await run_translation(store.root, transport=idle)).status == "needs_attention"
        assert not idle.calls
    with restart_scope():
        resumed = ScriptedTransport()
        assert (await run_translation(store.root, transport=resumed)).status == "translated"
        assert resumed.calls


@pytest.mark.parametrize("failed_stage", ["translate", "review"])
def test_new_command_retries_exhausted_items_separately_without_resetting_history(tmp_path, failed_stage):
    case = prepare_case(
        tmp_path,
        "<p>First phrase</p><p>Second phrase</p><p>Good phrase</p>",
        ("First phrase", "Second phrase", "Good phrase"),
    )
    failed_ids = {member.item_id for member in case.batch.items[:2]}
    calls = []
    fail = True

    async def transport(stage, payload):
        ids = tuple(item["item_id"] for item in payload["items"])
        calls.append((stage, ids))
        response = answer(stage, payload)
        if fail and stage == failed_stage:
            body = json.loads(response["raw"])
            for item in body["items"]:
                if item["item_id"] in failed_ids:
                    if failed_stage == "translate":
                        item["target"] = ""
                    else:
                        item["decision"] = "needs_attention"
            response["raw"] = json.dumps(body)
        return response

    def run():
        return asyncio.run(
            run_translation(
                case.session.store.root,
                model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
                transport=transport,
                reopen_attention=True,
            )
        )

    exhausted = run()
    assert exhausted.status == "needs_attention"
    journal = BodyJournal(case.session.store)
    assert all(failure_count(journal, journal.records()[i], failed_stage) == 10 for i in failed_ids)
    from engine.services.failures import _read

    for item_id in failed_ids:
        entry = next(value for value in _read(journal, item_id).entries if value.stage == failed_stage)
        assert len(entry.merged_events) == 3
        assert len(entry.singleton_events) == 7
    before = len(calls)
    assert run().status == "needs_attention" and len(calls) == before

    # An explicit new command grants one attempt despite the old generation cap.
    with restart_scope():
        assert run().status == "needs_attention"
        retried = calls[before:]
        assert {i for stage, ids in retried if stage == failed_stage for i in ids} == failed_ids
        assert all(stage == failed_stage for stage, _ids in retried)
        assert all(len(ids) == 1 for _stage, ids in retried)
        assert len(retried) == 2
        after = len(calls)
        assert run().status == "needs_attention" and len(calls) == after

    journal = BodyJournal(case.session.store)
    assert all(failure_count(journal, journal.records()[i], failed_stage) == 11 for i in failed_ids)
    fail = False
    with restart_scope():
        assert run().status == "translated"
    assert all(set(ids).issubset(failed_ids) and len(ids) == 1 for _stage, ids in calls[before:])
    before = len(calls)
    with restart_scope():
        assert run().status == "translated" and len(calls) == before


@pytest.mark.parametrize("failed_stage", ["translate", "review"])
def test_automatic_retries_keep_failed_items_separate_through_later_rounds(tmp_path, failed_stage):
    case = prepare_case(
        tmp_path,
        "<p>First phrase</p><p>Second phrase</p><p>Good phrase</p>",
        ("First phrase", "Second phrase", "Good phrase"),
    )
    failed_ids = {member.item_id for member in case.batch.items[:2]}
    counts = Counter()
    batches = []

    async def transport(stage, payload):
        response = answer(stage, payload)
        if stage == failed_stage:
            ids = tuple(item["item_id"] for item in payload["items"])
            if failed_ids.intersection(ids):
                batches.append(ids)
            body = json.loads(response["raw"])
            for item in body["items"]:
                counts[item["item_id"]] += 1
                if item["item_id"] in failed_ids and len(ids) > 1:
                    if failed_stage == "translate":
                        item["target"] = ""
                    else:
                        item["decision"] = "needs_attention"
            response["raw"] = json.dumps(body)
        return response

    with restart_scope():
        result = asyncio.run(
            run_translation(
                case.session.store.root,
                model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
                transport=transport,
                reopen_attention=True,
            )
        )
    assert result.status == "translated", result.reason
    assert failed_ids.issubset(batches[0])
    assert [len(ids) for ids in batches[:3]] == [3, 2, 2]
    assert len(batches) == 5 and all(len(ids) == 1 for ids in batches[3:])
    assert counts[case.batch.items[2].item_id] == 1
    from engine.services.failures import _read

    journal = BodyJournal(case.session.store)
    for item_id in failed_ids:
        entry = next(value for value in _read(journal, item_id).entries if value.stage == failed_stage)
        assert len(entry.merged_events) == 3
        assert entry.singleton_events == ()
