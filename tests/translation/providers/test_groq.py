"""Test Groq translation provider."""

import time
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import tenacity
import tiktoken
from groq import AsyncGroq

from app.core.config import settings
from app.db.models import LimitType
from app.db.models import TranslationProvider as ProviderModel
from app.translation.errors import ConfigurationError, TranslationError
from app.translation.factory import ProviderFactory
from app.translation.providers.groq import GroqProvider


@pytest.fixture
def provider_model():
    """Create a mock provider model for testing."""
    return ProviderModel(
        id=1,
        name="Groq Test",
        provider_type="groq",
        is_default=True,
        enabled=True,
        config={"api_key": settings.GROQ_API_KEY, "model": "llama3-8b-8192"},
        retry_count=3,
        retry_delay=1,
        limit_type=LimitType.TOKENS,
        limit_value=2500,
    )


@pytest.fixture
async def provider(provider_model):
    """Create a Groq provider instance with test configuration."""
    factory = ProviderFactory()
    provider = factory.create_provider(provider_model)
    await provider.initialize()
    return provider


def test_provider_type(provider):
    """Test provider type."""
    assert provider.get_provider_type() == "groq"


def test_validate_config():
    """Test config validation."""
    provider_model = ProviderModel(
        id=1,
        name="Groq Test",
        provider_type="groq",
        is_default=True,
        enabled=True,
        config={"api_key": "test-key"},
        retry_count=3,
        retry_delay=1,
        limit_type=LimitType.TOKENS,
        limit_value=2500,
    )
    provider = GroqProvider(provider_model)
    assert provider.validate_config({"api_key": "test-key"}) is True


def test_validate_config_no_api_key():
    """Test config validation with no API key."""
    provider_model = ProviderModel(
        id=1,
        name="Groq Test",
        provider_type="groq",
        is_default=True,
        enabled=True,
        config={},
        retry_count=3,
        retry_delay=1,
        limit_type=LimitType.TOKENS,
        limit_value=2500,
    )
    with pytest.raises(ConfigurationError, match="Groq API key is required"):
        provider = GroqProvider(provider_model)
        provider.validate_config({})


@pytest.mark.asyncio
async def test_translate_with_html_and_placeholders(provider):
    """Test translation with HTML tags and placeholders."""
    test_text = "<p>Hello {name}!</p>"
    mock_response = AsyncMock()
    mock_response.choices = [
        MagicMock(message=MagicMock(content="<p>Bonjour {name}!</p>"))
    ]

    provider.client.chat.completions.create = AsyncMock(return_value=mock_response)
    result = await provider.translate(test_text, "en", "fr")
    assert result == "<p>Bonjour {name}!</p>"


@pytest.mark.asyncio
async def test_translate_retry_on_error(provider):
    """Test translation retry mechanism."""
    test_text = "Hello"
    mock_response = AsyncMock()
    mock_response.choices = [MagicMock(message=MagicMock(content="Bonjour"))]

    provider.client.chat.completions.create = AsyncMock(
        side_effect=[Exception("API Error"), mock_response]
    )
    result = await provider.translate(test_text, "en", "fr")
    assert result == "Bonjour"


@pytest.mark.asyncio
async def test_translate_max_retries_exceeded(provider):
    """Test translation when max retries are exceeded."""
    test_text = "Hello"

    provider.client.chat.completions.create = AsyncMock(
        side_effect=Exception("API Error")
    )
    with pytest.raises(tenacity.RetryError) as exc_info:
        await provider.translate(test_text, "en", "fr")
    assert isinstance(exc_info.value.last_attempt.exception(), TranslationError)


@pytest.mark.asyncio
async def test_limit_checking(provider):
    """Test text length limit checking."""
    # 创建一个刚好在限制范围内的文本
    text = "a" * 1000  # 这个长度应该在限制范围内
    mock_response = AsyncMock()
    mock_response.choices = [MagicMock(message=MagicMock(content="test response"))]

    provider.client.chat.completions.create = AsyncMock(return_value=mock_response)
    result = await provider.translate(text, "en", "fr")
    assert result == "test response"


@pytest.mark.asyncio
async def test_translate_mock(provider):
    """Test translation with mocked API response."""
    test_text = "Hello world"
    mock_response = AsyncMock()
    mock_response.choices = [MagicMock(message=MagicMock(content="Bonjour le monde"))]

    provider.client.chat.completions.create = AsyncMock(return_value=mock_response)
    result = await provider.translate(test_text, "en", "fr")
    assert result == "Bonjour le monde"


@pytest.mark.skipif(
    not hasattr(settings, "GROQ_API_KEY") or not settings.GROQ_API_KEY,
    reason="GROQ_API_KEY not set",
)
@pytest.mark.asyncio
async def test_translate_real(provider_model):
    """Test translation with real Groq API.

    Note: This test requires GROQ_API_KEY to be set in configuration.
    It will be skipped if the API key is not set.
    """
    provider = GroqProvider(provider_model)
    result = await provider.translate("Hello world", "en", "fr")
    assert result
    assert isinstance(result, str)
    assert len(result) > 0
