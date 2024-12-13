"""Test Google translation provider."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import tenacity

from app.db.models import LimitType
from app.db.models import TranslationProvider as ProviderModel
from app.translation.errors import ConfigurationError, TranslationError
from app.translation.factory import ProviderFactory
from app.translation.providers.google import GoogleProvider


@pytest.fixture
def provider_model():
    """Create a mock provider model for testing."""
    return ProviderModel(
        id=1,
        name="Google Test",
        provider_type="google",
        is_default=True,
        enabled=True,
        config={},
        retry_count=3,
        retry_delay=1,
        limit_type=LimitType.TOKENS,
        limit_value=2500,
    )


@pytest.fixture
async def provider(provider_model):
    """Create a provider instance for testing."""
    provider = GoogleProvider(provider_model)
    await provider._initialize()
    yield provider
    await provider._cleanup()


def test_provider_type(provider):
    """Test provider type."""
    assert provider.get_provider_type() == "google"


def test_validate_config():
    """Test config validation."""
    provider = GoogleProvider(
        ProviderModel(
            id=1,
            name="test",
            provider_type="google",
            config={},
            limit_type=LimitType.TOKENS,
        )
    )
    assert provider.validate_config({}) is True


@pytest.mark.asyncio
async def test_translate_with_html_and_placeholders(provider):
    """Test translation with HTML tags and placeholders."""
    test_text = "<p>Hello {name}!</p>"
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(return_value={"sentences": [{"trans": "<p>你好 {name}！</p>"}]})

    # Mock httpx client's post method
    provider.client.post = AsyncMock(return_value=mock_response)
    result = await provider.translate(test_text, "en", "zh")
    assert result == "<p>你好 {name}！</p>"


@pytest.mark.asyncio
async def test_translate_retry_on_error(provider):
    """Test translation retry mechanism."""
    test_text = "Hello"
    mock_success = AsyncMock()
    mock_success.status_code = 200
    mock_success.json = MagicMock(return_value={"sentences": [{"trans": "你好"}]})

    # First request fails, second succeeds
    provider.client.post = AsyncMock(
        side_effect=[httpx.HTTPError("Test error"), mock_success]
    )
    result = await provider.translate(test_text, "en", "zh")
    assert result == "你好"


@pytest.mark.asyncio
async def test_translate_max_retries_exceeded(provider):
    """Test translation when max retries are exceeded."""
    test_text = "Hello"
    # All requests fail
    provider.client.post = AsyncMock(
        side_effect=httpx.HTTPError("Test error")
    )
    with pytest.raises(tenacity.RetryError):
        await provider.translate(test_text, "en", "zh")


@pytest.mark.asyncio
async def test_translate_text_cleaning(provider):
    """Test text cleaning functionality."""
    test_text = "您好，这是一个覆盖测试，法学硕士。"
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(return_value={"sentences": [{"trans": test_text}]})

    provider.client.post = AsyncMock(return_value=mock_response)
    result = await provider.translate(test_text, "zh", "zh")
    assert result == "你好，这是一个封面测试，LLM。"
