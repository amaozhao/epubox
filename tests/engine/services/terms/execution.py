from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest

from engine.agents.runtime import ProviderError, model_input_budget
from engine.schemas.contracts import TermExtractionRecord
from engine.services.atomic import IdentityMismatch
from engine.services.terms.runner import TermRunner
from tests.engine.services.terms.runner import _prepare


def _response(payload: dict) -> dict:
    item_id = payload["items"][0]["item_id"]
    return {
        "raw": json.dumps(
            {
                "protocol": "epubox-terms-1",
                "request_id": payload["request_id"],
                "items": [{"item_id": item_id, "candidates": []}],
            }
        ),
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _one_item_batches(monkeypatch) -> None:
    def measured(kind, payload, *, algorithm_version=1):
        return model_input_budget(kind, payload, algorithm_version=algorithm_version) | {
            "estimated_input_tokens": len(payload["items"]) * 1_000
        }

    monkeypatch.setattr("engine.services.terms.runner.model_input_budget", measured)


def test_concurrency_refills_before_the_slow_batch_finishes(tmp_path: Path, monkeypatch) -> None:
    store, item_ids = _prepare(tmp_path, concurrency=2, max_input_tokens=1_500)
    _one_item_batches(monkeypatch)
    active: set[str] = set()
    started: list[str] = []
    third_started = asyncio.Event()
    max_active = 0

    async def transport(_kind, payload):
        nonlocal max_active
        item_id = payload["items"][0]["item_id"]
        assert item_id not in active
        active.add(item_id)
        started.append(item_id)
        max_active = max(max_active, len(active))
        try:
            if item_id == item_ids[0]:
                await third_started.wait()
            elif item_id == item_ids[2]:
                assert item_ids[0] in active
                third_started.set()
            return _response(payload)
        finally:
            active.remove(item_id)

    async def exercise():
        return await asyncio.wait_for(TermRunner(store, transport=transport).run(), timeout=2)

    result = asyncio.run(exercise())

    assert result.status == "closed"
    assert started[:3] == list(item_ids[:3])
    assert max_active == 2
    assert len(started) == len(set(started)) == len(item_ids)


def test_pause_drains_paid_response_and_resume_skips_it(tmp_path: Path, monkeypatch) -> None:
    store, item_ids = _prepare(tmp_path, concurrency=2, max_input_tokens=1_500)
    _one_item_batches(monkeypatch)
    pause_started = asyncio.Event()
    calls: Counter[str] = Counter()

    async def first_transport(_kind, payload):
        item_id = payload["items"][0]["item_id"]
        calls[item_id] += 1
        if item_id == item_ids[0]:
            await pause_started.wait()
        elif item_id == item_ids[1]:
            pause_started.set()
            raise ProviderError("stop", status_code=401)
        return _response(payload)

    paused = asyncio.run(TermRunner(store, transport=first_transport).run())

    assert paused.status == "paused" and paused.reason == "stop"
    assert calls == Counter({item_ids[0]: 1, item_ids[1]: 1})
    assert store.read_extraction(item_ids[0]).status == "succeeded"
    assert store.read_extraction(item_ids[1]).status == "pending"

    async def resumed_transport(_kind, payload):
        calls[payload["items"][0]["item_id"]] += 1
        return _response(payload)

    resumed = asyncio.run(TermRunner(store, transport=resumed_transport).run())

    assert resumed.status == "closed"
    assert calls[item_ids[0]] == 1
    assert calls[item_ids[1]] == 2
    assert all(calls[item_id] == 1 for item_id in item_ids[2:])


def test_cancel_keeps_completed_journal_and_unknown_attempt_for_resume(tmp_path: Path, monkeypatch) -> None:
    store, item_ids = _prepare(tmp_path, concurrency=2, max_input_tokens=1_500)
    _one_item_batches(monkeypatch)
    items = {item.item_id: item for item in store.read_term_plan().items}
    for item_id in item_ids[2:]:
        item = items[item_id]
        store.save_extraction(
            TermExtractionRecord(
                item_id=item_id,
                document_id=item.document_id,
                view_ids=item.view_ids,
                extraction_input_hash=item.extraction_input_hash,
                status="succeeded",
            )
        )
    second_started = asyncio.Event()
    calls: Counter[str] = Counter()

    async def first_transport(_kind, payload):
        item_id = payload["items"][0]["item_id"]
        calls[item_id] += 1
        if item_id == item_ids[0]:
            await second_started.wait()
        else:
            second_started.set()
            await asyncio.Event().wait()
        return _response(payload)

    runner = TermRunner(store, transport=first_transport)
    accept = runner._accept_response

    def crash(item, *args, **kwargs):
        if item.item_id == item_ids[0]:
            raise RuntimeError("crash while merging completed response")
        return accept(item, *args, **kwargs)

    monkeypatch.setattr(runner, "_accept_response", crash)
    with pytest.raises(RuntimeError, match="merging completed response"):
        asyncio.run(runner.run())

    first = store.read_request(store.read_extraction(item_ids[0]).request_ids[-1]).attempts[-1]
    cancelled = store.read_request(store.read_extraction(item_ids[1]).request_ids[-1]).attempts[-1]
    assert first.state == "succeeded"
    assert cancelled.state == "unknown"

    async def resumed_transport(_kind, payload):
        calls[payload["items"][0]["item_id"]] += 1
        return _response(payload)

    resumed = asyncio.run(TermRunner(store, transport=resumed_transport).run())

    assert resumed.status == "closed"
    assert calls[item_ids[0]] == 1
    assert calls[item_ids[1]] == 2


@pytest.mark.parametrize("damage", ("missing", "stale"))
def test_receipt_guard_stops_fresh_http(tmp_path: Path, damage: str) -> None:
    store, _ = _prepare(tmp_path)
    if damage == "missing":
        (store.root / "checks" / "preflight.json").unlink()
    else:
        source = store.root / "source.epub"
        source.chmod(0o600)
        source.write_bytes(source.read_bytes() + b"stale")
    calls = 0

    async def forbidden(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("invalid preflight must stop before HTTP")

    if damage == "stale":
        with pytest.raises(IdentityMismatch, match="source snapshot"):
            TermRunner(store, transport=forbidden)
        assert calls == 0
        return

    result = asyncio.run(TermRunner(store, transport=forbidden).run())

    assert result.status == "paused"
    assert "atomic preflight is required" in (result.reason or "")
    assert calls == result.http_attempts == 0


def test_local_response_replays_without_a_receipt_or_new_http(tmp_path: Path, monkeypatch) -> None:
    store, item_ids = _prepare(tmp_path)
    items = {item.item_id: item for item in store.read_term_plan().items}
    for item_id in item_ids[1:]:
        item = items[item_id]
        store.save_extraction(
            TermExtractionRecord(
                item_id=item_id,
                document_id=item.document_id,
                view_ids=item.view_ids,
                extraction_input_hash=item.extraction_input_hash,
                status="succeeded",
            )
        )
    calls = 0

    async def transport(_kind, payload):
        nonlocal calls
        calls += 1
        return _response(payload)

    runner = TermRunner(store, transport=transport)
    monkeypatch.setattr(
        runner,
        "_accept_response",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("crash after journal")),
    )
    with pytest.raises(RuntimeError, match="crash after journal"):
        asyncio.run(runner.run())
    (store.root / "checks" / "preflight.json").unlink()

    async def forbidden(*_args):
        raise AssertionError("local replay must not call HTTP")

    resumed = asyncio.run(TermRunner(store, transport=forbidden).run())

    assert resumed.status == "closed"
    assert store.read_extraction(item_ids[0]).status == "succeeded"
    assert calls == resumed.http_attempts == 1


def test_spent_counters_do_not_rescan_request_records(tmp_path: Path, monkeypatch) -> None:
    store, item_ids = _prepare(tmp_path)
    runner = TermRunner(store, transport=lambda *_: None)
    reads = 0
    original = store.read_request

    def counted(request_id: str):
        nonlocal reads
        reads += 1
        return original(request_id)

    monkeypatch.setattr(store, "read_request", counted)

    for _ in range(100):
        assert runner._spent() == 0
        assert runner._spent(item_ids[0]) == 0
        assert runner._logical_calls(item_ids[0]) == 0

    assert reads == 0
