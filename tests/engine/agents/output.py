from copy import deepcopy
from types import SimpleNamespace

import pytest

from engine.agents import wire
from engine.agents.models import (
    build_fallback_model,
    build_primary_model,
    proofreader_fallback_model,
    proofreader_model,
)
from engine.agents.runtime import InputBudgetError, ModelRuntime, RequestError, wire_hash

from .runtime import FakeOpenAIClient, context, payload, payload_with_estimated_input
from .wire import payload as compact_payload


def test_model_factories_ignore_legacy_output_caps():
    models = (
        build_primary_model(max_completion_tokens=1),
        build_fallback_model(max_completion_tokens=1),
        proofreader_model,
        proofreader_fallback_model,
    )
    for model in models:
        assert getattr(model, "max_tokens", None) is None
        assert getattr(model, "max_completion_tokens", None) is None
        assert getattr(model, "max_output_tokens", None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("historical_cap", (None, 8192))
async def test_provider_request_has_no_output_limit_and_accepts_large_complete_usage(historical_cap):
    requests: list[dict] = []

    class Completions:
        async def create(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="x" * 9000), finish_reason="stop")],
                usage={"prompt_tokens": 10, "completion_tokens": 9000, "total_tokens": 9010},
                id="response",
                model="unlimited-model",
                system_fingerprint=None,
            )

    client = FakeOpenAIClient(Completions())

    class Model:
        id = "unlimited-model"
        provider = "Agnes"
        max_tokens = 1
        max_completion_tokens = 2
        max_output_tokens = 3
        max_retries = 2
        api_key = "test-key"

        def get_async_client(self):
            return client

        def _format_all_messages(self, messages, _compress):
            return [{"role": message.role, "content": message.content} for message in messages]

        def get_request_params(self, **_kwargs):
            return {
                "max_tokens": self.max_tokens,
                "max_completion_tokens": self.max_completion_tokens,
                "max_output_tokens": self.max_output_tokens,
                "extra_body": {"max_tokens": 4, "max_output_tokens": 5, "keep": "value"},
            }

    runtime = ModelRuntime(model=Model(), model_max_output_tokens=1, provider_output_token_field="max_tokens")
    request_context = context(output_tokens=historical_cap, estimated_output_tokens=9000)
    result = await runtime.invoke("translate", payload("translate"), request_context)

    assert result["usage"]["output_tokens"] == 9000
    assert set(requests[0]).isdisjoint({"max_tokens", "max_completion_tokens", "max_output_tokens"})
    assert requests[0]["extra_body"] == {"keep": "value"}


@pytest.mark.asyncio
async def test_large_output_estimate_does_not_block_tpm_or_cached_legacy_response():
    calls = 0

    async def transport(_kind, _payload):
        nonlocal calls
        calls += 1
        return {"raw": "dispatched"}

    runtime = ModelRuntime(transport=transport, tpm=10_000, model_max_output_tokens=1)
    response = await runtime.invoke(
        "translate",
        payload("translate"),
        context(output_tokens=None, estimated_output_tokens=1_000_000),
    )
    assert response["raw"] == "dispatched"

    cached = ModelRuntime(
        transport=transport,
        model_max_output_tokens=1,
        replay_response=lambda *_args: {"raw": "cached"},
    )
    replayed = await cached.invoke("translate", payload("translate"), context(output_tokens=8192))
    assert replayed["raw"] == "cached"
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("size", "tpm", "error"),
    ((50_001, None, InputBudgetError), (5_000, 4_999, RequestError)),
)
async def test_cached_response_replays_before_physical_input_gate(size, tpm, error):
    calls = 0
    prepared = 0
    request_payload = payload_with_estimated_input("translate", size)

    async def transport(_kind, _payload):
        nonlocal calls
        calls += 1
        return {"raw": "network"}

    def prepare(*_args):
        nonlocal prepared
        prepared += 1

    cached = ModelRuntime(
        transport=transport,
        tpm=tpm,
        prepare_request=prepare,
        replay_response=lambda *_args: {"raw": "cached"},
    )
    response = await cached.invoke("translate", request_payload, context(output_tokens=8192))
    assert response["raw"] == "cached"
    assert prepared == 1 and calls == 0

    fresh = ModelRuntime(transport=transport, tpm=tpm, prepare_request=prepare)
    with pytest.raises(error):
        await fresh.invoke("translate", request_payload, context(output_tokens=8192))
    assert prepared == 2 and calls == 0


def _attempt(payload, *, cap, unlimited, identifier):
    metadata = {
        "wire_version": wire.VERSION,
        "wire_hash": wire_hash("translate", payload, cap, compact=True, wire_version=wire.VERSION),
    }
    if unlimited:
        metadata["output_unlimited"] = "true"
    reservation = {} if unlimited else {"output_tokens": cap}
    return SimpleNamespace(attempt_id=identifier, metadata=metadata, reservation=reservation)


@pytest.mark.parametrize("manifest_unlimited", (False, True))
def test_wire_verifies_mixed_historical_and_unlimited_attempts(manifest_unlimited):
    book = compact_payload("translate")
    historical_cap = 8192
    request = SimpleNamespace(
        stage="translate",
        output_unlimited=manifest_unlimited,
        wire_hash=wire_hash("translate", book, None if manifest_unlimited else historical_cap),
        attempts=(
            _attempt(book, cap=historical_cap, unlimited=False, identifier="old"),
            _attempt(book, cap=None, unlimited=True, identifier="new"),
        ),
    )

    wire.verify(request, book, historical_cap)

    tampered_attempt = deepcopy(request)
    tampered_attempt.attempts[1].metadata.pop("output_unlimited")
    with pytest.raises(ValueError, match="physical wire differs"):
        wire.verify(tampered_attempt, book, historical_cap)

    tampered_request = deepcopy(request)
    tampered_request.output_unlimited = not manifest_unlimited
    with pytest.raises(ValueError, match="frozen logical payload"):
        wire.verify(tampered_request, book, historical_cap)
