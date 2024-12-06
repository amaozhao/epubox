"""Test Mistral translation provider."""

import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import tiktoken
from mistralai import Mistral, models

from app.core.config import settings
from app.translation.errors import ConfigurationError, RateLimitError, TranslationError
from app.translation.models import LimitType
from app.translation.models import TranslationProvider as ProviderModel
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
async def test_rate_limit_handling(provider):
    """Test rate limit handling."""
    # 确保初始状态没有限流
    assert provider._last_error_time is None
    assert provider._rate_limit_reset is None

    # 模拟限流错误
    await provider._handle_rate_limit()

    # 验证限流状态被正确设置
    assert provider._last_error_time is not None
    assert provider._rate_limit_reset is not None
    assert provider._rate_limit_reset > provider._last_error_time
    assert provider._rate_limit_reset - provider._last_error_time == timedelta(hours=1)

    # 验证在限流期间的请求会被拒绝
    with pytest.raises(RateLimitError) as exc_info:
        await provider.check_rate_limit()
    assert "Rate limit exceeded" in str(exc_info.value)

    # 模拟时间流逝，限流应该解除
    provider._rate_limit_reset = datetime.now() - timedelta(minutes=1)
    await provider.check_rate_limit()  # 不应该抛出异常


@pytest.mark.asyncio
@patch("mistralai.Mistral")
async def test_translate_rate_limit_handling(mock_mistral, provider):
    """Test translation with rate limit error handling."""
    # Setup mock client
    mock_client = AsyncMock()
    mock_mistral.return_value = mock_client
    provider.client = mock_client

    # 模拟限流错误
    error_message = (
        "resource_exhausted: rate limit exceeded for model; try again in about an hour"
    )
    mock_client.chat.complete_async.side_effect = models.SDKError(
        message=error_message, status_code=429
    )

    # 验证翻译请求会触发限流处理
    with pytest.raises(RateLimitError) as exc_info:
        await provider.translate(
            text="Hello, world!", source_lang="en", target_lang="zh"
        )

    assert "Rate limit exceeded" in str(exc_info.value)
    assert provider._rate_limit_reset is not None
    assert provider._rate_limit_reset > datetime.now()


@pytest.mark.asyncio
@patch("mistralai.Mistral")
@patch("app.translation.providers.mistral.BeautifulSoup")
async def test_translate_with_html_and_placeholders(mock_soup, mock_mistral, provider):
    """Test translation with HTML tags and placeholders."""
    # Setup mock response
    mock_client = AsyncMock()
    mock_mistral.return_value = mock_client

    # Setup BeautifulSoup mock
    mock_soup_instance = MagicMock()
    mock_soup.return_value = mock_soup_instance

    test_cases = [
        # 测试HTML标签 - 模型正确保留标签的情况
        {
            "input": "<p>Hello, world!</p>",
            "expected": "<p>你好，世界！</p>",
            "mock_response": "<p>你好，世界！</p>",
            "original_text": "Hello, world!",
            "description": "Basic HTML tag - preserved",
            "needs_beautifulsoup": False,
        },
        # 测试带属性的HTML标签 - 模型正确保留标签的情况
        {
            "input": '<div class="test">Hello, world!</div>',
            "expected": '<div class="test">你好，世界！</div>',
            "mock_response": '<div class="test">你好，世界！</div>',
            "original_text": "Hello, world!",
            "description": "HTML tag with attributes - preserved",
            "needs_beautifulsoup": False,
        },
        # 测试占位符
        {
            "input": "Hello †0†, world!",
            "expected": "你好 †0†，世界！",
            "mock_response": "你好 †0†，世界！",
            "original_text": "Hello †0†, world!",
            "description": "Text with placeholder",
            "needs_beautifulsoup": False,
        },
        # 测试混合HTML和占位符 - 模型正确保留标签的情况
        {
            "input": "<p>Hello †0†, <span>world</span>!</p>",
            "expected": "<p>你好 †0†，<span>世界</span>！</p>",
            "mock_response": "<p>你好 †0†，<span>世界</span>！</p>",
            "original_text": "Hello †0†, world!",
            "description": "Mixed HTML and placeholder - preserved",
            "needs_beautifulsoup": False,
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

        # Setup BeautifulSoup mock for this case
        mock_soup_instance.get_text.return_value = case["original_text"]
        mock_soup.reset_mock()
        mock_soup_instance.get_text.reset_mock()

        # Test translation
        result = await provider.translate(
            text=case["input"], source_lang="en", target_lang="zh"
        )

        # Verify result
        assert result == case["expected"], f"Failed test case: {case['description']}"
        mock_client.chat.complete_async.assert_called_once()

        # Verify BeautifulSoup usage
        if case["needs_beautifulsoup"]:
            mock_soup.assert_called_with(case["input"], "html.parser")
            mock_soup_instance.get_text.assert_called_once()
        else:
            mock_soup.assert_not_called()
            mock_soup_instance.get_text.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not settings.MISTRAL_API_KEY, reason="MISTRAL_API_KEY not set in configuration"
)
async def test_translate_with_html_and_placeholders_real(provider_model):
    """Test translation with HTML tags and placeholders using real API.

    Note: This test requires MISTRAL_API_KEY to be set in configuration.
    It will be skipped if the API key is not set.
    """
    provider = MistralProvider(provider_model)

    test_cases = [{"input": "<p>Hello, world!</p>", "description": "Basic HTML tag"}]

    try:
        for case in test_cases:
            try:
                result = await provider.translate(
                    text=case["input"], source_lang="en", target_lang="zh"
                )

                # 验证结果
                assert result, f"Empty result for case: {case['description']}"
                assert len(result) > 0

                # 验证HTML标签和占位符的保留
                if "<" in case["input"]:
                    assert (
                        "<" in result and ">" in result
                    ), f"HTML tags not preserved in case: {case['description']}"
                if "†" in case["input"]:
                    assert (
                        "†" in result
                    ), f"Placeholder not preserved in case: {case['description']}"

                print(f"\nTest case: {case['description']}")
                print(f"Input: {case['input']}")
                print(f"Output: {result}")

            except RateLimitError as e:
                print(f"\nRate limit hit for case: {case['description']}")
                print(f"Error: {str(e)}")
                pytest.skip(f"Rate limit exceeded, skipping test: {str(e)}")

    except Exception as e:
        pytest.fail(f"Test failed with error: {str(e)}")


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

    except RateLimitError as e:
        pytest.skip(f"Rate limit exceeded, skipping test: {str(e)}")
    except Exception as e:
        pytest.fail(f"Translation failed: {str(e)}")
