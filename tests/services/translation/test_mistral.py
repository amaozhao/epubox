"""Tests for Mistral-based translator implementation."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mistralai import Mistral
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.translation.mistral import MistralTranslationError, MistralTranslator


@pytest.fixture
async def translator(db: AsyncSession):
    """Create a MistralTranslator instance for testing."""
    translator = MistralTranslator(
        api_key="test_key",
        source_lang="en",
        target_lang="es",
        model="mistral-large-latest",
    )
    yield translator
    await translator.close()


@pytest.fixture
def mock_chat_response():
    """Create a mock chat response."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message = {"content": "¡Hola mundo!"}
    return response


@pytest.mark.asyncio
async def test_translate_text_success(translator, mock_chat_response):
    """Test successful text translation."""
    with patch.object(
        translator.client.chat,
        "complete_async",
        AsyncMock(return_value=mock_chat_response),
    ):
        result = await translator.translate_text("Hello world!")
        assert result == "¡Hola mundo!"


@pytest.mark.asyncio
async def test_translate_text_api_error(translator):
    """Test translation with API error."""
    with patch.object(
        translator.client.chat,
        "complete_async",
        AsyncMock(side_effect=Exception("API Error")),
    ):
        try:
            await translator.translate_text("Hello world!")
            pytest.fail("Expected MistralTranslationError")
        except MistralTranslationError as e:
            assert "API Error" in str(e)


@pytest.mark.asyncio
async def test_translate_text_timeout(translator):
    """Test translation with timeout error."""
    with patch.object(
        translator.client.chat,
        "complete_async",
        AsyncMock(side_effect=asyncio.TimeoutError()),
    ):
        try:
            await translator.translate_text("Hello world!")
            pytest.fail("Expected MistralTranslationError")
        except MistralTranslationError as e:
            assert "TimeoutError" in str(e)


@pytest.mark.asyncio
async def test_translate_batch_success(translator, mock_chat_response):
    """Test successful batch translation."""
    texts = ["Hello", "World", "Test"]
    with patch.object(
        translator.client.chat,
        "complete_async",
        AsyncMock(return_value=mock_chat_response),
    ):
        results = await translator.translate_batch(texts)
        assert len(results) == 3
        assert all(result == "¡Hola mundo!" for result in results)


@pytest.mark.asyncio
async def test_translate_batch_with_errors(translator, mock_chat_response):
    """Test batch translation with some failures."""
    texts = ["Hello", "Error", "World"]

    async def mock_translate(model, messages, **kwargs):
        if "Error" in messages[1]["content"]:
            raise Exception("API Error")
        return mock_chat_response

    with patch.object(
        translator.client.chat, "complete_async", side_effect=mock_translate
    ):
        results = await translator.translate_batch(texts)
        assert len(results) == 3
        assert results[0] == "¡Hola mundo!"
        assert results[1] == ""  # Failed translation should return empty string
        assert results[2] == "¡Hola mundo!"


@pytest.mark.asyncio
async def test_translate_batch_chunking(translator, mock_chat_response):
    """Test batch translation with chunking."""
    texts = ["Text"] * (MistralTranslator.BATCH_CHUNK_SIZE + 5)
    with patch.object(
        translator.client.chat,
        "complete_async",
        AsyncMock(return_value=mock_chat_response),
    ):
        results = await translator.translate_batch(texts)
        assert len(results) == len(texts)
        assert all(result == "¡Hola mundo!" for result in results)


@pytest.mark.asyncio
async def test_detect_language_success(translator, mock_chat_response):
    """Test successful language detection."""
    mock_chat_response.choices[0].message = {"content": "en"}
    with patch.object(
        translator.client.chat,
        "complete_async",
        AsyncMock(return_value=mock_chat_response),
    ):
        result = await translator.detect_language("Hello world!")
        assert result == "en"


@pytest.mark.asyncio
async def test_detect_language_error(translator):
    """Test language detection with error."""
    with patch.object(
        translator.client.chat,
        "complete_async",
        AsyncMock(side_effect=Exception("API Error")),
    ):
        try:
            await translator.detect_language("Hello world!")
            pytest.fail("Expected MistralTranslationError")
        except MistralTranslationError as e:
            assert "API Error" in str(e)


@pytest.mark.asyncio
async def test_get_supported_languages(translator):
    """Test getting supported languages."""
    languages = translator.get_supported_languages()
    assert isinstance(languages, list)
    assert len(languages) > 0
    assert all(isinstance(lang, str) for lang in languages)
    assert "en" in languages
    assert "es" in languages


@pytest.mark.asyncio
async def test_semaphore_limiting(translator, mock_chat_response):
    """Test concurrent request limiting."""
    texts = ["Text"] * (MistralTranslator.MAX_CONCURRENT_REQUESTS * 2)
    with patch.object(
        translator.client.chat,
        "complete_async",
        AsyncMock(return_value=mock_chat_response),
    ):
        results = await translator.translate_batch(texts)
        assert len(results) == len(texts)
        assert all(result == "¡Hola mundo!" for result in results)


@pytest.mark.asyncio
async def test_prepare_messages(translator):
    """Test message preparation for different tasks."""
    # Test translation messages
    trans_messages = translator._prepare_messages("Hello", "translate")
    assert len(trans_messages) == 2
    assert isinstance(trans_messages[0], dict)
    assert trans_messages[0]["role"] == "system"
    assert "translate" in trans_messages[0]["content"].lower()
    assert trans_messages[1]["content"] == "Hello"

    # Test detection messages
    detect_messages = translator._prepare_messages("Hello", "detect")
    assert len(detect_messages) == 2
    assert isinstance(detect_messages[0], dict)
    assert detect_messages[0]["role"] == "system"
    assert "detect" in detect_messages[0]["content"].lower()
    assert detect_messages[1]["content"] == "Hello"


@pytest.mark.asyncio
async def test_make_request_retry(translator, mock_chat_response):
    """Test request retry mechanism."""
    # Mock chat to fail twice then succeed
    mock_chat = AsyncMock(
        side_effect=[
            Exception("First failure"),
            Exception("Second failure"),
            mock_chat_response,
        ]
    )

    with patch.object(translator.client.chat, "complete_async", new=mock_chat):
        result = await translator.translate_text("Hello world!")
        assert result == "¡Hola mundo!"
        assert mock_chat.call_count == 3


@pytest.mark.asyncio
async def test_close(translator):
    """Test close method."""
    await translator.close()  # Should do nothing
