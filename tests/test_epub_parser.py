import os
import shutil
import tempfile
from pathlib import Path

import pytest

from app.services.epub_parser import EPUBParser

TEST_EPUB_PATH = Path(__file__).parent / "test.epub"


@pytest.fixture
def sample_epub_path():
    """Create a temporary copy of the test EPUB file for testing."""
    # Verify the source file exists and has content
    assert TEST_EPUB_PATH.exists(), f"Test EPUB file not found at {TEST_EPUB_PATH}"
    assert (
        TEST_EPUB_PATH.stat().st_size > 0
    ), f"Test EPUB file is empty at {TEST_EPUB_PATH}"

    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp_file:
        # Copy the test EPUB file to a temporary location
        shutil.copy2(TEST_EPUB_PATH, tmp_file.name)

        # Verify the copied file
        tmp_size = os.path.getsize(tmp_file.name)
        print(f"\nDebug: Copied EPUB file size: {tmp_size} bytes")
        print(f"Debug: Temporary file path: {tmp_file.name}")

        yield tmp_file.name
        # Cleanup
        os.unlink(tmp_file.name)


def test_epub_parser_initialization(sample_epub_path):
    """Test that the EPUBParser initializes correctly."""
    print(f"\nDebug: Testing with EPUB file: {sample_epub_path}")
    parser = EPUBParser(sample_epub_path)
    print(f"Debug: Number of chapters found: {len(parser.chapters)}")
    print(f"Debug: Metadata: {parser.metadata}")

    assert parser.book is not None
    assert len(parser.chapters) > 0

    # Print chapter information
    for i, chapter in enumerate(parser.chapters):
        print(f"\nDebug: Chapter {i + 1}:")
        print(f"  ID: {chapter.get('id', 'No ID')}")
        print(f"  Name: {chapter.get('name', 'No Name')}")
        print(f"  Content length: {len(chapter.get('content', ''))}")
        print(f"  Text content length: {len(chapter.get('text_content', ''))}")
        print(f"  Has text: {chapter.get('has_text', False)}")

        # Basic content validation
        assert chapter.get("id"), "Chapter should have an ID"
        assert chapter.get("name"), "Chapter should have a name"
        assert chapter.get("content"), "Chapter should have content"
        assert "text_content" in chapter, "Chapter should have text_content field"
        assert "has_text" in chapter, "Chapter should have has_text field"

    # Verify at least one chapter has text content
    assert any(
        chapter.get("has_text") for chapter in parser.chapters
    ), "EPUB should have at least one chapter with text content"


def test_get_translatable_content(sample_epub_path):
    """Test extracting translatable content from chapters."""
    parser = EPUBParser(sample_epub_path)
    assert len(parser.chapters) > 0, "No chapters found in EPUB file"

    # Track if we found any translatable content
    found_translatable_content = False
    total_segments = 0

    # Test each chapter
    for chapter in parser.chapters:
        chapter_id = chapter["id"]
        print(f"\nDebug: Testing chapter ID: {chapter_id}")
        print(f"Debug: Chapter name: {chapter['name']}")
        print(f"Debug: Chapter content: {chapter['content'][:200]}")

        # Get translatable content
        content = parser.get_translatable_content(chapter_id)
        total_segments += len(content)
        if len(content) > 0:
            found_translatable_content = True
            print(f"Found {len(content)} translatable segments")
            print("First segment:", content[0])

    # Verify that we found some translatable content across all chapters
    assert found_translatable_content, (
        f"No translatable content found in any chapter. "
        f"Total chapters: {len(parser.chapters)}, "
        f"Total segments: {total_segments}"
    )


def test_get_all_translatable_content(sample_epub_path):
    """Test getting all translatable content from the EPUB file."""
    parser = EPUBParser(sample_epub_path)
    all_content = parser.get_all_translatable_content()

    print("\nDebug: EPUB Content Analysis:")
    print(f"Total chapters: {parser.chapter_count}")
    print(f"Chapters with text: {parser.text_chapter_count}")

    total_segments = 0
    for chapter_id, content in all_content.items():
        print(f"Chapter {chapter_id}: {len(content)} segments")
        total_segments += len(content)
    print(f"Total translatable segments: {total_segments}")

    # Verify we got content for all chapters that have text content
    chapters_with_content = len(all_content)
    print(f"Chapters with extracted content: {chapters_with_content}")

    # Some chapters might be images or have no translatable content
    assert (
        chapters_with_content <= parser.chapter_count
    ), "Cannot have more chapters with content than total chapters"

    # If we found any content, we should have at least one segment
    if chapters_with_content > 0:
        assert (
            total_segments > 0
        ), "Chapters with content should have at least one segment"

    # Each chapter in the result should have at least one segment
    for content in all_content.values():
        assert (
            len(content) > 0
        ), "Each chapter in results should have at least one segment"

    # Each segment should have the required fields
    for chapter_id, segments in all_content.items():
        for segment in segments:
            assert "text" in segment, "Segment should have text"
            assert "context_path" in segment, "Segment should have context path"
            assert "chapter_id" in segment, "Segment should have chapter ID"
            assert (
                segment["chapter_id"] == chapter_id
            ), "Segment chapter ID should match"


