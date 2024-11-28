"""Tests for HTML splitter."""

import pytest
from bs4 import BeautifulSoup

from app.services.epub import HTMLSplitter


@pytest.fixture
def html_splitter():
    return HTMLSplitter()


def test_simple_text_extraction(html_splitter):
    """Test extraction of simple text content."""
    html = "<p>Simple text</p>"
    parts = html_splitter.split_content(html, "test.html", 1)

    translatable = [p for p in parts if p["content_type"] == "translatable"]
    assert len(translatable) == 1
    assert translatable[0]["original_content"].strip() == "Simple text"

    # Test translation
    translatable[0]["content"] = "Translated text"
    reassembled = html_splitter.reassemble_content(parts, html)
    assert "Translated text" in reassembled
    assert "<p>Translated text</p>" in reassembled


def test_untranslatable_content(html_splitter):
    """Test handling of untranslatable content."""
    html = """
    <div>
        <p>Text before</p>
        <pre>def code(): pass</pre>
        <p>Text after</p>
    </div>
    """
    parts = html_splitter.split_content(html, "test.html", 1)

    translatable = [p for p in parts if p["content_type"] == "translatable"]
    untranslatable = [p for p in parts if p["content_type"] == "untranslatable"]

    # Check content separation
    assert len(translatable) == 2
    assert len(untranslatable) == 1

    # Verify text content
    text_content = {p["original_content"].strip() for p in translatable}
    assert text_content == {"Text before", "Text after"}

    # Verify code preservation
    code_content = untranslatable[0]["original_content"]
    assert "def code(): pass" in code_content

    # Test translation
    for part in translatable:
        part["content"] = f"Trans_{part['original_content'].strip()}"

    reassembled = html_splitter.reassemble_content(parts, html)
    assert "Trans_Text before" in reassembled
    assert "Trans_Text after" in reassembled
    assert "def code(): pass" in reassembled


def test_nested_content(html_splitter):
    """Test handling of nested content."""
    html = """
    <div>
        <p>First <em>Second <strong>Third</strong></em></p>
    </div>
    """
    parts = html_splitter.split_content(html, "test.html", 1)

    translatable = [p for p in parts if p["content_type"] == "translatable"]

    # Verify text nodes are found and inline tags are kept intact
    assert len(translatable) == 1
    text = translatable[0]["content"]
    assert text.startswith("First")
    assert "__TAG_" in text

    # Verify marker replacement
    markers = translatable[0]["markers"]
    assert len(markers) > 0
    marker_key = next(iter(markers.keys()))
    assert "<em>" in markers[marker_key]
    assert "<strong>" in markers[marker_key]


def test_mixed_content(html_splitter):
    """Test handling of mixed translatable and untranslatable content."""
    html = """
    <div>
        <p>Text with <code>some_code()</code> and <em>emphasis</em></p>
    </div>
    """
    parts = html_splitter.split_content(html, "test.html", 1)

    translatable = [p for p in parts if p["content_type"] == "translatable"]
    untranslatable = [p for p in parts if p["content_type"] == "untranslatable"]

    # Verify content separation
    assert len(translatable) == 1
    assert len(untranslatable) == 1

    # Verify content
    text = translatable[0]["content"]
    assert text.startswith("Text with")
    assert "__UNTRANSLATABLE_" in text
    assert "__TAG_" in text


def test_attribute_preservation(html_splitter):
    """Test preservation of HTML attributes."""
    html = '<div class="test"><p id="para">Text</p></div>'
    parts = html_splitter.split_content(html, "test.html", 1)

    # Test translation
    for part in parts:
        if part["content_type"] == "translatable":
            part["content"] = "Translated"

    reassembled = html_splitter.reassemble_content(parts, html)
    soup = BeautifulSoup(reassembled, "html.parser")

    # Verify attributes
    div = soup.div
    assert div["class"] == ["test"]
    p = soup.p
    assert p["id"] == "para"
    assert p.string == "Translated"


def test_whitespace_handling(html_splitter):
    """Test handling of whitespace and formatting."""
    html = """
    <div>
        <p>  Text with  spaces  </p>
        <p>Multiple
           lines</p>
    </div>
    """
    parts = html_splitter.split_content(html, "test.html", 1)

    translatable = [p for p in parts if p["content_type"] == "translatable"]

    # Verify text extraction
    text_content = {p["content"].strip() for p in translatable}
    assert "Text with spaces" in text_content
    assert "Multiple lines" in text_content


def test_sentence_preservation(html_splitter):
    """Test that complete sentences are preserved during translation."""
    html = "<p>This is a <strong>test case</strong>.</p>"
    parts = html_splitter.split_content(html, "test.html", 1)

    # Should only have one translatable part (the complete sentence)
    translatable = [p for p in parts if p["content_type"] == "translatable"]
    assert len(translatable) == 1

    # The text should contain a marker for the strong tag
    text = translatable[0]["content"]
    assert "This is a" in text
    assert "." in text
    assert "__TAG_" in text

    # Verify marker mapping
    markers = translatable[0]["markers"]
    assert len(markers) == 1
    marker_key = next(iter(markers.keys()))
    assert "<strong>" in markers[marker_key]
