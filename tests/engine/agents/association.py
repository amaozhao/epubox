import asyncio
import json

import pytest

from engine.agents.workflow import run_workflow
from engine.schemas.contracts import ItemStatus
from engine.services.journal import BodyJournal
from engine.services.resume import plan_resume
from tests.engine.agents.workflow import prepare_case
from tests.engine.execution.atomic import answer


@pytest.mark.parametrize("stage", ("translate", "review"))
def test_one_wrong_id_keeps_valid_siblings_and_durable_proofs(tmp_path, stage):
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p><p>Third.</p>", ("First.", "Second.", "Third."))
    journal = BodyJournal(case.session.store, case.session)
    wrong = case.batch.items[0].item_id

    async def transport(kind, payload):
        response = answer(kind, payload)
        if kind == stage:
            value = json.loads(response["raw"])
            value["items"][0]["item_id"] = "wrong-id"
            response["raw"] = json.dumps(value)
        return response

    result = asyncio.run(
        run_workflow(
            case.prepared,
            case.batch,
            case.index,
            journal.runtime(transport=transport),
            session=case.session,
            save=journal.save,
            records=journal.records(case.batch.manifest.item_ids),
        )
    )
    assert result.status == "needs_attention"
    assert result.results[wrong].status == ItemStatus.NEEDS_ATTENTION
    assert all(result.results[item.item_id].status == ItemStatus.REVIEWED for item in case.batch.items[1:])
    restored = BodyJournal(case.session.store)
    assert all(restored.records()[item.item_id].status == ItemStatus.REVIEWED for item in case.batch.items[1:])
    restored.recover_results()
    assert not plan_resume(case.session.store.root).reasons


def test_batch_error_is_not_recursively_prefixed_by_other_items(tmp_path):
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p><p>Third.</p>", ("First.", "Second.", "Third."))
    journal = BodyJournal(case.session.store, case.session)

    async def transport(_kind, _payload):
        return {"raw": "not JSON"}

    result = asyncio.run(
        run_workflow(
            case.prepared,
            case.batch,
            case.index,
            journal.runtime(transport=transport),
            session=case.session,
            save=journal.save,
        )
    )
    messages = []
    for record in result.results.values():
        assert record.failure is not None
        message = record.failure["message"]
        assert isinstance(message, str)
        messages.append(message)
    assert len(set(messages)) == 1
    assert all(message.count(case.batch.manifest.request_id) == 1 for message in messages)


@pytest.mark.parametrize("stage", ("translate", "review"))
def test_empty_span_duplicate_close_is_normalized_and_proven_on_resume(tmp_path, stage):
    projection = "First.⟦+g1⟧⟦-g1⟧ Tail."
    case = prepare_case(tmp_path, "<p>First.<span></span> Tail.</p>", (projection,))
    journal = BodyJournal(case.session.store, case.session)

    async def transport(kind, payload):
        response = answer(kind, payload)
        if kind == stage:
            value = json.loads(response["raw"])
            if kind == "translate":
                value["items"][0]["target"] += "⟦-g1⟧"
            else:
                value["items"][0].update(decision="replace", target=payload["items"][0]["target"] + "⟦-g1⟧")
            response["raw"] = json.dumps(value)
        return response

    result = asyncio.run(
        run_workflow(
            case.prepared,
            case.batch,
            case.index,
            journal.runtime(transport=transport),
            session=case.session,
            save=journal.save,
        )
    )
    assert result.status == "completed"
    item = case.batch.items[0].item_id
    target = result.results[item].target_projection
    assert isinstance(target, str) and target.count("⟦-g1⟧") == 1
    restored = BodyJournal(case.session.store)
    assert restored.records()[item].status == ItemStatus.REVIEWED
    restored.recover_results()
    assert not plan_resume(case.session.store.root).reasons
