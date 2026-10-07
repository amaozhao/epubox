from __future__ import annotations

import asyncio
import json

import pytest

import engine.services.journal as journal_module
from engine.agents.workflow import run_workflow
from engine.schemas.contracts import ItemStatus, canonical_hash
from engine.services.atomic import IdentityMismatch
from engine.services.journal import BodyJournal
from engine.services.resume import plan_resume
from tests.engine.agents.workflow import prepare_case
from tests.engine.services.journal import _answer


@pytest.mark.parametrize("malformed", ("{}", "not json", "extra"))
def test_restart_uses_successful_retry_after_malformed_translation(tmp_path, malformed, monkeypatch) -> None:
    case = prepare_case(tmp_path, "<p>Hello world.</p>", ("Hello world.",))
    journal = BodyJournal(case.session.store, case.session)

    async def failed(kind, payload):
        assert kind == "translate"
        raw = malformed
        if raw == "extra":
            value = json.loads(_answer(kind, payload)["raw"])
            value["error"] = "provider could not complete request"
            raw = json.dumps(value)
        return {"raw": raw}

    def run(journal, transport):
        return asyncio.run(
            run_workflow(
                journal.session.prepared,
                case.batch,
                journal.session.index,
                journal.runtime(transport=transport),
                session=journal.session,
                save=journal.save,
                records=journal.records(case.batch.manifest.item_ids),
            )
        )

    assert run(journal, failed).status == "needs_attention"
    failed_request = next(request.request_id for request in journal._requests.values() if request.stage == "translate")
    journal = BodyJournal(case.session.store)
    with pytest.raises(IdentityMismatch, match="no proven saved draft"):
        journal._review_target(case.batch.items[0].item_id, canonical_hash("译文1。"), 0)
    journal.retry_units(tuple(item.unit_id for item in case.batch.items))
    calls = []

    async def succeeded(kind, payload):
        calls.append(kind)
        return _answer(kind, payload)

    assert run(journal, succeeded).status == "completed"
    assert calls == ["translate", "review"]
    # A new process must prove the later successful draft despite the older bad response.
    valid_request = journal.records()[case.batch.items[0].item_id].request_id
    validations = []
    validate = journal_module.validate_translation_response

    def counted(raw, request_id, sources):
        validations.append(request_id)
        return validate(raw, request_id, sources)

    monkeypatch.setattr(journal_module, "validate_translation_response", counted)
    resumed = BodyJournal(case.session.store)
    resumed.recover_results()
    assert all(
        record.status == ItemStatus.REVIEWED for record in resumed.records(case.batch.manifest.item_ids).values()
    )
    assert failed_request not in validations and validations.count(valid_request) == 2
    assert not plan_resume(case.session.store.root).reasons
    assert run(resumed, succeeded).status == "completed"
    assert calls == ["translate", "review"]
