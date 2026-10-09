"""In-memory workflow leases and per-key provider limits."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, cast

workflow_limit: ContextVar[int | None] = ContextVar("epubox_workflow_limit", default=None)


def limit_workflows[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Keep a CLI concurrency override local to one translate invocation."""

    if asyncio.iscoroutinefunction(function):

        @wraps(function)
        async def async_wrapped(*args: P.args, **kwargs: P.kwargs) -> Any:
            value = dict(kwargs).get("concurrency")
            limit = value if type(value) is int and value > 0 else None
            token = workflow_limit.set(limit)
            try:
                return await function(*args, **kwargs)
            finally:
                workflow_limit.reset(token)

        return cast(Callable[P, R], async_wrapped)

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        value = dict(kwargs).get("concurrency")
        limit = value if type(value) is int and value > 0 else None
        token = workflow_limit.set(limit)
        try:
            return function(*args, **kwargs)
        finally:
            workflow_limit.reset(token)

    return wrapped


class PoolUnavailable(RuntimeError):
    def __init__(self, message: str, *, all_disabled: bool = False):
        super().__init__(message)
        self.all_disabled = all_disabled


@dataclass
class _Slot:
    index: int
    leased: bool = False
    disabled: bool = False
    verified: bool = False
    cooldown_until: float = 0.0
    failures: int = 0
    reservations: deque[tuple[float, int]] = field(default_factory=deque)


@dataclass
class _Session:
    slot: _Slot | None = None


class WorkflowState:
    """Quota and lease state shared by runtimes using the same in-memory run model."""

    def __init__(self, size: int) -> None:
        if size < 1:
            raise ValueError("workflow state requires at least one slot")
        self.slots = tuple(_Slot(index) for index in range(size))
        self.lock = asyncio.Lock()
        self.changed = asyncio.Event()
        self.cursor = 0

    def signal(self) -> None:
        changed = self.changed
        self.changed = asyncio.Event()
        changed.set()


