"""Test cases for translation services."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.services.translation.translator import (
    DeepLTranslator,
    GoogleTranslator,
    MistralTranslator,
    OpenAITranslator,
    TranslationError,
    TranslationProvider,
    TranslationService,
    create_translator,
)


@pytest.fixture
def mock_openai_response():
    """Mock OpenAI API response."""

    class MockChoice:
        def __init__(self, text):
            self.message = MagicMock()
            self.message.content = text

    class MockResponse:
        def __init__(self, text):
            self.choices = [MockChoice(text)]

    return MockResponse


@pytest.fixture
def mock_httpx_response():
    """Mock httpx response."""

    class MockResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json_data = json_data

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("Mock error", request=None, response=self)

        def json(self):
            return self._json_data

    return MockResponse


class TestOpenAITranslator:
    """Test OpenAI translation service."""

    @pytest.mark.asyncio
    async def test_translate_batch_success(self, mock_openai_response):
        """Test successful batch translation."""
        texts = ["Hello", "World"]
        translated_texts = ["Bonjour", "Monde"]
        translated_response = "\n---\n".join(translated_texts)

        with patch("openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.return_value = mock_openai_response(
                translated_response
            )
            mock_openai.return_value = mock_client

            translator = OpenAITranslator(api_key="test-key")
            result = await translator.translate_batch(texts, "en", "fr")

            assert result == translated_texts
            mock_client.chat.completions.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_translate_batch_api_error(self):
        """Test translation with API error."""
        with patch("openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.side_effect = Exception("API Error")
            mock_openai.return_value = mock_client

            translator = OpenAITranslator(api_key="test-key")
            with pytest.raises(Exception) as exc_info:
                await translator.translate_batch(["Hello"], "en", "fr")

            assert "OpenAI translation failed" in str(exc_info.value)


class TestGoogleTranslator:
    """Test Google translation service."""

    @pytest.mark.asyncio
    async def test_translate_batch_success(self, mock_httpx_response):
        """Test successful batch translation."""
        texts = ["Hello", "World"]
        translated_texts = ["Bonjour", "Monde"]
        mock_response = {
            "translations": [{"translatedText": text} for text in translated_texts]
        }

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post.return_value = (
                mock_httpx_response(200, mock_response)
            )

            translator = GoogleTranslator(api_key="test-key", project_id="test-project")
            result = await translator.translate_batch(texts, "en", "fr")

            assert result == translated_texts

    @pytest.mark.asyncio
    async def test_translate_batch_api_error(self, mock_httpx_response):
        """Test translation with API error."""
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post.return_value = (
                mock_httpx_response(400, {"error": "API Error"})
            )

            translator = GoogleTranslator(api_key="test-key", project_id="test-project")
            with pytest.raises(Exception) as exc_info:
                await translator.translate_batch(["Hello"], "en", "fr")

            assert "Google translation failed" in str(exc_info.value)


class TestMistralTranslator:
    """Test Mistral translation service."""

    @pytest.mark.asyncio
    async def test_translate_batch_success(self, mock_httpx_response):
        """Test successful batch translation."""
        texts = ["Hello", "World"]
        translated_texts = ["Bonjour", "Monde"]
        translated_response = "\n---\n".join(translated_texts)
        mock_response = {"choices": [{"message": {"content": translated_response}}]}

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post.return_value = (
                mock_httpx_response(200, mock_response)
            )

            translator = MistralTranslator(api_key="test-key")
            result = await translator.translate_batch(texts, "en", "fr")

            assert result == translated_texts

    @pytest.mark.asyncio
    async def test_translate_batch_api_error(self, mock_httpx_response):
        """Test translation with API error."""
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post.return_value = (
                mock_httpx_response(400, {"error": "API Error"})
            )

            translator = MistralTranslator(api_key="test-key")
            with pytest.raises(Exception) as exc_info:
                await translator.translate_batch(["Hello"], "en", "fr")

            assert "Mistral translation failed" in str(exc_info.value)


class TestDeepLTranslator:
    """Test DeepL translation service."""

    @pytest.mark.asyncio
    async def test_translate_batch_success(self, mock_httpx_response):
        """Test successful batch translation."""
        texts = ["Hello", "World"]
        translated_texts = ["Bonjour", "Monde"]
        mock_response = {"translations": [{"text": text} for text in translated_texts]}

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post.return_value = (
                mock_httpx_response(200, mock_response)
            )

            translator = DeepLTranslator(api_key="test-key")
            result = await translator.translate_batch(texts, "en", "fr")

            assert result == translated_texts

    @pytest.mark.asyncio
    async def test_translate_batch_api_error(self, mock_httpx_response):
        """Test translation with API error."""
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post.return_value = (
                mock_httpx_response(400, {"error": "API Error"})
            )

            translator = DeepLTranslator(api_key="test-key")
            with pytest.raises(Exception) as exc_info:
                await translator.translate_batch(["Hello"], "en", "fr")

            assert "DeepL translation failed" in str(exc_info.value)


class TestTranslatorFactory:
    """Test translator factory function."""

    def test_create_openai_translator(self):
        """Test creating OpenAI translator."""
        translator = create_translator(
            provider=TranslationProvider.OPENAI,
            api_key="test-key",
            model="gpt-3.5-turbo",
            temperature=0.5,
        )
        assert isinstance(translator, OpenAITranslator)
        assert translator.api_key == "test-key"
        assert translator.model == "gpt-3.5-turbo"
        assert translator.temperature == 0.5

    def test_create_google_translator(self):
        """Test creating Google translator."""
        translator = create_translator(
            provider=TranslationProvider.GOOGLE,
            api_key="test-key",
            project_id="test-project",
        )
        assert isinstance(translator, GoogleTranslator)
        assert translator.api_key == "test-key"
        assert translator.project_id == "test-project"

    def test_create_mistral_translator(self):
        """Test creating Mistral translator."""
        translator = create_translator(
            provider=TranslationProvider.MISTRAL,
            api_key="test-key",
            model="mistral-medium",
            temperature=0.3,
        )
        assert isinstance(translator, MistralTranslator)
        assert translator.api_key == "test-key"
        assert translator.model == "mistral-medium"
        assert translator.temperature == 0.3

    def test_create_deepl_translator(self):
        """Test creating DeepL translator."""
        translator = create_translator(
            provider=TranslationProvider.DEEPL, api_key="test-key", is_pro=True
        )
        assert isinstance(translator, DeepLTranslator)
        assert translator.api_key == "test-key"
        assert translator.is_pro is True

    def test_create_invalid_provider(self):
        """Test creating translator with invalid provider."""
        with pytest.raises(TranslationError) as exc_info:
            create_translator(provider="invalid", api_key="test-key")
        assert isinstance(exc_info.value, TranslationError)
