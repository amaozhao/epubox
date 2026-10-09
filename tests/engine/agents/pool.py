import asyncio
from types import SimpleNamespace

import pytest

import engine.agents.runtime as runtime_module
from engine.agents.pool import WorkflowPool, WorkflowState, finite_cooldown, limit_workflows, workflow_limit
from engine.agents.runtime import ModelRuntime, ProviderError, RuntimePaused
from engine.schemas.internal import RequestManifest

from .runtime import FakeOpenAIClient, MemoryJournal, context, payload


def pooled_runtime(monkeypatch, transports, **values) -> ModelRuntime:
    keys = tuple(f"secret-{index}" for index in range(len(transports)))
    source = SimpleNamespace(
        id="agnes-model",
        provider="Agnes",
        max_tokens=100,
        max_completion_tokens=None,
        max_retries=2,
        api_key=keys[0],
        _epubox_keys=keys,
    )
    models = tuple(
        SimpleNamespace(**(vars(source) | {"api_key": key, "transport": transport}))
        for key, transport in zip(keys, transports, strict=True)
    )
    monkeypatch.setattr(runtime_module, "build_key_models", lambda _model: models)
    monkeypatch.setattr(ModelRuntime, "_agno_transport", lambda _self, model: model.transport)
    return ModelRuntime(model=source, model_max_output_tokens=100, **values)


@pytest.mark.asyncio
async def test_parallel_workflows_overlap_and_each_keeps_one_key(monkeypatch):
    calls: list[tuple[int, str, str]] = []
    entered = asyncio.Event()

    def transport(index):
        async def call(kind, request_payload):
            calls.append((index, request_payload["request_id"], kind))
            if kind == "translate":
                if len([row for row in calls if row[2] == "translate"]) == 2:
                    entered.set()
                await entered.wait()
            return {"raw": "{}"}

        return call

    runtime = pooled_runtime(monkeypatch, (transport(0), transport(1)))

    async def run(request_id):
        async with runtime.workflow():
            await runtime.invoke("translate", payload("translate", request_id), context(request_id))
            await runtime.invoke("review", payload("review", f"{request_id}-review"), context(f"{request_id}-review"))

    await asyncio.gather(run("a"), run("b"))

    assignments = {request_id: index for index, request_id, _kind in calls if not request_id.endswith("-review")}
    assert set(assignments.values()) == {0, 1}
    assert all(index == assignments[request_id.removesuffix("-review")] for index, request_id, _kind in calls)


@pytest.mark.asyncio
async def test_duplicate_key_capacity_and_explicit_limit(monkeypatch):
    async def transport(_kind, _payload):
        return {"raw": "{}"}

    one = pooled_runtime(monkeypatch, (transport,))
    assert one.key_count == 1
    assert one.workflow_capacity == 1

    two = pooled_runtime(monkeypatch, (transport, transport))

    @limit_workflows
    async def capacity(*, concurrency=None):
        return two.workflow_capacity, workflow_limit.get()

    assert await capacity(concurrency=1) == (1, 1)
    assert two.workflow_capacity == 2
    assert workflow_limit.get() is None


@pytest.mark.asyncio
async def test_429_and_timeout_switch_key_without_resetting_workflow(monkeypatch):
    calls: list[tuple[int, str]] = []

    async def first(kind, _payload):
        calls.append((0, kind))
        raise ProviderError("limited", status_code=429, retry_after=0)

    async def second(kind, _payload):
        calls.append((1, kind))
        return {"raw": "{}"}

    runtime = pooled_runtime(monkeypatch, (first, second), sleep=lambda _seconds: asyncio.sleep(0))
    async with runtime.workflow():
        await runtime.invoke("translate", payload("translate"), context())
        await runtime.invoke("review", payload("review", "r2"), context("r2"))

    assert calls == [(0, "translate"), (1, "translate"), (1, "review")]


@pytest.mark.asyncio
async def test_one_keys_service_failure_count_does_not_pause_healthy_key(monkeypatch):
    calls: list[int] = []

    async def failing(_kind, _payload):
        calls.append(0)
        raise ProviderError("unavailable", status_code=500)

    async def healthy(_kind, _payload):
        calls.append(1)
        return {"raw": "{}"}

    runtime = pooled_runtime(
        monkeypatch,
        (failing, healthy),
        cooldown_seconds=0,
        max_service_failures=2,
        sleep=lambda _seconds: asyncio.sleep(0),
    )
    await runtime.invoke("translate", payload("translate", "r1"), context("r1"))
    await runtime.invoke("translate", payload("translate", "r2"), context("r2"))
    assert calls == [0, 1, 0, 1]


