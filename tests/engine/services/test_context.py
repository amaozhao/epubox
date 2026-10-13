import asyncio
from typing import Any, cast

from engine.agents.workflow import run_workflow
from engine.schemas.contracts import ItemStatus
from engine.services.journal import BodyJournal
from tests.engine.agents.test_workflow import prepare_case
from tests.engine.services.test_journal import _answer


def test_workflow_and_proof_recovery_do_not_require_cache_retention(tmp_path, monkeypatch) -> None:
    import engine.services.context as context_module

    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store, case.session)
    for name in (
        "_responses",
        "_translations",
        "_reviews",
        "_translation_batches",
        "_review_batches",
        "_review_records",
    ):
        cast(Any, getattr(journal, name)).max_bytes = 0

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

    monkeypatch.setattr(context_module, "_CACHE_BYTES", 0)
    resumed = BodyJournal(case.session.store)
    assert all(
        record.status == ItemStatus.REVIEWED for record in resumed.records(case.batch.manifest.item_ids).values()
    )


def test_unchanged_disk_proof_cache_rejects_nested_memory_result_mutation(tmp_path):
    import pytest

    from engine.services.atomic import IdentityMismatch

    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store, case.session)
    record = next(iter(journal._records.values()))
    record.checks["forged"] = "changed only in memory"
    with pytest.raises(IdentityMismatch, match="body results changed"):
        journal.verify_persisted_proofs()
