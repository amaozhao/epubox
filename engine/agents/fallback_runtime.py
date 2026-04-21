import asyncio
import time
from typing import Awaitable, Callable, TypeVar

from engine.core.logger import engine_logger as logger

T = TypeVar("T")

FALLBACK_MIN_INTERVAL_SECONDS = 60.0

_fallback_lock = asyncio.Lock()
_last_fallback_started_at: float | None = None
_monotonic = time.monotonic
_sleep = asyncio.sleep


async def reset_fallback_runtime_state() -> None:
    global _fallback_lock, _last_fallback_started_at
    _fallback_lock = asyncio.Lock()
    _last_fallback_started_at = None


async def run_with_fallback_rate_limit(kind: str, runner: Callable[[], Awaitable[T]]) -> T:
    global _last_fallback_started_at

    async with _fallback_lock:
        now = _monotonic()
        if _last_fallback_started_at is not None:
            wait_seconds = FALLBACK_MIN_INTERVAL_SECONDS - (now - _last_fallback_started_at)
            if wait_seconds > 0:
                logger.info(f"fallback {kind} 调用等待 {wait_seconds:.1f} 秒以满足限流要求")
                await _sleep(wait_seconds)
                now = _monotonic()

        _last_fallback_started_at = now
        logger.info(f"开始执行 fallback {kind} 调用")
        return await runner()


async def run_fallback_agent(kind: str, agent, payload: str):
    return await run_with_fallback_rate_limit(kind, lambda: agent.arun(payload))
