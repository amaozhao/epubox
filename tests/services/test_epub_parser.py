"""Tests for EPUB parser."""

import os
from pathlib import Path
from typing import Dict, List

import ebooklib
import pytest
from bs4 import BeautifulSoup
from ebooklib import epub

from app.models.translation_project import TranslationProject
from app.services.epub import EPUBParser


@pytest.fixture
def parser(sample_epub_path):
    """Create an EPUBParser instance for testing."""
    return EPUBParser(sample_epub_path)


@pytest.fixture
def sample_translation_project():
    """Create a sample translation project for testing."""
    return TranslationProject(
        id=1,
        epub_file_id=1,
        source_language="en",
        target_language="es",
        provider="test_provider",
        status="pending",
    )


def test_epub_parser_initialization(parser):
    """Test that the EPUBParser initializes correctly."""
    assert parser.book is not None
    assert isinstance(parser.book, epub.EpubBook)
    assert len(parser.chapters) > 0
    assert isinstance(parser.metadata, dict)
    assert isinstance(parser.chapters, list)
    assert isinstance(parser.toc_items, list)


def test_metadata_extraction(parser):
    """Test metadata extraction from EPUB."""
    # Standard metadata fields that should be present
    required_fields = ["title", "language"]
    for field in required_fields:
        assert field in parser.metadata, f"Missing required metadata field: {field}"

    # Verify metadata values are strings
    for key, value in parser.metadata.items():
        assert isinstance(value, str), f"Metadata value for {key} should be string"


def test_chapter_analysis(parser):
    """Test chapter content analysis."""
    for chapter in parser.chapters:
        # Verify required fields
        assert "file_name" in chapter
        assert "content_type" in chapter
        assert "word_count" in chapter
        assert "structure" in chapter

        # Verify structure analysis
        structure = chapter["structure"]
        assert isinstance(structure, dict)
        assert "headings" in structure
        assert "paragraphs" in structure
        assert "lists" in structure
        assert "tables" in structure
        assert "images" in structure
        assert "links" in structure

        # Verify content type is valid
        assert chapter["content_type"] in [
            "text",
            "image",
            "image-heavy",
            "table-heavy",
        ]

        # Verify word count
        assert isinstance(chapter["word_count"], int)
        assert chapter["word_count"] >= 0


def test_toc_extraction(parser):
    """Test table of contents extraction."""
    assert len(parser.toc_items) > 0, "EPUB should have table of contents"

    for item in parser.toc_items:
        if item:  # Some items might be None for complex TOC structures
            assert "title" in item
            assert "href" in item
            assert "level" in item
            assert isinstance(item["title"], str)
            assert isinstance(item["href"], str)
            assert isinstance(item["level"], int)


def test_chunk_generation(parser, sample_translation_project):
    """Test translation chunk generation."""
    chunks = parser.generate_translation_chunks(sample_translation_project)

    assert len(chunks) > 0, "Should generate at least one translation chunk"

    for chunk in chunks:
        # Verify chunk properties
        assert chunk.project_id == sample_translation_project.id
        assert chunk.original_content is not None
        assert chunk.content_type is not None
        assert chunk.context is not None
        assert chunk.word_count >= 0

        # Verify content types
        assert chunk.content_type in [
            "text",
            "toc",
            "atomic_h1",
            "atomic_h2",
            "atomic_h3",
            "atomic_h4",
            "atomic_h5",
            "atomic_h6",
            "atomic_title",
            "atomic_figcaption",
            "atomic_caption",
            "atomic_th",
        ]


def test_resource_handling(parser):
    """Test resource (images, etc.) handling."""
    # Test getting resource by href
    has_images = False
    for chapter in parser.chapters:
        if chapter["structure"]["images"] > 0:
            has_images = True
            for image in chapter["images"]:
                resource = parser.get_resource_by_href(image["src"])
                assert resource is not None

                # Verify image properties
                assert image["src"]
                assert "alt" in image
                assert "title" in image

    assert has_images, "Test EPUB should contain at least one image"


def test_content_type_determination(parser):
    """Test content type determination logic."""
    for chapter in parser.chapters:
        structure = chapter["structure"]
        content_type = chapter["content_type"]

        # Verify content type matches structure
        if structure["images"] > 0 and structure["paragraphs"] == 0:
            assert content_type == "image"
        elif structure["images"] > structure["paragraphs"]:
            assert content_type == "image-heavy"
        elif structure["tables"] > structure["paragraphs"]:
            assert content_type == "table-heavy"
        else:
            assert content_type == "text"
