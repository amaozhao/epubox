import pytest
import os
from pathlib import Path
from services.epub_book.pipeline import EPUBHandler, ContentProcessor, TranslationPipeline
from app.core.config import settings

pytestmark = pytest.mark.asyncio

TEST_EPUB_PATH = Path(__file__).parent.parent / "data" / "test.epub"


@pytest.fixture(scope="module")
def test_epub_file():
    """Create a test EPUB file."""
    # Create test EPUB file if it doesn't exist
    os.makedirs(TEST_EPUB_PATH.parent, exist_ok=True)
    if not TEST_EPUB_PATH.exists():
        # Create a minimal EPUB file for testing
        pass
    return TEST_EPUB_PATH


@pytest.fixture
async def epub_handler():
    """Create an EPUBHandler instance."""
    return EPUBHandler()


@pytest.fixture
async def content_processor():
    """Create a ContentProcessor instance."""
    return ContentProcessor()


@pytest.fixture
async def translation_pipeline():
    """Create a TranslationPipeline instance."""
    return TranslationPipeline()


async def test_epub_handler_load(epub_handler, test_epub_file):
    """Test loading an EPUB file."""
    book = await epub_handler.load_epub(test_epub_file)
    assert book is not None
    assert len(book.get_items()) > 0


async def test_epub_handler_extract_text(epub_handler, test_epub_file):
    """Test extracting text from EPUB."""
    book = await epub_handler.load_epub(test_epub_file)
    text = await epub_handler.extract_text(book)
    assert isinstance(text, str)
    assert len(text) > 0


async def test_content_processor_clean_text(content_processor):
    """Test text cleaning functionality."""
    test_text = "<p>Test content with <b>HTML tags</b></p>"
    cleaned_text = await content_processor.clean_text(test_text)
    assert "<p>" not in cleaned_text
    assert "<b>" not in cleaned_text
    assert "Test content with HTML tags" in cleaned_text


async def test_content_processor_segment_text(content_processor):
    """Test text segmentation."""
    test_text = "This is a test sentence. This is another sentence."
    segments = await content_processor.segment_text(test_text)
    assert len(segments) == 2
    assert "This is a test sentence" in segments[0]
    assert "This is another sentence" in segments[1]


async def test_translation_pipeline_translate_text(translation_pipeline):
    """Test text translation."""
    test_text = "Hello, world!"
    translated_text = await translation_pipeline.translate_text(
        test_text, 
        source_lang="en", 
        target_lang="zh"
    )
    assert isinstance(translated_text, str)
    assert translated_text != test_text
    assert len(translated_text) > 0


async def test_full_pipeline_process(translation_pipeline, test_epub_file):
    """Test the complete pipeline process."""
    result = await translation_pipeline.process_epub(
        test_epub_file,
        source_lang="en",
        target_lang="zh"
    )
    assert result is not None
    assert isinstance(result, dict)
    assert "translated_file" in result
    assert "original_text" in result
    assert "translated_text" in result


async def test_pipeline_error_handling(translation_pipeline):
    """Test error handling in the pipeline."""
    with pytest.raises(FileNotFoundError):
        await translation_pipeline.process_epub(
            "nonexistent.epub",
            source_lang="en",
            target_lang="zh"
        )


async def test_pipeline_invalid_language(translation_pipeline, test_epub_file):
    """Test handling of invalid language codes."""
    with pytest.raises(ValueError):
        await translation_pipeline.process_epub(
            test_epub_file,
            source_lang="invalid",
            target_lang="zh"
        )


async def test_pipeline_large_file_handling(translation_pipeline, test_epub_file):
    """Test handling of large files."""
    # Create a large test file
    result = await translation_pipeline.process_epub(
        test_epub_file,
        source_lang="en",
        target_lang="zh",
        chunk_size=1000  # Small chunk size for testing
    )
    assert result is not None
    assert "translated_file" in result
