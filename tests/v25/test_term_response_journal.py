from __future__ import annotations

import asyncio
import json

import pytest

from engine.agents.runtime import TERM_PROMPT_VERSION, ModelRuntime, request_messages
from engine.schemas.contracts import Attempt, RequestManifest, TermExtractionRecord
from engine.services.atomic_store import CorruptRecord, IdentityMismatch, StaleWrite
from engine.services.term_runner import TermRunner
from tests.v25.test_store import _prepare, _write_term_plan
from tests.v25.test_term_runner import _prepare as _prepare_term_run


@pytest.mark.asyncio
async def test_terms_response_is_journaled_before_attempt_succeeds() -> None:
    events: list[str] = []

    async def transport(_kind, _payload):
        return {
            "raw": '{"items":[]}',
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "finish_reason": "length",
            "metadata": {"response_id": "response-1"},
        }

    def finish(_request_id, _attempt_id, *, state, **_fields):
        events.append(state)

    def persist(request_id: str, attempt_id: str, envelope: dict[str, object]):
        assert request_id == "request-1" and attempt_id
        assert envelope == {
            "raw": '{"items":[]}',
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            "finish_reason": "length",
            "metadata": {"response_id": "response-1", "finish_reason": "length"},
        }
        events.append("persisted")

    runtime = ModelRuntime(
        transport=transport,
        finish_attempt=finish,
        persist_response=persist,
        model_max_output_tokens=100,
    )
    await runtime.invoke(
        "terms",
        {"protocol": "epubox-terms-1", "request_id": "request-1", "items": []},
        {"request_id": "request-1", "item_ids": ["item-1"], "output_tokens": 10},
    )
    await runtime.invoke(
        "translate",
        {"protocol": "epubox-text-1", "request_id": "request-2", "items": []},
        {"request_id": "request-2", "item_ids": ["item-2"], "output_tokens": 10},
    )

    assert events == ["sent", "persisted", "succeeded", "sent", "succeeded"]


def test_term_response_journal_rejects_corruption_and_cross_attempt_replay(tmp_path) -> None:
    store, preparation = _prepare(tmp_path)
    item = _write_term_plan(store, preparation).items[0]
    store.save_extraction(
        TermExtractionRecord(
            item_id=item.item_id,
            document_id=item.document_id,
            view_ids=item.view_ids,
            extraction_input_hash=item.extraction_input_hash,
        )
    )
    request = RequestManifest(
        request_id="request-1",
        stage="terms",
        owner_kind="extraction_item",
        owner_id=item.item_id,
        item_ids=(item.item_id,),
        input_hashes={item.item_id: item.extraction_input_hash},
        wire_hash="wire-1",
    )
    store.write_request(request)
    for attempt_id in ("attempt-1", "attempt-2"):
        store.reserve_attempt(
            request.request_id,
            Attempt(attempt_id=attempt_id, affected_items=(item.item_id,), created_at="2026-09-29T00:00:00Z"),
        )

    envelope = {
        "raw": "原始回答",
        "finish_reason": "length",
        "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18, "known_cost": 0.02},
        "metadata": {"response_id": "response-1", "model": "model-1"},
    }
    store.save_term_response("request-1", "attempt-1", envelope)
    store.save_term_response("request-1", "attempt-1", envelope)
    replay = store.read_term_response("request-1", "attempt-1")
    assert replay is not None
    assert replay.raw == "原始回答" and replay.finish_reason == "length"
    assert replay.usage is not None and replay.usage.total_tokens == 18 and replay.usage.known_cost == 0.02
    assert replay.metadata == {"response_id": "response-1", "model": "model-1"}
    with pytest.raises(StaleWrite, match="immutable term response"):
        store.save_term_response("request-1", "attempt-1", envelope | {"raw": "替换回答"})

    first = store._term_response_path("request-1", "attempt-1")
    second = store._term_response_path("request-1", "attempt-2")
    second.parent.mkdir(parents=True, exist_ok=True)
    second.write_bytes(first.read_bytes())
    with pytest.raises(IdentityMismatch, match="identity"):
        store.read_term_response("request-1", "attempt-2")

    payload = json.loads(first.read_text())
    payload["response"]["raw"] = "篡改回答"
    first.write_text(json.dumps(payload, ensure_ascii=False))
    with pytest.raises(CorruptRecord, match="size or hash mismatch"):
        store.read_term_response("request-1", "attempt-1")


def test_truncated_journal_replay_preserves_usage_without_accepting_response(tmp_path, monkeypatch) -> None:
    store, item_ids = _prepare_term_run(tmp_path)

    async def transport(_kind, payload):
        item_id = payload["items"][0]["item_id"]
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": item_id, "candidates": []}],
                }
            ),
            "usage": {"input_tokens": 11, "output_tokens": 7, "known_cost": 0.02},
            "finish_reason": "length",
            "metadata": {"response_id": "truncated-response"},
        }

    real_finish = store.finish_attempt

    def interrupted_finish(*args, **kwargs):
        if kwargs["state"] == "succeeded":
            raise RuntimeError("crash before succeeded")
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(store, "finish_attempt", interrupted_finish)
    with pytest.raises(RuntimeError, match="crash before succeeded"):
        asyncio.run(TermRunner(store, transport=transport).run())

    monkeypatch.setattr(store, "finish_attempt", real_finish)
    item = next(item for item in store.read_term_plan().items if item.item_id == item_ids[0])
    record = store.read_extraction(item.item_id)
    runner = TermRunner(store, transport=transport)
    monkeypatch.setattr(
        runner,
        "_accept_response",
        lambda *_args: (_ for _ in ()).throw(AssertionError("truncated response must not be accepted")),
    )
    replayed = runner._replay_response(item, record)

    assert replayed.status == "retry_wait"
    request = store.read_request(record.request_ids[0])
    attempt = request.attempts[0]
    assert attempt.state == "succeeded"
    assert attempt.usage is not None and attempt.usage.input_tokens == 11 and attempt.usage.known_cost == 0.02
    assert attempt.metadata["response_id"] == "truncated-response"


def test_terms_prompt_requires_exact_primary_evidence_schema() -> None:
    system = request_messages("terms", {"protocol": "epubox-terms-1", "request_id": "request-1", "items": []})[0][
        "content"
    ]
    assert TERM_PROMPT_VERSION == "epubox-v25-3"
    assert "scope_hint is document or book" in system
    assert "evidence is a nonempty array" in system
    assert "exact contiguous quote from that primary view" in system
    assert "Omit a candidate when an exact primary-view citation is unavailable" in system
    assert "retry_feedback" in system
