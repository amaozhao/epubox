import asyncio
from types import SimpleNamespace

from engine.execution.atomic import _pending_batches, run_atomic
from engine.item.budget import request_source_tokens
from engine.schemas.contracts import ItemStatus
from engine.services.journal import BodyJournal
from engine.services.ready import limits_from_config
from tests.engine.agents.workflow import prepare_case
from tests.engine.execution.atomic import answer
from tests.engine.execution.scheduling import _case


def test_frozen_lower_source_cap_is_retained_when_regrouping():
    journal, initial, _records = _case()
    config = journal.session.prepared.plan.translation_config | {"max_source_tokens": 5000, "source_hard_limit": 1000}
    journal.session.prepared.plan = SimpleNamespace(derived_sources={}, translation_config=config)
    batches = _pending_batches(journal, initial, output_unlimited=True)
    assert batches
    assert all(batch.manifest.source_hard_limit == 1000 for batch in batches)
    assert all(batch.budget.source_tokens <= 1000 for batch in batches)
    assert limits_from_config(config, source_hard_limit=1000).source_ceiling == 1000


def test_old_oversized_batch_is_regrouped_without_exceeding_1500(tmp_path):
    texts = tuple(" ".join([f"word{index}"] * 400) for index in range(3))
    case = prepare_case(tmp_path, "".join(f"<p>{text}</p>" for text in texts), texts)
    assert case.batch.budget.source_tokens > 1500
    journal = BodyJournal(case.session.store, case.session)
    planned = _pending_batches(journal, tuple(case.session._prepared_batches.values()), output_unlimited=True)
    assert all(batch.budget.source_tokens <= 1500 for batch in planned)
    assert all(batch.manifest.source_hard_limit == 1500 for batch in planned)
    assert sorted(item.item_id for batch in planned for item in batch.items) == sorted(
        item.item_id for batch in case.session._prepared_batches.values() for item in batch.items
    )


def test_indivisible_oversized_paragraph_is_pending_while_other_items_continue(tmp_path):
    large = " ".join(["word"] * 1600)
    case = prepare_case(tmp_path, f"<p>{large}</p><p>Small.</p>", (large, "Small."))
    sent = []

    async def transport(stage, payload):
        assert request_source_tokens(payload, "gpt-3.5-turbo") <= 1500
        sent.extend(item["item_id"] for item in payload["items"])
        return answer(stage, payload)

    result = asyncio.run(run_atomic(case.session.store.root, transport=transport, ready_session=case.session))
    assert result.status == "needs_attention"
    assert case.batch.items[0].item_id not in sent
    assert case.batch.items[1].item_id in sent
    restored = BodyJournal(case.session.store)
    record = restored.records()[case.batch.items[0].item_id]
    assert record.status == ItemStatus.NEEDS_ATTENTION
    assert record.failure is not None and "hard limit 1500" in str(record.failure["message"])
    assert restored.records()[case.batch.items[1].item_id].status == ItemStatus.REVIEWED
    before = len(sent)
    asyncio.run(run_atomic(case.session.store.root, transport=transport, reopen_attention=True))
    assert len(sent) == before
