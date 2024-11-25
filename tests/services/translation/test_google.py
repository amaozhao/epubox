"""Test cases for Google Translate implementation."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.translation.google import GoogleTranslationError, GoogleTranslator


@pytest.fixture
def translator():
    """Create a GoogleTranslator instance for testing."""
    return GoogleTranslator(api_key="dummy_key", source_lang="en", target_lang="zh")


@pytest.mark.asyncio
async def test_init(translator):
    """Test translator initialization."""
    assert translator.source_lang == "en"
    assert translator.target_lang == "zh"
    assert translator.api_url == "https://translate.google.com/translate_a/single"
    assert isinstance(translator._client, httpx.AsyncClient)


@pytest.mark.asyncio
async def test_translate_text_success(translator):
    """Test successful text translation."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "sentences": [{"trans": "你好"}, {"trans": "世界"}]
    }

    mock_post = AsyncMock(return_value=mock_response)

    with patch.object(translator._client, "post", mock_post):
        result = await translator.translate_text("Hello World")
        assert result == "你好世界"

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert translator.api_url in args
        assert kwargs["data"]["q"] == "Hello World"


@pytest.mark.asyncio
async def test_translate_text_http_error(translator):
    """Test translation with HTTP error."""
    mock_response = MagicMock()
    mock_response.status_code = 500

    mock_post = AsyncMock(return_value=mock_response)

    with patch.object(translator._client, "post", mock_post):
        # Test that it returns the input text with fallback replacements
        result = await translator.translate_text("您好")
        assert result == "你好"  # 您 should be replaced with 你


@pytest.mark.asyncio
async def test_translate_text_network_error(translator):
    """Test translation with network error."""
    mock_post = AsyncMock(side_effect=httpx.RequestError("Network error"))

    with patch.object(translator._client, "post", mock_post):
        # Should return original text with replacements after retries
        result = await translator.translate_text("您好")
        assert result == "你好"


@pytest.mark.asyncio
async def test_translate_text_timeout(translator):
    """Test translation with timeout error."""
    mock_post = AsyncMock(side_effect=asyncio.TimeoutError())

    with patch.object(translator._client, "post", mock_post):
        # Should return original text with replacements after retries
        result = await translator.translate_text("覆盖测试")
        assert result == "封面测试"


@pytest.mark.asyncio
async def test_translate_batch(translator):
    """Test batch translation."""
    texts = ["Hello", "World"]
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"sentences": [{"trans": "你好"}]}

    mock_post = AsyncMock(return_value=mock_response)

    with patch.object(translator._client, "post", mock_post):
        results = await translator.translate_batch(texts)
        assert len(results) == 2
        assert all(isinstance(result, str) for result in results)
        assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_detect_language_success(translator):
    """Test successful language detection."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"src": "en"}

    mock_post = AsyncMock(return_value=mock_response)

    with patch.object(translator._client, "post", mock_post):
        result = await translator.detect_language("Hello")
        assert result == "en"

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert translator.api_url in args
        assert kwargs["data"]["q"] == "Hello"


@pytest.mark.asyncio
async def test_detect_language_http_error(translator):
    """Test language detection with HTTP error."""
    mock_response = MagicMock()
    mock_response.status_code = 500

    mock_post = AsyncMock(return_value=mock_response)

    with patch.object(translator._client, "post", mock_post):
        # Should return 'en' as fallback
        result = await translator.detect_language("Hello")
        assert result == "en"


@pytest.mark.asyncio
async def test_detect_language_missing_src(translator):
    """Test language detection with missing src in response."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {}  # Missing 'src' field

    mock_post = AsyncMock(return_value=mock_response)

    with patch.object(translator._client, "post", mock_post):
        # Should return 'en' as fallback
        result = await translator.detect_language("Hello")
        assert result == "en"


@pytest.mark.asyncio
async def test_detect_language_network_error(translator):
    """Test language detection with network error."""
    mock_post = AsyncMock(side_effect=httpx.RequestError("Network error"))

    with patch.object(translator._client, "post", mock_post):
        # Should return 'en' as fallback
        result = await translator.detect_language("Hello")
        assert result == "en"


def test_get_supported_languages(translator):
    """Test getting supported languages."""
    languages = translator.get_supported_languages()
    assert isinstance(languages, list)
    assert all(isinstance(lang, str) for lang in languages)
    assert "en" in languages
    assert "zh" in languages
    assert "auto" in languages


@pytest.mark.asyncio
async def test_close(translator):
    """Test client cleanup."""
    mock_aclose = AsyncMock()

    with patch.object(translator._client, "aclose", mock_aclose):
        await translator.close()
        mock_aclose.assert_called_once()


@pytest.mark.asyncio
async def test_translate_text_empty_input(translator):
    """Test translation with empty input."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"sentences": []}

    mock_post = AsyncMock(return_value=mock_response)

    with patch.object(translator._client, "post", mock_post):
        result = await translator.translate_text("")
        assert result == ""


@pytest.mark.asyncio
async def test_translate_text_special_characters(translator):
    """Test translation with special characters."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"sentences": [{"trans": "特殊字符：@#$%"}]}

    mock_post = AsyncMock(return_value=mock_response)

    with patch.object(translator._client, "post", mock_post):
        result = await translator.translate_text("Special chars: @#$%")
        assert result == "特殊字符：@#$%"


@pytest.mark.asyncio
async def test_translate_text_long_input(translator):
    """Test translation with long input text."""
    long_text = "Hello " * 1000
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"sentences": [{"trans": "你好 " * 1000}]}

    mock_post = AsyncMock(return_value=mock_response)

    with patch.object(translator._client, "post", mock_post):
        result = await translator.translate_text(long_text)
        assert len(result) > 1000  # Ensure long text is handled
