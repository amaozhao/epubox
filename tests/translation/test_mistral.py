"""Test Mistral translation provider."""

import time
from unittest.mock import AsyncMock, Mock, patch

import pytest
import tiktoken
from mistralai import Mistral, models

from app.core.config import settings
from app.translation.errors import ConfigurationError, RateLimitError, TranslationError
from app.translation.models import LimitType
from app.translation.models import TranslationProvider as ProviderModel
from app.translation.providers.base import RateLimiter
from app.translation.providers.mistral import MistralProvider


@pytest.fixture
def mock_time():
    """Create a mock time that returns increasing values."""
    with patch("time.time") as mock:
        current_time = [1000.0]

        def side_effect():
            return current_time[0]

        mock.side_effect = side_effect

        def move_time(seconds):
            current_time[0] += seconds

        mock.move_time = move_time
        yield mock


@pytest.fixture
def provider_model():
    """Create a mock provider model for testing."""
    return ProviderModel(
        id=1,
        name="Mistral Test",
        provider_type="mistral",
        is_default=True,
        enabled=True,
        config={"api_key": settings.MISTRAL_API_KEY, "model": "mistral-tiny"},
        rate_limit=1,
        retry_count=3,
        retry_delay=1,
        limit_type=LimitType.TOKENS,
        limit_value=25000,
    )


@pytest.fixture
async def provider(provider_model, mock_time):
    """Create a Mistral provider instance with test configuration."""
    provider = MistralProvider(provider_model)
    return provider


@pytest.mark.asyncio
async def test_provider_type(provider):
    """Test provider type identification."""
    assert provider.get_provider_type() == "mistral"


@pytest.mark.asyncio
async def test_validate_config(provider_model):
    """Test configuration validation."""
    # Valid config
    valid_config = {"api_key": "test-key"}
    provider_model.config = valid_config
    provider = MistralProvider(provider_model)
    assert provider.validate_config(valid_config) is True

    # Invalid config - missing API key
    invalid_config = {}
    provider_model.config = invalid_config
    provider = MistralProvider(provider_model)
    with pytest.raises(ConfigurationError) as exc_info:
        provider.validate_config(invalid_config)
    assert "API key is required" in str(exc_info.value)


@pytest.mark.asyncio
async def test_token_counting(provider):
    """Test token counting functionality."""
    test_cases = [
        ("Hello, world!", 4),  # Basic English text
        ("你好，世界！", 7),  # Chinese text (tiktoken cl100k_base encoding)
        ("", 0),  # Empty text
        # ("Hello\nWorld", 4),      # Text with newline - tiktoken treats this differently
        ("This is a test.", 5),  # Simple sentence
        ("Hello World", 2),  # Multiple spaces
    ]

    for text, expected_tokens in test_cases:
        token_count = provider._count_tokens(text)
        assert token_count == expected_tokens, f"Token count mismatch for '{text}'"


@pytest.mark.asyncio
async def test_limit_checking(provider):
    """Test text length limit checking."""
    # Test within limits
    short_text = "Hello, world!"
    await provider.check_limits(short_text)  # Should not raise

    # Test exceeding limits
    long_text = "Hello, world! " * 10000  # Should exceed token limit
    with pytest.raises(TranslationError) as exc_info:
        await provider.check_limits(long_text)
    assert "exceeds maximum allowed" in str(exc_info.value)


@pytest.mark.asyncio
@patch("mistralai.Mistral")
async def test_translate_mock(mock_mistral, provider):
    """Test translation with mocked API response."""
    # Setup mock response
    mock_client = AsyncMock()
    mock_mistral.return_value = mock_client

    mock_response = AsyncMock()
    mock_response.choices = [AsyncMock()]
    mock_response.choices[0].message.content = "你好，世界！"
    mock_client.chat.complete_async.return_value = mock_response
    provider.client = mock_client

    # Test successful translation
    result = await provider.translate(
        text="Hello, world!", source_lang="en", target_lang="zh"
    )
    assert result == "你好，世界！"
    mock_client.chat.complete_async.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not settings.MISTRAL_API_KEY, reason="MISTRAL_API_KEY not set in configuration"
)
async def test_translate_real(provider_model):
    """Test translation with real Mistral API.

    Note: This test requires MISTRAL_API_KEY to be set in configuration.
    It will be skipped if the API key is not set.
    """
    provider = MistralProvider(provider_model)

    try:
        # Test basic translation
        result = await provider.translate(
            text="Hello, how are you?", source_lang="en", target_lang="zh"
        )
        assert result
        assert len(result) > 0
        print(f"\nTranslated text: {result}")

    except Exception as e:
        pytest.fail(f"Translation failed: {str(e)}")


@pytest.mark.asyncio
async def test_rate_limiting(mock_time):
    """Test rate limiting functionality."""
    # Create rate limiter with 1 request per second
    rate_limiter = RateLimiter(requests_per_second=1)

    # First request should succeed
    await rate_limiter.acquire()  # Should not raise

    # Move time forward by a small amount (0.1 seconds)
    mock_time.move_time(0.1)

    # Second immediate request should fail with rate limit error
    with pytest.raises(RateLimitError) as exc_info:
        await rate_limiter.acquire()
    assert "Rate limit exceeded" in str(exc_info.value)

    # Move time forward by enough time to get a new token (2 seconds to be safe)
    mock_time.move_time(2.0)

    # Third request after waiting should succeed
    await rate_limiter.acquire()  # Should not raise


@pytest.mark.asyncio
async def test_provider_rate_limiting(mock_time, provider):
    """Test rate limiting through the provider interface."""
    # Mock the _translate method to avoid actual API calls
    provider._translate = AsyncMock(return_value="Translated text")

    # First request should succeed
    result = await provider.translate("Hello", "en", "zh")
    assert result == "Translated text"

    # Move time forward by a small amount
    mock_time.move_time(0.1)

    # Second request should fail due to rate limiting
    with pytest.raises(RateLimitError) as exc_info:
        await provider.translate("Hello", "en", "zh")
    assert "Rate limit exceeded" in str(exc_info.value)

    # Move time forward by enough time to get a new token (2 seconds to be safe)
    mock_time.move_time(2.0)

    # Third request should succeed
    result = await provider.translate("Hello", "en", "zh")
    assert result == "Translated text"
