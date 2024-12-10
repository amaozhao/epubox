"""Test Mistral translation provider."""

import time
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import tiktoken
from mistralai import Mistral, models

from app.core.config import settings
from app.translation.errors import ConfigurationError, TranslationError
from app.translation.factory import ProviderFactory
from app.translation.models import LimitType
from app.translation.models import TranslationProvider as ProviderModel
from app.translation.providers.mistral import MistralProvider


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
        retry_count=3,
        retry_delay=1,
        limit_type=LimitType.TOKENS,
        limit_value=2500,
    )


@pytest.fixture
async def provider(provider_model):
    """Create a Mistral provider instance with test configuration."""
    factory = ProviderFactory()
    provider = factory.create_provider(provider_model)
    await provider.initialize()
    return provider


def test_provider_type(provider):
    """Test provider type."""
    assert provider.get_provider_type() == "mistral"


def test_validate_config():
    """Test config validation."""
    provider_model = Mock()
    provider_model.limit_type = LimitType.TOKENS
    provider_model.config = {"api_key": "test"}

    provider = MistralProvider(provider_model)
    assert provider.validate_config({"api_key": "test"}) is True


def test_validate_config_no_api_key():
    """Test config validation with no API key."""
    provider_model = Mock()
    provider_model.limit_type = LimitType.TOKENS
    provider_model.config = {}

    with pytest.raises(ConfigurationError):
        MistralProvider(provider_model)


@pytest.mark.asyncio
@patch("mistralai.Mistral")
async def test_translate_with_html_and_placeholders(mock_mistral, provider):
    """Test translation with HTML tags and placeholders."""
    # Setup mock response
    mock_client = AsyncMock()
    mock_mistral.return_value = mock_client

    test_cases = [
        # 测试HTML标签 - 模型正确保留标签的情况
        {
            "input": "<p>Hello, world!</p>",
            "expected": "<p>你好，世界！</p>",
            "mock_response": "<p>你好，世界！</p>",
            "original_text": "Hello, world!",
            "description": "Basic HTML tag - preserved",
        },
        # 测试带属性的HTML标签 - 模型正确保留标签的情况
        {
            "input": '<div class="test">Hello, world!</div>',
            "expected": '<div class="test">你好，世界！</div>',
            "mock_response": '<div class="test">你好，世界！</div>',
            "original_text": "Hello, world!",
            "description": "HTML tag with attributes - preserved",
        },
        # 测试占位符
        {
            "input": "Hello †0†, world!",
            "expected": "你好 †0†，世界！",
            "mock_response": "你好 †0†，世界！",
            "original_text": "Hello †0†, world!",
            "description": "Text with placeholder",
        },
        # 测试混合HTML和占位符 - 模型正确保留标签的情况
        {
            "input": "<p>Hello †0†, <span>world</span>!</p>",
            "expected": "<p>你好 †0†，<span>世界</span>！</p>",
            "mock_response": "<p>你好 †0†，<span>世界</span>！</p>",
            "original_text": "Hello †0†, world!",
            "description": "Mixed HTML and placeholder - preserved",
        },
    ]

    provider.client = mock_client

    for case in test_cases:
        # Setup mock response for this case
        mock_response = AsyncMock()
        mock_response.choices = [AsyncMock()]
        mock_response.choices[0].message.content = case["mock_response"]
        mock_client.chat.complete_async.return_value = mock_response
        mock_client.chat.complete_async.reset_mock()

        # Perform translation
        result = await provider.translate(
            text=case["input"], source_lang="en", target_lang="zh"
        )

        # Verify the result
        assert result == case["expected"], f"Failed case: {case['description']}"


@pytest.mark.asyncio
@patch("mistralai.Mistral")
async def test_translate_retry_on_error(mock_mistral, provider):
    """Test translation retry mechanism."""
    # Setup mock client
    mock_client = AsyncMock()
    mock_mistral.return_value = mock_client
    provider.client = mock_client

    # Mock API response to fail twice and succeed on third try
    error_responses = [
        models.SDKError("API error", status_code=500),
        models.SDKError("API error", status_code=500),
    ]
    success_response = AsyncMock()
    success_response.choices = [AsyncMock()]
    success_response.choices[0].message.content = "翻译结果"

    mock_client.chat.complete_async.side_effect = [
        *error_responses,
        success_response,
    ]

    # Perform translation
    result = await provider.translate(
        text="Hello, world!", source_lang="en", target_lang="zh"
    )

    # Verify the result
    assert result == "翻译结果"
    assert mock_client.chat.complete_async.call_count == 3


@pytest.mark.asyncio
@patch("mistralai.Mistral")
async def test_translate_max_retries_exceeded(mock_mistral, provider):
    """Test translation when max retries are exceeded."""
    # Setup mock client
    mock_client = AsyncMock()
    mock_mistral.return_value = mock_client
    provider.client = mock_client

    # Mock API to always fail
    mock_client.chat.complete_async.side_effect = models.SDKError(
        "API error", status_code=500
    )

    # Verify that translation raises TranslationError after max retries
    with pytest.raises(TranslationError) as exc_info:
        await provider.translate(
            text="Hello, world!", source_lang="en", target_lang="zh"
        )

    assert "Translation failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_limit_checking(provider):
    """Test text length limit checking."""
    # Test with text under limit
    with patch.object(provider, "_count_tokens", return_value=2000):
        # Should not raise any exception
        await provider.translate(text="Some text", source_lang="en", target_lang="zh")

    # Test with text over limit
    with patch.object(provider, "_count_tokens", return_value=3000):
        with pytest.raises(TranslationError) as exc_info:
            await provider.translate(
                text="Some text", source_lang="en", target_lang="zh"
            )
        assert "exceeds maximum allowed" in str(exc_info.value)


@pytest.mark.asyncio
@patch("mistralai.Mistral")
async def test_translate_mock(mock_mistral, provider):
    """Test translation with mocked API response."""
    # Setup mock client
    mock_client = AsyncMock()
    mock_mistral.return_value = mock_client

    # Setup mock response
    mock_response = AsyncMock()
    mock_response.choices = [AsyncMock()]
    mock_response.choices[0].message.content = "你好，世界！"
    mock_client.chat.complete_async.return_value = mock_response

    provider.client = mock_client

    # Test translation
    result = await provider.translate(
        text="Hello, world!", source_lang="en", target_lang="zh"
    )
    assert result == "你好，世界！"


@pytest.mark.skipif(
    not settings.MISTRAL_API_KEY,
    reason="MISTRAL_API_KEY not set in environment",
)
@pytest.mark.asyncio
async def test_translate_real(provider_model):
    """Test translation with real Mistral API.

    Note: This test requires MISTRAL_API_KEY to be set in configuration.
    It will be skipped if the API key is not set.
    """
    provider = MistralProvider(provider_model)
    result = await provider.translate(
        text="Hello, world!", source_lang="en", target_lang="zh"
    )
    assert result
    assert isinstance(result, str)
    assert len(result) > 0
