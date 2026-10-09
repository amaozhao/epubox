import asyncio

import pytest

from engine.agents.runtime import ModelRuntime, ProviderError

from .pool import pooled_runtime
from .runtime import context, payload


@pytest.mark.asyncio
async def test_auth_failure_reduces_capacity_and_emits_only_safe_slot(monkeypatch):
    events: list[dict] = []

    async def unauthorized(_kind, _payload):
        raise ProviderError("secret-0 must never be logged", status_code=401)

    async def healthy(_kind, _payload):
        return {"raw": "{}"}

    runtime = pooled_runtime(monkeypatch, (unauthorized, healthy), events=events.append)
    async with runtime.workflow():
        await runtime.invoke("translate", payload("translate"), context())
        stats = runtime.workflow_stats

    assert runtime.snapshot == {
        "key_count": 2,
        "enabled_keys": 1,
        "verified_keys": 1,
        "cooling_keys": 0,
        "leased_keys": 0,
        "http_active": 0,
        "http_peak": 1,
        "workflow_capacity": 1,
    }
    assert stats["keys_used"] == ("agnes-1", "agnes-2")
    assert stats["http_attempts"] == 2
    states = [(event["key_slot"], event["state"]) for event in events if event["event"] == "key_state"]
    assert states == [("agnes-1", "disabled"), ("agnes-2", "verified")]
    assert "secret-0" not in str(events)


@pytest.mark.asyncio
async def test_workflow_timing_accumulates_across_nested_calls(monkeypatch):
    now = {"value": 0.0}

    async def sleep(seconds):
        now["value"] += seconds

    async def transport(_kind, _payload):
        now["value"] += 3
        return {"raw": "{}"}

    runtime = pooled_runtime(
        monkeypatch,
        (transport,),
        rpm=1,
        sleep=sleep,
        monotonic=lambda: now["value"],
    )
    async with runtime.workflow():
        await runtime.invoke("translate", payload("translate", "r1"), context("r1"))
        async with runtime.workflow():
            await runtime.invoke("review", payload("review", "r2"), context("r2"))
            nested = runtime.workflow_stats
        stats = runtime.workflow_stats

    assert nested == stats
    assert stats == {
        "keys_used": ("agnes-1",),
        "key_wait_seconds": 0.0,
        "rate_wait_seconds": 57.0,
        "http_seconds": 6.0,
        "http_attempts": 2,
    }
    assert runtime.workflow_stats["http_attempts"] == 0


@pytest.mark.asyncio
async def test_http_peak_tracks_real_overlap_and_returns_to_zero():
    events: list[dict] = []
    gate = asyncio.Event()
    started = 0

    async def transport(_kind, _payload):
        nonlocal started
        started += 1
        if started == 2:
            gate.set()
        await gate.wait()
        return {"raw": "{}"}

    runtime = ModelRuntime(transport=transport, max_inflight=2, events=events.append)
    await asyncio.gather(
        runtime.invoke("translate", payload("translate", "r1"), context("r1")),
        runtime.invoke("translate", payload("translate", "r2"), context("r2")),
    )

    assert runtime.snapshot["http_peak"] == 2
    assert runtime.snapshot["http_active"] == 0
    assert max(event["http_active"] for event in events if event["event"] == "http_start") == 2


@pytest.mark.asyncio
async def test_timeout_emits_cooling_notice_without_vendor_error(monkeypatch):
    events: list[dict] = []

    async def timeout(_kind, _payload):
        raise TimeoutError("secret-0 vendor details")

    async def healthy(_kind, _payload):
        return {"raw": "{}"}

    runtime = pooled_runtime(
        monkeypatch,
        (timeout, healthy),
        cooldown_seconds=0,
        events=events.append,
    )
    await runtime.invoke("translate", payload("translate"), context())

    cooling = next(event for event in events if event.get("state") == "cooling")
    assert cooling["key_slot"] == "agnes-1"
    assert cooling["status_code"] is None
    assert "请求超时或连接失败" in cooling["notice"]
    assert "secret-0" not in str(events)
    assert "vendor details" not in str(events)


@pytest.mark.asyncio
async def test_cancellation_decrements_http_active():
    started = asyncio.Event()

    async def transport(_kind, _payload):
        started.set()
        await asyncio.Event().wait()
        return {"raw": "{}"}

    runtime = ModelRuntime(transport=transport)
    task = asyncio.create_task(runtime.invoke("translate", payload("translate"), context()))
    await started.wait()
    assert runtime.snapshot["http_active"] == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.snapshot["http_active"] == 0


@pytest.mark.asyncio
async def test_http_seconds_excludes_retry_backoff():
    now = {"value": 0.0}
    calls = 0

    async def sleep(seconds):
        now["value"] += seconds

    async def transport(_kind, _payload):
        nonlocal calls
        calls += 1
        now["value"] += 2
        if calls == 1:
            raise OSError("offline")
        return {"raw": "{}"}

    runtime = ModelRuntime(transport=transport, sleep=sleep, monotonic=lambda: now["value"])
    async with runtime.workflow():
        await runtime.invoke("translate", payload("translate"), context())
        stats = runtime.workflow_stats

    assert stats["http_attempts"] == 2
    assert stats["http_seconds"] == 4.0
    assert now["value"] == 5.0