class WorkflowPool:
    """Lease one provider identity for a workflow and switch only after failure."""

    def __init__(
        self,
        items: tuple[Any, ...],
        *,
        rpm: int | None,
        tpm: int | None,
        sleep: Callable[[float], Awaitable[None]],
        monotonic: Callable[[], float],
        state: WorkflowState | None = None,
    ) -> None:
        if not items:
            raise ValueError("workflow pool requires at least one item")
        self._items = items
        self._state = state or WorkflowState(len(items))
        if len(self._state.slots) != len(items):
            raise ValueError("workflow pool items differ from shared state")
        self._session: ContextVar[_Session | None] = ContextVar("epubox_workflow_session", default=None)
        self._rpm = rpm
        self._tpm = tpm
        self._sleep = sleep
        self._monotonic = monotonic

    @property
    def capacity(self) -> int:
        enabled = sum(not slot.disabled for slot in self._state.slots)
        limit = workflow_limit.get()
        return min(enabled, limit) if limit is not None else enabled

    @property
    def size(self) -> int:
        return len(self._state.slots)

    @property
    def active(self) -> bool:
        return self._session.get() is not None

    @asynccontextmanager
    async def workflow(self) -> AsyncIterator[None]:
        if self.active:
            yield
            return
        session = _Session()
        token = self._session.set(session)
        try:
            yield
        finally:
            try:
                await asyncio.shield(self.release(session))
            finally:
                self._session.reset(token)

    async def item(self) -> Any:
        session = self._required_session()
        if session.slot is None:
            session.slot = await self._acquire()
        return self._items[session.slot.index]

    @property
    def slot_label(self) -> str:
        return f"agnes-{self._required_slot().index + 1}"

    def snapshot(self) -> dict[str, int]:
        now = self._monotonic()
        enabled = tuple(slot for slot in self._state.slots if not slot.disabled)
        return {
            "key_count": self.size,
            "enabled_keys": len(enabled),
            "verified_keys": sum(slot.verified for slot in enabled),
            "cooling_keys": sum(slot.cooldown_until > now for slot in enabled),
            "leased_keys": sum(slot.leased for slot in enabled),
        }

    async def reserve(self, estimated_tokens: int) -> None:
        slot = self._required_slot()
        while True:
            now = self._monotonic()
            while slot.reservations and now - slot.reservations[0][0] >= 60:
                slot.reservations.popleft()
            wait_for = 0.0
            if self._rpm is not None and len(slot.reservations) >= self._rpm:
                wait_for = 60 - (now - slot.reservations[0][0])
            if self._tpm is not None and sum(tokens for _, tokens in slot.reservations) + estimated_tokens > self._tpm:
                wait_for = max(wait_for, 60 - (now - slot.reservations[0][0])) if slot.reservations else 60.0
            if wait_for <= 0:
                slot.reservations.append((now, estimated_tokens))
                return
            await self._sleep(wait_for)

    async def succeeded(self) -> bool:
        slot = self._required_slot()
        newly_verified = not slot.verified
        slot.failures = 0
        slot.verified = True
        return newly_verified

    async def failed(
        self,
        *,
        cooldown: float,
        disabled: bool,
        max_failures: int,
    ) -> tuple[bool, bool]:
        session = self._required_session()
        slot = self._required_slot()
        async with self._state.lock:
            slot.leased = False
            slot.disabled = slot.disabled or disabled
            slot.failures += 1
            if not slot.disabled:
                slot.cooldown_until = max(slot.cooldown_until, self._monotonic() + max(0.0, cooldown))
            session.slot = None
            self._state.signal()
            available = tuple(candidate for candidate in self._state.slots if not candidate.disabled)
            return not available, bool(available) and all(
                not candidate.leased and candidate.failures >= max_failures for candidate in available
            )

    async def release(self, session: _Session | None = None) -> None:
        session = self._required_session() if session is None else session
        slot = session.slot
        if slot is None:
            return
        async with self._state.lock:
            slot.leased = False
            session.slot = None
            self._state.signal()

    def _required_session(self) -> _Session:
        session = self._session.get()
        if session is None:
            raise RuntimeError("provider access requires a workflow context")
        return session

    def _required_slot(self) -> _Slot:
        slot = self._required_session().slot
        if slot is None:
            raise RuntimeError("workflow has no provider lease")
        return slot

    async def _acquire(self) -> _Slot:
        while True:
            wait_for: float | None = None
            async with self._state.lock:
                enabled = tuple(slot for slot in self._state.slots if not slot.disabled)
                if not enabled:
                    raise PoolUnavailable("all Agnes API keys are disabled", all_disabled=True)
                now = self._monotonic()
                if sum(slot.leased for slot in self._state.slots) < self.capacity:
                    ready = next(
                        (
                            self._state.slots[(self._state.cursor + offset) % self.size]
                            for offset in range(self.size)
                            if not self._state.slots[(self._state.cursor + offset) % self.size].disabled
                            and not self._state.slots[(self._state.cursor + offset) % self.size].leased
                            and self._state.slots[(self._state.cursor + offset) % self.size].cooldown_until <= now
                        ),
                        None,
                    )
                    if ready is not None:
                        ready.leased = True
                        self._state.cursor = (ready.index + 1) % self.size
                        return ready
                    cooling = [slot.cooldown_until - now for slot in enabled if not slot.leased]
                    if cooling:
                        wait_for = max(0.0, min(cooling))
                changed = self._state.changed
            if wait_for is None:
                await changed.wait()
                continue
            notified = asyncio.create_task(changed.wait())
            cooled = asyncio.ensure_future(self._sleep(wait_for))
            try:
                done, pending = await asyncio.wait((notified, cooled), return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                notified.cancel()
                cooled.cancel()
                await asyncio.gather(notified, cooled, return_exceptions=True)
                raise
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            for task in done:
                task.result()


def finite_cooldown(value: object, default: float) -> float:
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        return float(value)
    return default