@pytest.mark.asyncio
async def test_unknown_attempt_switches_key_and_keeps_attempt_evidence(monkeypatch):
    store = MemoryJournal()
    store.write_request(
        RequestManifest(
            request_id="r1",
            stage="translate",
            unit_ids=("u1",),
            item_ids=("i1",),
            plan_epochs={"u1": 0},
            revisions={"u1": 1},
            input_hashes={"u1": "input"},
            wire_hash="wire",
        )
    )

    async def timed_out(_kind, _payload):
        raise TimeoutError("secret-0 timed out")

    async def healthy(_kind, _payload):
        return {"raw": "{}"}

    runtime = pooled_runtime(
        monkeypatch,
        (timed_out, healthy),
        reserve_attempt=store.reserve_attempt,
        finish_attempt=store.finish_attempt,
        sleep=lambda _seconds: asyncio.sleep(0),
    )
    await runtime.invoke("translate", payload("translate"), context(item_ids=["i1"]))

    attempts = store.read_request("r1").attempts
    assert [attempt.state for attempt in attempts] == ["unknown", "succeeded"]
    assert [attempt.reservation["attempt_number"] for attempt in attempts] == [1, 2]
    assert [attempt.metadata["key_slot"] for attempt in attempts] == ["agnes-1", "agnes-2"]
    assert "secret-0" not in str(attempts)


@pytest.mark.asyncio
async def test_per_key_rate_capacity_does_not_block_other_key():
    now = {"value": 0.0}
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)
        now["value"] += seconds

    pool = WorkflowPool(("a", "b"), rpm=1, tpm=None, sleep=sleep, monotonic=lambda: now["value"])
    async with pool.workflow():
        assert await pool.item() == "a"
        await pool.reserve(1)
    async with pool.workflow():
        assert await pool.item() == "b"
        await pool.reserve(1)
    assert sleeps == []
    async with pool.workflow():
        assert await pool.item() == "a"
        await pool.reserve(1)
    assert sleeps == [60.0]


@pytest.mark.asyncio
async def test_runtimes_share_per_key_quota_without_sharing_transports():
    now = {"value": 0.0}
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)
        now["value"] += seconds

    state = WorkflowState(1)
    first = WorkflowPool(
        ("first-transport",),
        rpm=1,
        tpm=None,
        sleep=sleep,
        monotonic=lambda: now["value"],
        state=state,
    )
    second = WorkflowPool(
        ("second-transport",),
        rpm=1,
        tpm=None,
        sleep=sleep,
        monotonic=lambda: now["value"],
        state=state,
    )
    async with first.workflow():
        assert await first.item() == "first-transport"
        await first.reserve(1)
    async with second.workflow():
        assert await second.item() == "second-transport"
        await second.reserve(1)
    assert sleeps == [60.0]


@pytest.mark.asyncio
async def test_each_key_reuses_only_its_own_async_client(monkeypatch):
    class Completions:
        async def create(self, **_kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason="stop")],
                usage=None,
                id="response",
                model="agnes-model",
                system_fingerprint=None,
            )

    class FakeModel:
        id = "agnes-model"
        provider = "Agnes"
        _epubox_keys: tuple[str, ...] = ()
        max_tokens = 100
        max_completion_tokens = None
        max_retries = 2

        def __init__(self, key):
            self.api_key = key
            self.async_client = None
            self.client_creations = 0

        def get_async_client(self):
            if self.async_client is None:
                self.client_creations += 1
                self.async_client = FakeOpenAIClient(Completions())
            return self.async_client

        def _format_all_messages(self, messages, _compress):
            return [{"role": message.role, "content": message.content} for message in messages]

        def get_request_params(self, **_kwargs):
            return {"max_tokens": self.max_tokens}

    models = (FakeModel("secret-0"), FakeModel("secret-1"))
    source = FakeModel("secret-0")
    source._epubox_keys = ("secret-0", "secret-1")
    monkeypatch.setattr(runtime_module, "build_key_models", lambda _model: models)
    runtime = ModelRuntime(model=source, model_max_output_tokens=100)

    for request_id in ("r1", "r2", "r3", "r4"):
        await runtime.invoke("translate", payload("translate", request_id), context(request_id))

    assert [model.client_creations for model in models] == [1, 1]
    assert models[0].async_client is not models[1].async_client


