from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from engine.agents.workflow import run_workflow
from engine.execution.atomic import _pending_batches
from engine.schemas.contracts import canonical_json_bytes
from engine.services.journal import BodyJournal
from engine.services.resume import plan_resume
from tests.engine.agents.physical import Model
from tests.engine.agents.runtime import FakeOpenAIClient
from tests.engine.agents.workflow import prepare_case, review_item


def test_fresh_legacy_ready_batches_are_replanned_without_the_combined_context_gate(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",), output_budget_version=6)
    journal = BodyJournal(case.session.store, case.session)
    initial = tuple(case.session._prepared_batches.values())

    assert initial and all(batch.manifest.output_unlimited for batch in initial)
    scheduled = _pending_batches(journal, initial, output_unlimited=True)
    assert scheduled and all(batch.manifest.context_unlimited for batch in scheduled)
    assert {item.item_id for batch in scheduled for item in batch.items} == {
        item.item_id for batch in initial for item in batch.items
    }


def _complete(tmp_path):
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",), output_budget_version=6)
    journal = BodyJournal(case.session.store, case.session)

    class Completions:
        def __init__(self, mode):
            self.mode = mode

        async def create(self, **kwargs):
            payload = json.loads(kwargs["messages"][1]["content"])
            translate = payload["protocol"] == "epubox-text-1"
            if translate and self.mode == "translate_failure":
                items = []
            elif translate:
                items = [
                    {
                        "item_id": item["item_id"],
                        "target": {slot: "译文。" for slot in item["slot_ids"]},
                    }
                    for item in payload["items"]
                ]
            elif self.mode == "review_failure":
                items = [review_item(item, decision="needs_attention") for item in payload["items"]]
            else:
                items = [review_item(item, decision="no_change") for item in payload["items"]]
            raw = json.dumps({"protocol": payload["protocol"], "request_id": payload["request_id"], "items": items})
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=raw), finish_reason="stop")],
                usage={"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
                id="feedback-response",
                model="test-model",
                system_fingerprint=None,
            )

    def run(mode, batch=case.batch):
        runtime = journal.runtime(model=Model(FakeOpenAIClient(Completions(mode))))
        return asyncio.run(
            run_workflow(
                journal.session.prepared,
                batch,
                journal.session.index,
                runtime,
                session=journal.session,
                save=journal.save,
                records=journal.records(batch.manifest.item_ids),
            )
        )

    assert run("translate_failure").status == "needs_attention"
    journal.retry_units((case.batch.items[0].unit_id,))
    retry = next(
        batch
        for batch in _pending_batches(journal, tuple(case.session._prepared_batches.values()))
        if case.batch.items[0].item_id in batch.manifest.item_ids
    )

    assert run("review_failure", retry).status == "needs_attention"
    journal.retry_units((case.batch.items[0].unit_id,))
    retry = next(
        batch
        for batch in _pending_batches(journal, tuple(case.session._prepared_batches.values()))
        if case.batch.items[0].item_id in batch.manifest.item_ids
    )

    completed = run("healthy", retry)
    assert completed.status == "completed", completed.issues
    return case, journal


@pytest.mark.parametrize("stage", ["translate", "review"])
def test_feedback_requests_survive_cold_resume_and_reject_tampering(tmp_path, stage) -> None:
    case, journal = _complete(tmp_path)
    requests = [
        request for request in journal._requests.values() if request.stage == stage and request.feedback_by_item
    ]
    assert len(requests) == 1
    assert any("wire_version" in attempt.metadata for attempt in requests[0].attempts)
    assert not plan_resume(case.session.store.root).reasons
    BodyJournal(case.session.store)

    request = requests[0]
    item_id = request.item_ids[0]
    forged = request.model_copy(update={"feedback_by_item": {item_id: "forged diagnostic"}})
    path = case.session.store.root / "requests" / f"{request.request_id}.json"
    case.session.store._base.atomic_write_bytes(path, canonical_json_bytes(forged))
    before = {
        path.relative_to(case.session.store.root): path.read_bytes()
        for path in case.session.store.root.rglob("*")
        if path.is_file()
    }

    preview = plan_resume(case.session.store.root)

    assert preview.status == "needs_attention"
    assert preview.actions == ("repair_shared_identity",)
    assert {
        path.relative_to(case.session.store.root): path.read_bytes()
        for path in case.session.store.root.rglob("*")
        if path.is_file()
    } == before
