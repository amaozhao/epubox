"""Tests for the translation service."""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.epub.translator import TranslationError, TranslationService


@pytest.fixture
def translation_service():
    """Create a translation service instance for testing."""
    return TranslationService(api_key="test_key")


@pytest.mark.asyncio
async def test_translate_simple_text(translation_service):
    """Test translation of simple text without markers."""
    with patch.object(
        translation_service.client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value.choices = [
            AsyncMock(message=AsyncMock(content="Bonjour le monde"))
        ]

        chunks = [
            {"content": "Hello world", "content_type": "translatable", "node_index": 0}
        ]

        translated = await translation_service.translate_chunks(chunks, "en", "fr")
        assert translated[0]["translated_content"] == "Bonjour le monde"


@pytest.mark.asyncio
async def test_translate_with_markers(translation_service):
    """Test translation of text with markers."""
    with patch.object(
        translation_service.client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value.choices = [
            AsyncMock(message=AsyncMock(content="Bonjour __TAG_0__ le monde __TAG_1__"))
        ]

        chunks = [
            {
                "content": "Hello __TAG_0__ world __TAG_1__",
                "content_type": "translatable",
                "node_index": 0,
            }
        ]

        translated = await translation_service.translate_chunks(chunks, "en", "fr")
        assert "__TAG_0__" in translated[0]["translated_content"]
        assert "__TAG_1__" in translated[0]["translated_content"]


@pytest.mark.asyncio
async def test_untranslatable_content(translation_service):
    """Test handling of untranslatable content."""
    chunks = [
        {
            "content": '<code>print("Hello")</code>',
            "content_type": "untranslatable",
            "node_index": 0,
        }
    ]

    translated = await translation_service.translate_chunks(chunks, "en", "fr")
    assert translated[0]["content"] == '<code>print("Hello")</code>'
    assert "translated_content" not in translated[0]


@pytest.mark.asyncio
async def test_mixed_content(translation_service):
    """Test translation of mixed translatable and untranslatable content."""
    responses = [
        AsyncMock(choices=[AsyncMock(message=AsyncMock(content="Bonjour le monde"))]),
        AsyncMock(choices=[AsyncMock(message=AsyncMock(content="Au revoir!"))]),
    ]
    mock_create = AsyncMock(side_effect=responses)

    with patch.object(
        translation_service.client.chat.completions, "create", mock_create
    ):
        chunks = [
            {
                "content": '<code>print("Hello")</code>',
                "content_type": "untranslatable",
                "node_index": 0,
            },
            {"content": "Hello world", "content_type": "translatable", "node_index": 1},
            {"content": "Goodbye!", "content_type": "translatable", "node_index": 2},
        ]

        translated = await translation_service.translate_chunks(chunks, "en", "fr")
        assert len(translated) == 3
        assert translated[0]["content"] == '<code>print("Hello")</code>'
        assert translated[1]["translated_content"] == "Bonjour le monde"
        assert translated[2]["translated_content"] == "Au revoir!"


@pytest.mark.asyncio
async def test_api_error_handling(translation_service):
    """Test handling of API errors."""
    with patch.object(
        translation_service.client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.side_effect = Exception("API Error")

        chunks = [
            {"content": "Hello world", "content_type": "translatable", "node_index": 0}
        ]

        with pytest.raises(TranslationError):
            await translation_service.translate_chunks(chunks, "en", "fr")


@pytest.mark.asyncio
async def test_marker_validation(translation_service):
    """Test validation of markers in translated text."""
    original = "Hello __TAG_0__ world __TAG_1__"
    translated = "Bonjour __TAG_0__ le monde __TAG_1__"
    assert await translation_service.validate_translation(original, translated)

    # Test with missing marker
    bad_translation = "Bonjour __TAG_0__ le monde"
    assert not await translation_service.validate_translation(original, bad_translation)
