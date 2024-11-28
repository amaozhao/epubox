"""Unit tests for EPUBTranslator class."""

import os
from unittest.mock import AsyncMock, Mock, patch

import ebooklib
import pytest
from bs4 import BeautifulSoup
from ebooklib import epub

from app.services.translation.base import BaseTranslator
from app.services.translation.epub_translator import EPUBTranslator


@pytest.fixture
def mock_translator():
    """Create a mock translator."""
    translator = Mock(spec=BaseTranslator)
    # Return a valid translation for each input text
    translator.translate_batch = AsyncMock(
        side_effect=lambda texts: ["Translated: " + text for text in texts]
    )
    return translator


@pytest.fixture
def epub_translator(mock_translator):
    """Create an EPUBTranslator instance."""
    return EPUBTranslator(
        translator=mock_translator,
        source_lang="en",
        target_lang="es",
        preserve_formatting=True,
        project_id=1,
    )


@pytest.fixture
def sample_epub_path(tmp_path):
    """Create a sample EPUB file for testing."""
    book = epub.EpubBook()
    book.set_identifier("id123")
    book.set_title("Test Book")
    book.set_language("en")

    # Add chapter
    c1 = epub.EpubHtml(title="Chapter 1", file_name="chap_01.xhtml", lang="en")
    c1.content = """
        <html>
            <head></head>
            <body>
                <h1>Chapter 1</h1>
                <p>Test content</p>
                <img src="image.jpg" />
            </body>
        </html>
    """
    book.add_item(c1)

    # Add navigation files
    nav = epub.EpubNav()
    book.add_item(nav)

    ncx = epub.EpubNcx()
    book.add_item(ncx)

    # Create Table of Contents
    book.toc = [(epub.Section("Test Book"), [c1])]

    # Add to spine
    book.spine = [nav, c1]

    # Save epub
    epub_path = tmp_path / "test.epub"
    epub.write_epub(str(epub_path), book)
    return str(epub_path)


@pytest.mark.asyncio
async def test_translate_epub_success(epub_translator, sample_epub_path, tmp_path):
    """Test successful EPUB translation."""
    output_path = str(tmp_path / "output.epub")

    # Mock progress callback
    progress_callback = Mock()

    # Translate
    stats = await epub_translator.translate_epub(
        sample_epub_path, output_path, progress_callback=progress_callback
    )

    # Verify translation completed
    assert stats["translated_items"] > 0
    assert stats["progress"] == 100
    assert stats["skipped_items"] == 0
    assert os.path.exists(output_path)

    # Verify progress callback was called
    assert progress_callback.call_count > 0


@pytest.mark.asyncio
async def test_translate_epub_input_validation(epub_translator, tmp_path):
    """Test input EPUB validation."""
    # Test non-existent file
    non_existent_path = str(tmp_path / "nonexistent.epub")
    output_path = str(tmp_path / "output.epub")

    # The validation should be handled by _validate_input_epub
    with pytest.raises(ValueError) as exc_info:
        await epub_translator.translate_epub(non_existent_path, output_path)

    # Verify the validation error was recorded
    assert any(
        error["level"] == "critical" and "Input file not found" in error["message"]
        for error in epub_translator.validation_errors
    )


@pytest.mark.asyncio
async def test_translate_epub_content_validation(
    epub_translator, sample_epub_path, tmp_path
):
    """Test content validation during translation."""
    output_path = str(tmp_path / "output.epub")

    # Create EPUB with invalid HTML
    book = epub.EpubBook()
    book.set_identifier("id123")
    c1 = epub.EpubHtml(title="Bad Chapter", file_name="bad.xhtml", lang="en")
    # Use actually broken HTML that will fail validation
    c1.content = """
        <html>
            <body>
                <div>Unclosed div
                <p>Unclosed paragraph
                <a href="test">Broken link
            </body>
        </html>
    """
    book.add_item(c1)

    # Add navigation files
    nav = epub.EpubNav()
    book.add_item(nav)
    ncx = epub.EpubNcx()
    book.add_item(ncx)

    # Create Table of Contents
    book.toc = [(epub.Section("Bad Book"), [c1])]

    # Add to spine
    book.spine = [nav, c1]

    bad_epub_path = str(tmp_path / "bad.epub")
    epub.write_epub(bad_epub_path, book)

    # Attempt translation
    stats = await epub_translator.translate_epub(bad_epub_path, output_path)

    # Debug print to see what validation errors we got
    print("\nValidation Errors:", stats.get("validation_errors", []))

    # Verify validation warnings were logged for malformed HTML
    assert any(
        error["type"] == "content_warning"
        and error["level"] == "warning"
        and "structure was auto-fixed" in error["message"]
        for error in stats.get("validation_errors", [])
    )

    # Verify translation still proceeded (no error-level validation issues)
    assert (
        stats["skipped_items"] == 0
    ), "Items should not be skipped for auto-fixed HTML"
    assert (
        stats["translated_items"] > 0
    ), "Translation should proceed with auto-fixed HTML"


