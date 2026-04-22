import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_run_with_fallback_rate_limit_waits_between_calls(monkeypatch):
    from engine.agents import fallback_runtime

    await fallback_runtime.reset_fallback_runtime_state()

    current_time = {"value": 100.0}
    sleep_calls: list[float] = []

    def fake_now() -> float:
        return current_time["value"]

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        current_time["value"] += seconds

    monkeypatch.setattr(fallback_runtime, "_monotonic", fake_now)
    monkeypatch.setattr(fallback_runtime, "_sleep", fake_sleep)

    calls: list[str] = []

    async def first_call():
        calls.append("first")
        return "ok-1"

    async def second_call():
        calls.append("second")
        return "ok-2"

    result1 = await fallback_runtime.run_with_fallback_rate_limit("translate", first_call)
    current_time["value"] += 10.0
    result2 = await fallback_runtime.run_with_fallback_rate_limit("proofread", second_call)

    assert result1 == "ok-1"
    assert result2 == "ok-2"
    assert calls == ["first", "second"]
    assert sleep_calls == [50.0]


@pytest.mark.asyncio
async def test_run_with_fallback_rate_limit_serializes_concurrent_calls(monkeypatch):
    from engine.agents import fallback_runtime

    await fallback_runtime.reset_fallback_runtime_state()

    current_time = {"value": 200.0}
    sleep_calls: list[float] = []
    started: list[str] = []

    def fake_now() -> float:
        return current_time["value"]

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        current_time["value"] += seconds

    monkeypatch.setattr(fallback_runtime, "_monotonic", fake_now)
    monkeypatch.setattr(fallback_runtime, "_sleep", fake_sleep)

    first_gate = asyncio.Event()
    second_can_finish = asyncio.Event()

    async def first_call():
        started.append("first")
        first_gate.set()
        await second_can_finish.wait()
        return "first-done"

    async def second_call():
        started.append("second")
        return "second-done"

    first_task = asyncio.create_task(fallback_runtime.run_with_fallback_rate_limit("translate", first_call))
    await first_gate.wait()
    current_time["value"] += 1.0
    second_task = asyncio.create_task(fallback_runtime.run_with_fallback_rate_limit("proofread", second_call))
    await asyncio.sleep(0)
    assert started == ["first"]

    second_can_finish.set()
    result1 = await first_task
    result2 = await second_task

    assert result1 == "first-done"
    assert result2 == "second-done"
    assert started == ["first", "second"]
    assert sleep_calls == [59.0]


@pytest.mark.asyncio
async def test_run_fallback_agent_delegates_to_agent_arun(monkeypatch):
    from engine.agents import fallback_runtime

    await fallback_runtime.reset_fallback_runtime_state()
    monkeypatch.setattr(fallback_runtime, "FALLBACK_MIN_INTERVAL_SECONDS", 0.0)

    agent = MagicMock()
    agent.arun = AsyncMock(return_value="translated")

    result = await fallback_runtime.run_fallback_agent("translate", agent, '{"text":"hello"}')

    assert result == "translated"
    agent.arun.assert_awaited_once_with('{"text":"hello"}')
