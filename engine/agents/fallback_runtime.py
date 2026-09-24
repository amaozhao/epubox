import asyncio
import time
from collections.abc import Awaitable, Callable

from engine.core.config import settings
from engine.core.logger import engine_logger as logger

FALLBACK_MIN_INTERVAL_SECONDS = 60.0
PRIMARY_MIN_INTERVAL_SECONDS = 60.0 / settings.AGNES_TEXT_RPM

# ponytail: process-local limit; use shared storage if parallel CLI processes share one API key.
_locks = {"primary": asyncio.Lock(), "fallback": asyncio.Lock()}
_last_started_at: dict[str, float | None] = {"primary": None, "fallback": None}
_monotonic = time.monotonic
_sleep = asyncio.sleep


async def reset_fallback_runtime_state() -> None:
    _locks["fallback"] = asyncio.Lock()
    _last_started_at["fallback"] = None


async def reset_primary_runtime_state() -> None:
    _locks["primary"] = asyncio.Lock()
    _last_started_at["primary"] = None


async def _wait_for_rate_limit(
    channel: str,
    kind: str,
    min_interval_seconds: float,
) -> None:
    now = _monotonic()
    last_started_at = _last_started_at[channel]
    if last_started_at is not None:
        wait_seconds = min_interval_seconds - (now - last_started_at)
        if wait_seconds > 0:
            logger.info(f"{channel} {kind} 调用等待 {wait_seconds:.1f} 秒以满足限流要求")
            await _sleep(wait_seconds)
            now = _monotonic()

    _last_started_at[channel] = now
    logger.info(f"开始执行 {channel} {kind} 调用")


async def run_with_fallback_rate_limit[T](kind: str, runner: Callable[[], Awaitable[T]]) -> T:
    async with _locks["fallback"]:
        await _wait_for_rate_limit("fallback", kind, FALLBACK_MIN_INTERVAL_SECONDS)
        return await runner()


async def run_with_primary_rate_limit[T](kind: str, runner: Callable[[], Awaitable[T]]) -> T:
    async with _locks["primary"]:
        await _wait_for_rate_limit("primary", kind, PRIMARY_MIN_INTERVAL_SECONDS)
    return await runner()


async def run_fallback_agent(kind: str, agent, payload: str):
    return await run_with_fallback_rate_limit(kind, lambda: agent.arun(payload))


async def run_primary_agent(kind: str, agent, payload: str):
    return await run_with_primary_rate_limit(kind, lambda: agent.arun(payload))
