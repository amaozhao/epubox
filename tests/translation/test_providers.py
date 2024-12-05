"""Test translation providers."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.translation.providers import (
    ConfigurationError,
    GoogleProvider,
    OpenAIProvider,
    RateLimiter,
    RateLimitError,
    TranslationProvider,
)


async def test_rate_limiter():
    """Test rate limiter functionality."""
    limiter = RateLimiter(rate_limit=2, time_window=1)

    # First two requests should succeed
    await limiter.acquire()
    await limiter.acquire()

    # Third request should fail
    with pytest.raises(RateLimitError):
        await limiter.acquire()

    # Wait for tokens to replenish
    await asyncio.sleep(1)

    # Should be able to make another request
    await limiter.acquire()


class TestOpenAIProvider:
    """Test OpenAI translation provider."""

    @pytest.fixture
    async def provider(self):
        """Create an OpenAI provider instance."""
        config = {"api_key": "test-key", "model": "gpt-3.5-turbo"}
        provider = OpenAIProvider(config)
        await provider.initialize()
        yield provider
        await provider.cleanup()

    def test_provider_type(self, provider):
        """Test provider type identification."""
        assert provider.get_provider_type() == "openai"

    def test_validate_config(self, provider):
        """Test configuration validation."""
        # Valid config
        assert provider.validate_config({"api_key": "key"})

        # Invalid config
        assert not provider.validate_config({})
        assert not provider.validate_config({"wrong_key": "value"})

    @patch("openai.AsyncOpenAI")
    async def test_translate(self, mock_openai, provider):
        """Test translation functionality."""
        mock_client = AsyncMock()
        mock_openai.return_value = mock_client

        mock_response = AsyncMock()
        mock_response.choices[0].message.content = "翻译后的文本"
        mock_client.chat.completions.create.return_value = mock_response

        result = await provider.translate(
            text="Hello, world!", source_lang="en", target_lang="zh"
        )

        assert result == "翻译后的文本"
        mock_client.chat.completions.create.assert_called_once()


class TestGoogleProvider:
    """Test Google translation provider."""

    @pytest.fixture
    async def provider(self):
        """Create a Google provider instance."""
        config = {"api_key": "test-key"}
        provider = GoogleProvider(config)
        await provider.initialize()
        yield provider
        await provider.cleanup()

    def test_provider_type(self, provider):
        """Test provider type identification."""
        assert provider.get_provider_type() == "google"

    def test_validate_config(self, provider):
        """Test configuration validation."""
        # Valid config
        assert provider.validate_config({"api_key": "key"})

        # Invalid config
        assert not provider.validate_config({})
        assert not provider.validate_config({"wrong_key": "value"})

    @patch("google.cloud.translate_v2.Client")
    async def test_translate(self, mock_client, provider):
        """Test translation functionality."""
        mock_instance = mock_client.return_value
        mock_instance.translate.return_value = {"translatedText": "翻译后的文本"}

        result = await provider.translate(
            text="Hello, world!", source_lang="en", target_lang="zh"
        )

        assert result == "翻译后的文本"
        mock_instance.translate.assert_called_once_with(
            "Hello, world!", source_language="en", target_language="zh"
        )


async def test_retry_on_error():
    """Test retry mechanism."""
    mock_func = AsyncMock()
    mock_func.side_effect = [
        ConfigurationError("First error"),
        ConfigurationError("Second error"),
        "success",
    ]

    provider = OpenAIProvider({"api_key": "test"})
    provider.translate = mock_func

    result = await provider.translate(text="test", source_lang="en", target_lang="zh")

    assert result == "success"
    assert mock_func.call_count == 3  # Called three times before succeeding


async def test_retry_max_attempts():
    """Test retry mechanism reaches max attempts."""
    mock_func = AsyncMock()
    mock_func.side_effect = ConfigurationError("Persistent error")

    provider = OpenAIProvider({"api_key": "test"})
    provider.translate = mock_func

    with pytest.raises(ConfigurationError):
        await provider.translate(text="test", source_lang="en", target_lang="zh")

    assert mock_func.call_count == 3  # Default retry count is 3