@pytest.mark.asyncio
async def test_cancellation_releases_key_for_next_workflow():
    pool = WorkflowPool(("a",), rpm=None, tpm=None, sleep=asyncio.sleep, monotonic=lambda: 0.0)
    acquired = asyncio.Event()

    async def blocked():
        async with pool.workflow():
            assert await pool.item() == "a"
            acquired.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(blocked())
    await acquired.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with pool.workflow():
        assert await asyncio.wait_for(pool.item(), timeout=0.1) == "a"


@pytest.mark.asyncio
async def test_all_keys_cooling_waits_without_deadlock():
    now = {"value": 0.0}
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)
        now["value"] += seconds

    pool = WorkflowPool(("a",), rpm=None, tpm=None, sleep=sleep, monotonic=lambda: now["value"])
    async with pool.workflow():
        assert await pool.item() == "a"
        assert await pool.failed(cooldown=3, disabled=False, max_failures=3) == (False, False)
        assert await pool.item() == "a"
    assert sleeps == [3.0]


@pytest.mark.asyncio
async def test_healthy_key_release_wakes_waiter_before_other_key_cooldown():
    sleep_started = asyncio.Event()

    async def sleep(_seconds):
        sleep_started.set()
        await asyncio.Event().wait()

    pool = WorkflowPool(("a", "b"), rpm=None, tpm=None, sleep=sleep, monotonic=lambda: 0.0)
    async with pool.workflow():
        assert await pool.item() == "a"
        await pool.failed(cooldown=60, disabled=False, max_failures=3)

    holding = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with pool.workflow():
            assert await pool.item() == "b"
            holding.set()
            await release.wait()

    holder_task = asyncio.create_task(holder())
    await holding.wait()

    async def waiter():
        async with pool.workflow():
            return await pool.item()

    waiter_task = asyncio.create_task(waiter())
    await sleep_started.wait()
    release.set()
    assert await asyncio.wait_for(waiter_task, timeout=0.1) == "b"
    await holder_task


@pytest.mark.asyncio
async def test_all_auth_failures_pause_after_each_key_is_tried(monkeypatch):
    calls: list[int] = []

    def unauthorized(index):
        async def call(_kind, _payload):
            calls.append(index)
            raise ProviderError(f"secret-{index} unauthorized", status_code=401)

        return call

    runtime = pooled_runtime(monkeypatch, (unauthorized(0), unauthorized(1)))
    with pytest.raises(RuntimePaused, match="all Agnes API keys are disabled"):
        await runtime.invoke("translate", payload("translate"), context())

    assert calls == [0, 1]


@pytest.mark.asyncio
async def test_replayed_response_does_not_lease_disabled_pool(monkeypatch):
    async def unauthorized(_kind, _payload):
        raise ProviderError("unauthorized", status_code=401)

    def replay(_kind, request_payload, _context):
        return {"raw": "cached"} if request_payload["request_id"] == "cached" else None

    runtime = pooled_runtime(monkeypatch, (unauthorized,), replay_response=replay)
    with pytest.raises(RuntimePaused):
        await runtime.invoke("translate", payload("translate"), context())

    result = await runtime.invoke("translate", payload("translate", "cached"), context("cached"))
    assert result == {"raw": "cached"}


@pytest.mark.asyncio
async def test_simultaneous_failover_does_not_deadlock(monkeypatch):
    failures = 0
    gate = asyncio.Event()

    def transport(_index):
        calls = 0

        async def call(_kind, _payload):
            nonlocal calls, failures
            calls += 1
            if calls == 1:
                failures += 1
                if failures == 2:
                    gate.set()
                await gate.wait()
                raise TimeoutError("temporary outage")
            return {"raw": "{}"}

        return call

    runtime = pooled_runtime(
        monkeypatch,
        (transport(0), transport(1)),
        cooldown_seconds=0,
        sleep=lambda _seconds: asyncio.sleep(0),
    )
    await asyncio.wait_for(
        asyncio.gather(
            runtime.invoke("translate", payload("translate", "a"), context("a")),
            runtime.invoke("translate", payload("translate", "b"), context("b")),
        ),
        timeout=1,
    )
    assert failures == 2


def test_retry_after_must_be_finite_and_nonnegative():
    assert finite_cooldown(0, 10) == 0
    assert finite_cooldown(-1, 10) == 10
    assert finite_cooldown(float("nan"), 10) == 10
