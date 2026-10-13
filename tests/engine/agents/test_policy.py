"""Every provider and model stage uses complete responses with a 300s deadline."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from engine.agents import models
from engine.agents.runtime import ModelRuntime, RequestError
from engine.agents.streaming import StreamingOpenAILike
from tests.engine.agents.test_runtime import FakeOpenAIClient, context, payload


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["terms", "resolution", "translate", "review", "coherence"])
@pytest.mark.parametrize("provider", ["agnes", "cr_proxy", "legacy_streaming"])
async def test_every_stage_and_provider_requests_complete_response_with_300s_timeout(stage, provider, monkeypatch):
    monkeypatch.setattr(
        models,
        "settings",
        SimpleNamespace(
            AGNES_MODEL="agnes-test",
            AGNES_API_KEY="test-key",
            AGNES_BASE_URL="https://example.invalid/v1",
            CR_PROXY_MODEL="proxy-test",
            CR_PROXY_API_KEY="test-key",
            CR_PROXY_BASE_URL="https://example.invalid/v1",
        ),
    )
    requests = []
    deadlines = []
    wait_for = asyncio.wait_for

    async def checked_wait_for(aw, timeout):
        deadlines.append(timeout)
        return await wait_for(aw, timeout)

    monkeypatch.setattr(asyncio, "wait_for", checked_wait_for)
    response = json.dumps(payload(stage))

    class Completions:
        async def create(self, **kwargs):
            requests.append(kwargs)
            assert kwargs.get("stream") is False
            assert "stream_options" not in kwargs
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=response), finish_reason="stop")],
                usage={"prompt_tokens": 4, "completion_tokens": 2},
                id="response-id",
                model="provider-model",
                system_fingerprint="fingerprint",
            )

    class Client(FakeOpenAIClient):
        def is_closed(self):
            return False

    client = Client(Completions())
    model = (
        models.build_primary_model()
        if provider == "agnes"
        else models.build_fallback_model()
        if provider == "cr_proxy"
        else StreamingOpenAILike(id="legacy-model", api_key="test-key")
    )
    model.async_client = cast(Any, client)
    monkeypatch.setattr(
        model, "get_request_params", lambda **_kwargs: {"stream": True, "stream_options": {"include_usage": True}}
    )
    runtime = ModelRuntime(model=model, max_transport_retries=0)
    result = await runtime.invoke(stage, payload(stage), context())

    assert len(requests) == 1
    assert deadlines == [300.0]
    assert client.options == [(0, 300.0)]
    assert result["raw"] == response and result["finish_reason"] == "stop"
    assert result["usage"]["output_tokens"] == 2


@pytest.mark.asyncio
async def test_complete_response_timeout_still_cancels_and_never_persists_partial_output(monkeypatch):
    finished = []
    persisted = []
    cancelled = asyncio.Event()
    deadlines = []
    wait_for = asyncio.wait_for

    async def short_clock(aw, timeout):
        deadlines.append(timeout)
        return await wait_for(aw, 0.01)

    async def transport(*_args):
        try:
            await asyncio.Event().wait()
            return {}
        finally:
            cancelled.set()

    monkeypatch.setattr(asyncio, "wait_for", short_clock)
    runtime = ModelRuntime(
        transport=transport,
        max_transport_retries=0,
        finish_attempt=lambda *_args, **fields: finished.append(fields),
        persist_response=lambda *_args: persisted.append(_args),
    )
    with pytest.raises(RequestError, match="request timed out"):
        await runtime.invoke("terms", payload("terms"), context())
    assert deadlines == [300.0]
    assert cancelled.is_set() and not persisted
    assert finished[-1]["state"] == "unknown"


def test_term_runner_uses_300s_even_with_a_frozen_120s_timeout(tmp_path):
    from engine.services.terms.runner import TermRunner
    from tests.engine.services.terms.test_runner import _prepare

    store, _ = _prepare(tmp_path, request_timeout_seconds=120)
    runner = TermRunner(store, transport=lambda *_args: None)
    assert runner.config["request_timeout_seconds"] == 120
    assert runner.runtime._request_timeout_seconds == 300.0


def test_body_runner_uses_300s_even_with_a_frozen_120s_timeout(tmp_path):
    from engine.services.journal import BodyJournal
    from tests.engine.agents.test_workflow import prepare_case

    case = prepare_case(tmp_path, "<p>Hello.</p>", ("Hello.",))
    journal = BodyJournal(case.session.store)
    journal.session.prepared.plan.translation_config["request_timeout_seconds"] = 120
    runtime = journal.runtime(transport=lambda *_args: None)
    assert runtime._request_timeout_seconds == 300.0