def test_excluded_tags(sample_epub_path):
    """Test that excluded tags are properly handled."""
    parser = EPUBParser(sample_epub_path)
    if not parser.chapters:
        pytest.fail("No chapters found in EPUB file")

    # Find a chapter that has text content
    text_chapter = None
    for chapter in parser.chapters:
        if chapter.get("has_text"):
            text_chapter = chapter
            break

    if not text_chapter:
        pytest.skip("No chapters with text content found in EPUB file")

    chapter_id = text_chapter["id"]

    # Test with default excluded tags (code, pre, script, style)
    default_content = parser.get_translatable_content(chapter_id)

    if default_content:
        print("Debug: Default content first few segments:")
        for segment in default_content[:5]:
            print(f"  Text: {segment['text'][:100]}...")
            print(f"     Context: {segment['context_path']}")

    # Test with minimal excluded tags
    minimal_excluded = ["script", "style"]
    minimal_content = parser.get_translatable_content(
        chapter_id, excluded_tags=minimal_excluded
    )

    if minimal_content:
        print("Debug: Minimal exclusion content first few segments:")
        for segment in minimal_content[:5]:
            print(f"  Text: {segment['text'][:100]}...")
            print(f"     Context: {segment['context_path']}")

    # Test with extensive excluded tags
    extensive_tags = [
        "p",
        "div",
        "section",
        "article",
        "main",
        "header",
        "footer",
        "h1",
        "h2",
        "h3",
    ]
    extensive_content = parser.get_translatable_content(
        chapter_id, excluded_tags=extensive_tags
    )
    print(
        f"\nDebug: Content with extensive excluded tags: {len(extensive_content)} segments"
    )
    if extensive_content:
        print("Debug: Extensive exclusion content first few segments:")
        for segment in extensive_content[:5]:
            print(f"  Text: {segment['text'][:100]}...")
            print(f"     Context: {segment['context_path']}")

    # Basic assertions about content differences
    minimal_texts = {segment["text"].strip() for segment in minimal_content}
    extensive_texts = {segment["text"].strip() for segment in extensive_content}

    # Only run assertions if we found translatable content
    if not minimal_texts and not extensive_texts:
        pytest.skip("No translatable content found in chapter")

    # The minimal exclusion should have more segments than extensive exclusion
    assert len(minimal_texts) > len(extensive_texts), (
        f"Minimal exclusion ({len(minimal_texts)} segments) should have more content "
        f"than extensive exclusion ({len(extensive_texts)} segments)"
    )


def test_epub_properties(sample_epub_path):
    """Test the EPUB parser property methods."""
    parser = EPUBParser(sample_epub_path)

    # Test basic properties
    assert isinstance(
        parser.chapter_count, int
    ), "chapter_count should return an integer"
    assert isinstance(
        parser.text_chapter_count, int
    ), "text_chapter_count should return an integer"
    assert isinstance(
        parser.has_text_content, bool
    ), "has_text_content should return a boolean"

    # Test relationships between properties
    assert (
        parser.text_chapter_count <= parser.chapter_count
    ), "Text chapters should not exceed total chapters"

    if parser.has_text_content:
        assert (
            parser.text_chapter_count > 0
        ), "If has_text_content is True, should have at least one text chapter"
    else:
        assert (
            parser.text_chapter_count == 0
        ), "If has_text_content is False, should have no text chapters"

    # Test chapter names
    chapter_names = parser.get_chapter_names()
    assert isinstance(chapter_names, list), "get_chapter_names should return a list"
    assert (
        len(chapter_names) == parser.chapter_count
    ), "Number of chapter names should match chapter count"
    assert all(
        isinstance(name, str) for name in chapter_names
    ), "All chapter names should be strings"

    # Test text chapters
    text_chapters = parser.get_text_chapters()
    assert isinstance(text_chapters, list), "get_text_chapters should return a list"
    assert (
        len(text_chapters) == parser.text_chapter_count
    ), "Number of text chapters should match text_chapter_count"
    assert all(
        chapter.get("has_text") for chapter in text_chapters
    ), "All chapters returned by get_text_chapters should have text"


def test_chapter_types(sample_epub_path):
    """Test identification of different chapter types."""
    parser = EPUBParser(sample_epub_path)

    # Get chapter statistics
    stats = parser.get_chapter_stats()
    print("\nDebug: EPUB Chapter Statistics:")
    for key, value in stats.items():
        print(f"  {key}: {value}")

    # Basic sanity checks
    assert stats["total_chapters"] == len(parser.chapters)
    assert stats["total_chapters"] == (
        stats["cover_pages"]
        + stats["image_only_pages"]
        + stats["content_pages"]
        + stats["unclassified_pages"]
    ), "Total chapters should equal sum of all chapter types including unclassified"

    # Check cover pages
    for chapter in parser.cover_chapters:
        assert chapter["is_cover"]
        assert chapter["image_count"] == 1
        assert not chapter["has_text"]

    # Check image-only pages (excluding covers)
    for chapter in parser.image_only_chapters:
        assert chapter["is_image_only"]
        assert not chapter["is_cover"]  # Should not be counted as both
        assert chapter["image_count"] > 0
        assert not chapter["has_text"]

    # Check content pages
    for chapter in parser.content_chapters:
        assert not chapter["is_cover"]
        assert not chapter["is_image_only"]
        assert chapter["content_type"] == "content"

    # Verify that text chapters are a subset of content chapters
    text_chapter_ids = {c["id"] for c in parser.get_text_chapters()}
    content_chapter_ids = {c["id"] for c in parser.content_chapters}
    assert text_chapter_ids.issubset(content_chapter_ids)

    # If we have unclassified pages, log them for investigation
    if stats["unclassified_pages"] > 0:
        print("\nUnclassified chapters found:")
        for chapter in parser.chapters:
            if (
                not chapter.get("is_cover")
                and not chapter.get("is_image_only")
                and chapter.get("content_type") != "content"
            ):
                print(f"  - ID: {chapter['id']}")
                print(f"    Name: {chapter['name']}")
                print(f"    Type: {chapter.get('content_type')}")
                print(f"    Has text: {chapter.get('has_text')}")
                print(f"    Image count: {chapter.get('image_count')}")
