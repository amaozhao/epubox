from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import cast

import pytest

from engine.agents.workflow import run_workflow
from engine.execution.atomic import _pending_batches, run_atomic
from engine.item.members import MemberIndex, materialize_members, pack_members
from engine.schemas.contracts import ItemStatus, canonical_hash, canonical_json_bytes
from engine.services.journal import BodyJournal
from engine.services.resume import plan_resume
from tests.engine.agents.workflow import prepare_case
from tests.engine.execution.atomic import answer
from tests.engine.item.members import glossary, limits, prepared, record


@pytest.mark.parametrize("stage", ("translate", "review"))
@pytest.mark.parametrize("version", (4, 5))
def test_resume_groups_unfinished_members_across_reviewed_gap(tmp_path, stage, version):
    case = prepare_case(
        tmp_path,
        "<p>First.</p><p>Middle.</p><p>Last.</p>",
        ("First.", "Middle.", "Last."),
        output_budget_version=version,
    )
    first, middle, last = case.batch.items
    journal = BodyJournal(case.session.store, case.session)

    async def partial(kind, payload):
        response = answer(kind, payload)
        if kind == stage:
            value = json.loads(response["raw"])
            value["items"] = [item for item in value["items"] if item["item_id"] == middle.item_id]
            response["raw"] = json.dumps(value)
        return response

    asyncio.run(
        run_workflow(
            case.prepared,
            case.batch,
            case.index,
            journal.runtime(transport=partial),
            session=case.session,
            save=journal.save,
            records=journal.records(case.batch.manifest.item_ids),
        )
    )
    completed = canonical_json_bytes(journal.records()[middle.item_id])
    assert journal.records()[middle.item_id].status == ItemStatus.REVIEWED
    calls = []

    async def transport(kind, payload):
        calls.append((kind, tuple(item["item_id"] for item in payload["items"])))
        return answer(kind, payload)

    result = asyncio.run(run_atomic(case.session.store.root, transport=transport, reopen_attention=True))
    expected = (first.item_id, last.item_id)
    assert result.status == "translated", result.reason
    body_calls = [(kind, ids) for kind, ids in calls if set(ids) & {first.item_id, middle.item_id, last.item_id}]
    assert body_calls == (
        [("translate", expected), ("review", expected)] if stage == "translate" else [("review", expected)]
    )
    restarted = BodyJournal(case.session.store)
    assert canonical_json_bytes(restarted.records()[middle.item_id]) == completed
    assert len(restarted.parent_targets()) == case.prepared.plan.required_unit_count
    assert plan_resume(case.session.store.root).status == "ready"
    before = len(calls)
    assert asyncio.run(run_atomic(case.session.store.root, transport=transport)).status == "translated"
    assert len(calls) == before


def test_sparse_packing_keeps_html_boundaries_budgets_and_piece_ownership():
    body = ("word " * 850 + ". ") * 6
    first, first_report = prepared(body, cap=2000)
    second, second_report = prepared(body, path="OPS/other.xhtml", cap=2000)
    index = MemberIndex((first,), first_report)
    members = index.members
    assert len(members) >= 3
    selected = (members[0], members[-1])
    packed = pack_members("translate", selected, glossary(), index, limits(), sparse=True)
    assert len(packed.batches) == 1 and not packed.blocked
    assert packed.batches[0].manifest.sparse and packed.batches[0].context == ()
    assert packed.batches[0].items == selected
    narrow = pack_members("translate", selected, glossary(), index, limits(2000), sparse=True)
    assert len(narrow.batches) == 2 and not narrow.blocked
    with pytest.raises(ValueError, match="order"):
        pack_members("translate", selected[::-1], glossary(), index, limits(), sparse=True)
    with pytest.raises(ValueError, match="owned"):
        index.validate_items((members[0], members[-1].model_copy(update={"parent_hash": "a" * 64})), sparse=True)
    # Even the relaxed checkpoint lane must reject a different HTML resource.
    with pytest.raises(ValueError, match="owned|document"):
        index.validate_items((members[0], materialize_members((second,), second_report)[0]), sparse=True)


@pytest.mark.parametrize("stage", ("translate", "review"))
def test_sparse_schedule_keeps_mixed_retry_epochs_of_split_unit_separate(stage):
    inventory, report = prepared(("word " * 850 + ". ") * 6, cap=1000)
    index = MemberIndex((inventory,), report)
    assert len(index.members) >= 3 and len({member.unit_id for member in index.members}) == 1
    epoch_key = "review_epoch" if stage == "review" else "translation_epoch"
    records = {
        member.item_id: record(member.item_id, member.source_projection).model_copy(
            update={
                "status": ItemStatus.LOCAL_VALID if stage == "review" else ItemStatus.PENDING,
                "target_projection": member.source_projection if stage == "review" else None,
                "target_hash": canonical_hash(member.source_projection) if stage == "review" else None,
                "checks": {epoch_key: int(position == 0)},
            }
        )
        for position, member in enumerate(index.members)
    }
    config = {
        "model": "gpt-3.5-turbo",
        "max_source_tokens": 10000,
        "max_input_tokens": 50000,
        "max_output_tokens": 10000,
        "context_tokens": 60000,
    }
    session = SimpleNamespace(
        index=index,
        prepared=SimpleNamespace(
            glossary=glossary(),
            preparation=SimpleNamespace(translation_config=config),
            plan=SimpleNamespace(derived_sources={}, translation_config=config),
        ),
    )
    journal = SimpleNamespace(session=session, records=lambda: records)
    initial = pack_members("translate", index.members, glossary(), index, limits(10000)).batches
    batches = _pending_batches(journal, initial)
    assert len(batches) >= 2
    assert all(batch.budget.source_tokens <= 1500 for batch in batches)
    assert {member.item_id for batch in batches for member in batch.items} == set(records)
    for batch in batches:
        epochs = {cast(int, records[member.item_id].checks.get(epoch_key, 0)) for member in batch.items}
        assert len(epochs) == 1
        if stage == "translate":
            assert set(batch.manifest.record_versions.values()) == epochs
        else:
            revision = records[batch.items[0].item_id].checks[epoch_key]
            assert type(revision) is int
            review = pack_members(
                "review",
                batch.items,
                glossary(),
                index,
                limits(10000),
                targets={member.item_id: records[member.item_id] for member in batch.items},
                revisions={member.unit_id: revision for member in batch.items},
                sparse=True,
            )
            assert not review.blocked and len(review.batches) == 1
            assert set(review.batches[0].manifest.revisions.values()) == epochs
