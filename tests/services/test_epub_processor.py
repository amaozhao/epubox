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

    @pytest.mark.asyncio
    async def test_html_content_preservation(self, sample_epub):
        """Test HTML structure and style preservation."""
        processor = EPUBProcessor()

        # Create content with HTML structure and style
        styled_content = {
            "id": "chapter1",
            "file_name": "chapter1.xhtml",
            "media_type": "application/xhtml+xml",
            "content": """
            <html>
                <head>
                    <style type="text/css">
                        p { color: blue; }
                    </style>
                </head>
                <body>
                    <h1 class="title">Chapter 1</h1>
                    <div class="content">
                        <p style="font-weight: bold;">Styled content</p>
                        <p class="special">Special paragraph</p>
                    </div>
                </body>
            </html>
            """,
        }

        # Save content and verify
        temp_dir = tempfile.mkdtemp()
        output_path = os.path.join(temp_dir, "styled.epub")

        try:
            result_path = await processor.save_translated_content(
                sample_epub, [styled_content], output_path
            )

            # Verify the HTML structure is preserved
            book = epub.read_epub(result_path)
            chapter = next(
                item
                for item in book.get_items()
                if isinstance(item, epub.EpubHtml)
                and not isinstance(item, epub.EpubNav)
            )
            content = chapter.get_content().decode("utf-8")

            # 验证内容和样式是否保留
            assert '<h1 class="title">Chapter 1</h1>' in content
            assert '<div class="content">' in content
            assert '<p style="font-weight: bold;">Styled content</p>' in content
            assert '<p class="special">Special paragraph</p>' in content

        finally:
            import shutil

            shutil.rmtree(temp_dir)

    @pytest.mark.asyncio
    async def test_multiple_chapters(self, sample_epub):
        """Test processing EPUB with multiple chapters."""
        processor = EPUBProcessor()

        # Create a multi-chapter book
        book = epub.EpubBook()
        book.set_identifier("id123")
        book.set_title("Multi Chapter Test")
        book.set_language("en")

        # Add chapters
        chapters = []
        for i in range(3):
            c = epub.EpubHtml(
                title=f"Chapter {i+1}", file_name=f"chapter{i+1}.xhtml", lang="en"
            )
            c.content = (
                f"<html><body><h1>Chapter {i+1}</h1><p>Content {i+1}</p></body></html>"
            )
            book.add_item(c)
            chapters.append(c)

        # Add navigation
        book.toc = [(epub.Section("Chapters"), chapters)]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = ["nav"] + chapters

        # Save the book
        temp_dir = tempfile.mkdtemp()
        multi_epub_path = os.path.join(temp_dir, "multi.epub")
        epub.write_epub(multi_epub_path, book)

        try:
            # Test extraction
            contents = await processor.extract_content(multi_epub_path)
            assert len(contents) == 3

            # Test translation
            translated_contents = [
                {
                    "id": f"chapter{i+1}",
                    "file_name": f"chapter{i+1}.xhtml",
                    "media_type": "application/xhtml+xml",
                    "content": f"<html><body><h1>章节 {i+1}</h1><p>内容 {i+1}</p></body></html>",
                }
                for i in range(3)
            ]

            output_path = os.path.join(temp_dir, "translated_multi.epub")
            result_path = await processor.save_translated_content(
                multi_epub_path, translated_contents, output_path
            )

            # Verify all chapters are translated
            book = epub.read_epub(result_path)
            translated_chapters = [
                item
                for item in book.get_items()
                if isinstance(item, epub.EpubHtml)
                and not isinstance(item, epub.EpubNav)
            ]

            assert len(translated_chapters) == 3
            for i, chapter in enumerate(translated_chapters):
                content = chapter.get_content().decode("utf-8")
                assert f"章节 {i+1}" in content
                assert f"内容 {i+1}" in content

        finally:
            import shutil

            shutil.rmtree(temp_dir)

    @pytest.mark.asyncio
    async def test_empty_epub_handling(self):
        """Test handling of empty EPUB file."""
        processor = EPUBProcessor()

        # Create an empty EPUB
        book = epub.EpubBook()
        book.set_identifier("empty123")
        book.set_title("Empty Book")
        book.set_language("en")

        # Add only navigation
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = ["nav"]

        # Save the empty book
        temp_dir = tempfile.mkdtemp()
        empty_epub_path = os.path.join(temp_dir, "empty.epub")
        epub.write_epub(empty_epub_path, book)

        try:
            # Test extraction
            contents = await processor.extract_content(empty_epub_path)
            assert len(contents) == 0  # Should be empty

            # Test translation of empty book
            output_path = os.path.join(temp_dir, "translated_empty.epub")
            with pytest.raises(EPUBProcessorError) as exc_info:
                await processor.save_translated_content(
                    empty_epub_path, [], output_path
                )
            assert "No matching content found to update in EPUB" in str(exc_info.value)

        finally:
            import shutil

            shutil.rmtree(temp_dir)

    @pytest.mark.asyncio
    async def test_temp_directory_management(self):
        """Test temporary directory creation and cleanup."""
        # Test with custom temp directory
        custom_temp_dir = tempfile.mkdtemp()
        processor = EPUBProcessor(custom_temp_dir)

        try:
            # Verify directory exists
            assert os.path.exists(custom_temp_dir)

            # Create some test files
            test_file = os.path.join(custom_temp_dir, "test.txt")
            with open(test_file, "w") as f:
                f.write("test content")

            # Test cleanup
            await processor.cleanup()

            # Verify cleanup
            assert not os.path.exists(test_file)
            assert not os.path.exists(custom_temp_dir)

        except:
            # Ensure cleanup in case of test failure
            if os.path.exists(custom_temp_dir):
                import shutil

                shutil.rmtree(custom_temp_dir)
            raise

    @pytest.mark.asyncio
    async def test_malformed_epub_handling(self):
        """Test handling of malformed EPUB file."""
        processor = EPUBProcessor()

        # Create a malformed EPUB file
        temp_dir = tempfile.mkdtemp()
        malformed_epub = os.path.join(temp_dir, "malformed.epub")

        try:
            # Create a file that looks like EPUB but isn't
            with open(malformed_epub, "wb") as f:
                f.write(b"This is not a valid EPUB file")

            # Test extraction
            with pytest.raises(EPUBProcessorError) as exc_info:
                await processor.extract_content(malformed_epub)
            assert "Failed to extract EPUB content" in str(exc_info.value)

            # Test translation
            with pytest.raises(EPUBProcessorError) as exc_info:
                await processor.save_translated_content(
                    malformed_epub,
                    [{"id": "test", "file_name": "test.xhtml", "content": "test"}],
                    os.path.join(temp_dir, "output.epub"),
                )
            assert "Failed to read EPUB" in str(exc_info.value)

        finally:
            import shutil

            shutil.rmtree(temp_dir)
