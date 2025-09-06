from unittest.mock import AsyncMock, patch

import httpx
import pytest

from engine.services.google import GoogleTranslator


@pytest.mark.asyncio
class TestGoogleTranslator:
    """Test suite for GoogleTranslator class."""

    async def test_init(self):
        """Test initialization of GoogleTranslator class."""
        translator = GoogleTranslator()
        assert translator.api_url == (
            "https://translate.google.com/translate_a/single"
            "?client=it&dt=qca&dt=t&dt=rmt&dt=bd&dt=rms&dt=sos&dt=md&dt=gt"
            "&dt=ld&dt=ss&dt=ex&otf=2&dj=1&hl=en&ie=UTF-8&oe=UTF-8&sl=auto&tl=zh-CN"
        )
        assert translator.headers == {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "GoogleTranslate/6.29.59279 (iPhone; iOS 15.4; en; iPhone14,2)",
        }
        assert isinstance(translator.client, httpx.AsyncClient)

    async def test_translate_real_request(self):
        """Test translation with a real HTTP request."""
        translator = GoogleTranslator()
        result = await translator.translate("Hello World")
        # Close the client to avoid resource leaks
        await translator.client.aclose()
        # Google Translate typically returns "你好，世界！" or similar for "Hello World"
        assert "您好" in result  # Flexible assertion to account for API variations
        assert "世界" in result
        assert "\n\n\n" not in result  # Ensure newlines are normalized

    @patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    async def test_translate_failed_request(self, mock_post):
        """Test translation when HTTP request fails."""
        translator = GoogleTranslator()
        mock_response = httpx.Response(status_code=400)
        mock_post.return_value = mock_response

        result = await translator.translate("Hello World")
        mock_post.assert_called_once_with(translator.api_url, headers=translator.headers, data={"q": "Hello%20World"})
        assert result == "Hello World"  # Returns original text on failure

    @patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    async def test_translate_empty_text(self, mock_post):
        """Test translation with empty input text."""
        translator = GoogleTranslator()
        mock_response = httpx.Response(status_code=200, json={"sentences": []})
        mock_post.return_value = mock_response

        result = await translator.translate("")
        mock_post.assert_called_once_with(translator.api_url, headers=translator.headers, data={"q": ""})
        assert result == ""

    @patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    async def test_translate_multiple_newlines(self, mock_post):
        """Test translation with multiple newlines in response."""
        translator = GoogleTranslator()
        mock_response = httpx.Response(
            status_code=200, json={"sentences": [{"trans": "你好\n\n\n世界", "orig": "Hello\n\n\nWorld"}]}
        )
        mock_post.return_value = mock_response

        result = await translator.translate("Hello\n\n\nWorld")
        mock_post.assert_called_once_with(
            translator.api_url, headers=translator.headers, data={"q": "Hello%0A%0A%0AWorld"}
        )
        assert result == "你好\n\n世界"  # Multiple newlines reduced to two

    @patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    async def test_translate_missing_trans_key(self, mock_post):
        """Test translation when 'trans' key is missing in some sentences."""
        translator = GoogleTranslator()
        mock_response = httpx.Response(
            status_code=200,
            json={
                "sentences": [
                    {"trans": "你好", "orig": "Hello"},
                    {"orig": "World"},  # Missing 'trans' key
                ]
            },
        )
        mock_post.return_value = mock_response

        result = await translator.translate("Hello World")
        mock_post.assert_called_once_with(translator.api_url, headers=translator.headers, data={"q": "Hello%20World"})
        assert result == "你好"  # Only includes sentences with 'trans'
