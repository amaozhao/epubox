import pytest

from engine.agents.runtime import ModelRuntime, ProviderError, RequestError, RuntimePaused

from .runtime import context, payload


@pytest.mark.asyncio
async def test_body_timeouts_remain_local_and_later_requests_continue():
    calls = 0
    states: list[str] = []

    async def transport(kind, request_payload):
        nonlocal calls
        calls += 1
        if calls <= 4:
            raise TimeoutError("provider timed out")
        return {"raw": "{}"}

    def finish(_request_id, _attempt_id, *, state, **_fields):
        states.append(state)

    runtime = ModelRuntime(
        transport=transport,
        finish_attempt=finish,
        max_transport_retries=0,
        max_service_failures=3,
        shared_service_failures=False,
        model_max_output_tokens=100,
    )

    for index in range(4):
        request_id = f"r{index}"
        with pytest.raises(RequestError) as raised:
            await runtime.invoke("translate", payload("translate", request_id), context(request_id))
        assert raised.value.attempts == 1

    result = await runtime.invoke("review", payload("review", "healthy"), context("healthy"))

    assert result["raw"] == "{}"
    assert calls == 5
    assert states == ["sent", "unknown"] * 4 + ["sent", "succeeded"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 402, 403, 500])
async def test_body_provider_failures_surface_as_request_errors(status_code):
    async def transport(kind, request_payload):
        raise ProviderError("provider failed", status_code=status_code)

    runtime = ModelRuntime(
        transport=transport,
        max_transport_retries=0,
        shared_service_failures=False,
        model_max_output_tokens=100,
    )

    with pytest.raises(RequestError) as raised:
        await runtime.invoke("translate", payload("translate"), context())

    assert raised.value.status_code == status_code
    assert raised.value.attempts == 1


@pytest.mark.asyncio
async def test_default_runtime_keeps_shared_auth_pause_policy():
    async def transport(kind, request_payload):
        raise ProviderError("unauthorized", status_code=401)

    runtime = ModelRuntime(transport=transport, model_max_output_tokens=100)

    with pytest.raises(RuntimePaused, match="unauthorized"):
        await runtime.invoke("terms", payload("terms"), context())