@pytest.mark.asyncio
async def test_translate_epub_with_temp_storage(
    epub_translator, sample_epub_path, tmp_path
):
    """Test translation with temporary storage for recovery."""
    output_path = str(tmp_path / "output.epub")

    # Create temp storage with all required fields
    temp_storage = {
        "stats": {
            "total_items": 1,
            "translated_items": 0,
            "total_words": 10,
            "translated_words": 0,
            "skipped_items": 0,
            "progress": 0,
            "current_item": None,
            "validation_errors": [],
        },
        "translated_items": [],
        "failed_items": [],
    }

    # Translate with temp storage
    stats = await epub_translator.translate_epub(
        sample_epub_path, output_path, temp_storage=temp_storage
    )

    # Verify translation completed and used temp storage
    assert stats["translated_items"] > 0
    assert stats["progress"] == 100
    assert len(temp_storage["translated_items"]) > 0


@pytest.mark.asyncio
async def test_validate_translation_structure(epub_translator):
    """Test translation structure validation."""
    original = """
        <html>
            <body>
                <h1>Title</h1>
                <p>Content</p>
            </body>
        </html>
    """

    translated = """
        <html>
            <body>
                <h1>Título</h1>
                <p>Contenido</p>
            </body>
        </html>
    """

    # Validate translation
    await epub_translator._validate_translation(original, translated)

    # Verify no structure-related errors
    assert not any(
        error["message"] == "HTML structure mismatch between original and translation"
        for error in epub_translator.validation_errors
    )


@pytest.mark.asyncio
async def test_validate_translation_formatting_markers(epub_translator):
    """Test validation of formatting markers preservation."""
    original = """
        <p>Text with __MARKER_1__ and __MARKER_2__</p>
    """

    translated = """
        <p>Texto con __MARKER_1__ y __MARKER_2__</p>
    """

    # Validate translation
    await epub_translator._validate_translation(original, translated)

    # Verify no formatting marker errors
    assert not any(
        error["message"] == "Formatting markers not preserved in translation"
        for error in epub_translator.validation_errors
    )


@pytest.mark.asyncio
async def test_validate_output_epub(epub_translator, sample_epub_path):
    """Test output EPUB validation."""
    book = epub.read_epub(sample_epub_path)

    # Validate output EPUB
    await epub_translator._validate_output_epub(book)

    # Verify no critical errors
    assert not any(
        error["level"] == "critical" for error in epub_translator.validation_errors
    )


@pytest.mark.asyncio
async def test_translate_epub_resource_validation(
    epub_translator, sample_epub_path, tmp_path
):
    """Test validation of EPUB resources."""
    output_path = str(tmp_path / "output.epub")

    # Create EPUB with missing resource reference
    book = epub.EpubBook()
    book.set_identifier("id123")
    c1 = epub.EpubHtml(title="Chapter", file_name="chap.xhtml", lang="en")
    c1.content = """
        <html>
            <body>
                <img src="missing.jpg" />
                <link rel="stylesheet" href="missing.css" />
            </body>
        </html>
    """
    book.add_item(c1)

    # Add navigation files
    nav = epub.EpubNav()
    book.add_item(nav)
    ncx = epub.EpubNcx()
    book.add_item(ncx)

    # Create Table of Contents
    book.toc = [(epub.Section("Resource Test"), [c1])]

    # Add to spine
    book.spine = [nav, c1]

    bad_resource_epub = str(tmp_path / "bad_resource.epub")
    epub.write_epub(bad_resource_epub, book)

    # Translate
    stats = await epub_translator.translate_epub(bad_resource_epub, output_path)

    # Verify resource warnings
    assert any(
        error["message"] == "Missing resources in output EPUB"
        for error in stats.get("validation_errors", [])
    )


@pytest.mark.asyncio
async def test_translate_epub_security_validation(epub_translator, tmp_path):
    """Test security validation during translation."""
    # Create EPUB with script tags
    book = epub.EpubBook()
    book.set_identifier("id123")
    c1 = epub.EpubHtml(title="Chapter", file_name="chap.xhtml", lang="en")
    c1.content = """
        <html>
            <body>
                <script>alert('test');</script>
                <p>Content</p>
            </body>
        </html>
    """
    book.add_item(c1)

    # Add navigation files
    nav = epub.EpubNav()
    book.add_item(nav)
    ncx = epub.EpubNcx()
    book.add_item(ncx)

    # Create Table of Contents
    book.toc = [(epub.Section("Security Test"), [c1])]

    # Add to spine
    book.spine = [nav, c1]

    security_test_epub = str(tmp_path / "security_test.epub")
    epub.write_epub(security_test_epub, book)
    output_path = str(tmp_path / "output.epub")

    # Translate
    stats = await epub_translator.translate_epub(security_test_epub, output_path)

    # Verify security warning
    assert any(
        error["message"] == "Script tags found in content"
        for error in stats.get("validation_errors", [])
    )
