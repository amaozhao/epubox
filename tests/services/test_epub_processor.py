"""Test EPUB processor."""

import os
import tempfile
import warnings
from pathlib import Path

import pytest
from ebooklib import epub

from src.services.epub_processor import EPUBProcessor, EPUBProcessorError

# Filter out ebooklib's FutureWarning
warnings.filterwarnings("ignore", category=FutureWarning, module="ebooklib.epub")


@pytest.fixture
def sample_epub():
    """Create a sample EPUB file."""
    # Create a temporary EPUB file
    temp_dir = tempfile.mkdtemp()
    epub_path = os.path.join(temp_dir, "test.epub")

    # Create a basic EPUB file
    book = epub.EpubBook()

    # Set metadata
    book.set_identifier("id123")
    book.set_title("Test Book")
    book.set_language("en")

    # Add a chapter
    c1 = epub.EpubHtml(title="Chapter 1", file_name="chapter1.xhtml", lang="en")
    c1.content = (
        "<html><head></head><body><h1>Chapter 1</h1><p>Test content</p></body></html>"
    )

    # Add chapter to book
    book.add_item(c1)

    # Create table of contents
    book.toc = [epub.Link("chapter1.xhtml", "Chapter 1", "chapter1")]

    # Add navigation files
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # Create spine
    book.spine = ["nav", c1]

    # Write EPUB file
    epub.write_epub(epub_path, book)

    yield epub_path

    # Cleanup
    os.remove(epub_path)
    os.rmdir(temp_dir)


class TestEPUBProcessor:
    """Test EPUB processor."""

    @pytest.mark.asyncio
    async def test_extract_content(self, sample_epub):
        """Test content extraction from EPUB."""
        processor = EPUBProcessor()
        contents = await processor.extract_content(sample_epub)

        # We expect only the chapter content, not the nav
        assert len(contents) == 1

        # Verify content
        content = contents[0]
        assert content["id"] == "chapter1"
        assert content["file_name"] == "chapter1.xhtml"
        assert content["media_type"] == "application/xhtml+xml"
        assert "Test content" in content["content"]

    @pytest.mark.asyncio
    async def test_save_translated_content(self, sample_epub):
        """Test saving translated content back to EPUB."""
        # Create processor with temp directory
        temp_dir = tempfile.mkdtemp()
        processor = EPUBProcessor(temp_dir)

        try:
            # First extract content
            contents = await processor.extract_content(sample_epub)
            assert len(contents) == 1

            # Prepare translated content
            translated_contents = [
                {
                    "id": "chapter1",
                    "file_name": "chapter1.xhtml",
                    "media_type": "application/xhtml+xml",
                    "content": "<html><head></head><body><h1>Chapter 1</h1><p>翻译后的内容</p></body></html>",
                }
            ]

            # Save translated content
            output_path = os.path.join(temp_dir, "translated.epub")
            result_path = await processor.save_translated_content(
                sample_epub, translated_contents, output_path
            )

            # Verify the output file exists
            assert os.path.exists(result_path)

            # Read the translated book and verify content
            book = epub.read_epub(result_path)
            items = list(book.get_items())
            chapter = next(
                item
                for item in items
                if isinstance(item, epub.EpubHtml)
                and not isinstance(item, epub.EpubNav)
            )
            assert "翻译后的内容" in chapter.get_content().decode("utf-8")

        finally:
            # Cleanup
            await processor.cleanup()
            if os.path.exists(temp_dir):
                import shutil

                shutil.rmtree(temp_dir)

    @pytest.mark.asyncio
    async def test_extract_content_file_not_found(self):
        """Test extract_content with non-existent file."""
        processor = EPUBProcessor()
        with pytest.raises(EPUBProcessorError) as exc_info:
            await processor.extract_content("/nonexistent/path/book.epub")
        assert "EPUB file not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_save_translated_content_invalid_input(self):
        """Test save_translated_content with invalid input."""
        processor = EPUBProcessor()
        with pytest.raises(EPUBProcessorError) as exc_info:
            await processor.save_translated_content(
                "/nonexistent/path/book.epub", [], "/output/path/book.epub"
            )
        assert "Source EPUB file not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_save_translated_content_error(self):
        """Test error handling during content saving."""
        processor = EPUBProcessor()

        # Provide invalid translated content
        invalid_contents = [
            {
                "id": "invalid_id",
                "file_name": "invalid.xhtml",
                "media_type": "application/xhtml+xml",
                "content": "Invalid content",
            }
        ]

        # Save translated content
        output_path = os.path.join(tempfile.mkdtemp(), "translated.epub")
        with pytest.raises(EPUBProcessorError) as exc_info:
            await processor.save_translated_content(
                sample_epub, invalid_contents, output_path
            )
        assert "Failed to save translated content" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_cleanup_nonexistent_dir(self):
        """Test cleanup with non-existent directory."""
        # Create a processor with a non-existent directory path
        processor = EPUBProcessor("/nonexistent/temp/dir")

        # Attempt cleanup - should raise EPUBProcessorError
        with pytest.raises(EPUBProcessorError) as exc_info:
            await processor.cleanup()

        assert "Temporary directory not found" in str(exc_info.value)
