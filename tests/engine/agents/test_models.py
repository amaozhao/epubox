from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from agno.models.message import Message
from agno.models.mistral import MistralChat
from agno.models.response import ModelResponse

from engine.agents.models import build_primary_model, fallback_model
from engine.agents.streaming_openai_like import StreamingOpenAILike


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
    def test_build_primary_model_ignores_proxy_provider_and_stays_on_mistral(self, monkeypatch):
        fake_settings = SimpleNamespace(
            MODEL_PROVIDER="cr_proxy",
            CR_PROXY_MODEL="gpt-5.3-codex-spark",
            CR_PROXY_API_KEY="proxy-key",
            CR_PROXY_BASE_URL="http://proxy.example.com/api/v1",
            MISTRAL_MODEL="mistral-medium-latest",
            MISTRAL_API_KEY="mistral-key",
        )
        monkeypatch.setattr("engine.agents.models.settings", fake_settings)

        model = build_primary_model()

        assert isinstance(model, MistralChat)
        assert model.id == "mistral-medium-latest"

    def test_build_primary_model_defaults_to_mistral(self, monkeypatch):
        fake_settings = SimpleNamespace(
            MODEL_PROVIDER="mistral",
            CR_PROXY_MODEL="gpt-5.3-codex-spark",
            CR_PROXY_API_KEY="proxy-key",
            CR_PROXY_BASE_URL="http://proxy.example.com/api/v1",
            MISTRAL_MODEL="mistral-medium-latest",
            MISTRAL_API_KEY="mistral-key",
        )
        monkeypatch.setattr("engine.agents.models.settings", fake_settings)

        model = build_primary_model()

        assert isinstance(model, MistralChat)


class TestFallbackModel:
    def test_fallback_model_uses_proxy_client(self):
        assert isinstance(fallback_model, StreamingOpenAILike)
