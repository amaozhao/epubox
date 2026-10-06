from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from agno.models.message import Message
from agno.models.openai.like import OpenAILike
from agno.models.response import ModelResponse

from engine.agents.models import (
    build_primary_model,
    fallback_model,
    proofreader_fallback_model,
    proofreader_model,
)
from engine.agents.streaming import StreamingOpenAILike


class TestStreamingOpenAILike:
    def test_invoke_aggregates_stream_chunks(self, monkeypatch):
        model = StreamingOpenAILike(id="proxy-model", api_key="key", base_url="http://example.com")
        assistant_message = Message(role="assistant")

        def fake_stream(**kwargs):
            yield ModelResponse(role="assistant", content="{")
            yield ModelResponse(content='"translation":"OK"')
            yield ModelResponse(content="}", provider_data={"id": "resp_123"})

        monkeypatch.setattr(model, "invoke_stream", fake_stream)

        result = model.invoke(messages=[], assistant_message=assistant_message)

        assert result.role == "assistant"
        assert result.content == '{"translation":"OK"}'
        assert result.provider_data == {"id": "resp_123"}

    @pytest.mark.asyncio
    async def test_ainvoke_aggregates_async_stream_chunks(self, monkeypatch):
        model = StreamingOpenAILike(id="proxy-model", api_key="key", base_url="http://example.com")
        assistant_message = Message(role="assistant")

        async def fake_stream(**kwargs):
            yield ModelResponse(role="assistant", content='{"corrections":')
            yield ModelResponse(content=" {}}", response_usage=MagicMock())

        monkeypatch.setattr(model, "ainvoke_stream", fake_stream)

        result = await model.ainvoke(messages=[], assistant_message=assistant_message)

        assert result.role == "assistant"
        assert result.content == '{"corrections": {}}'
        assert result.response_usage is not None


class TestBuildPrimaryModel:
    def test_build_primary_model_uses_agnes_openai_compatible_api(self, monkeypatch):
        fake_settings = SimpleNamespace(
            AGNES_MODEL="agnes-2.0-flash",
            AGNES_API_KEY="agnes-key",
            AGNES_BASE_URL="https://apihub.agnes-ai.com/v1",
        )
        monkeypatch.setattr("engine.agents.models.settings", fake_settings)

        model = build_primary_model()

        assert isinstance(model, OpenAILike)
        assert model.id == "agnes-2.0-flash"
        assert model.api_key == "agnes-key"
        assert model.base_url == "https://apihub.agnes-ai.com/v1"
        assert model.provider == "Agnes"


class TestFallbackModel:
    def test_fallback_model_uses_proxy_client(self):
        assert isinstance(fallback_model, StreamingOpenAILike)


def test_proofreader_model_caps_completion_size():
    assert proofreader_model.max_completion_tokens == 2048
    assert proofreader_fallback_model.max_completion_tokens == 2048
