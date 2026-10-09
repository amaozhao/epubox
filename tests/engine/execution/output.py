from __future__ import annotations

import json

import pytest

import engine.execution.request as request_module
from engine.orchestrator import TranslationEngine
from tests.engine.execution.support import PartialBatchTransport, ready_batch_store


@pytest.mark.asyncio
async def test_legacy_dispatch_treats_output_estimate_as_diagnostic_only(tmp_path, monkeypatch) -> None:
    store = await ready_batch_store(tmp_path, "unlimited-output")
    transport = PartialBatchTransport()
    transport.omitted = "disabled"
    engine = TranslationEngine(store, transport=transport)
    jobs = tuple(engine._ready_jobs()[:2])
    observed: list[int | None] = []
    original = request_module.wire_hash

    def capture(stage, payload, output_tokens):
        observed.append(output_tokens)
        return original(stage, payload, output_tokens)

    monkeypatch.setattr(request_module, "recommended_output_tokens", lambda *args, **kwargs: 1_000_000)
    monkeypatch.setattr(request_module, "wire_hash", capture)

    await engine._run_batch(jobs)

    calls = [ids for stage, ids in transport.calls if stage == "translate"]
    manifest = store.read_request(next((store.root / "requests").glob("*.json")).stem)
    assert calls == [tuple(job.item_id for job in jobs)]
    assert manifest.output_unlimited is True
    assert observed == [None]
    assert manifest.attempts[0].reservation["estimated_output_tokens"] == 1_000_000
    assert "output_tokens" not in manifest.attempts[0].reservation


@pytest.mark.asyncio
async def test_truncated_batch_retries_every_item_without_accepting_partial_response(tmp_path) -> None:
    store = await ready_batch_store(tmp_path, "truncated-partial")
    base = PartialBatchTransport()
    base.omitted = "disabled"
    first_ids: tuple[str, ...] = ()
    partial_id = ""
    truncated = False

    async def transport(stage, payload):
        nonlocal first_ids, partial_id, truncated
        response = await base(stage, payload)
        if stage == "translate" and not truncated:
            truncated = True
            first_ids = tuple(item["item_id"] for item in payload["items"])
            partial_id = first_ids[0]
            raw = response["raw"]
            assert isinstance(raw, str)
            body = json.loads(raw)
            body["items"] = body["items"][:1]
            return dict(response) | {"raw": json.dumps(body), "finish_reason": "length"}
        return response

    result = await TranslationEngine(store, transport=transport).run()
    calls = [ids for stage, ids in base.calls if stage == "translate"]

    assert result.status == "translated"
    assert len(first_ids) > 1
    assert sum(partial_id in ids for ids in calls) == 2
    assert set().union(*(set(ids) for ids in calls[1:])) == set(first_ids)
