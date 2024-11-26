import pytest
from bs4 import BeautifulSoup

from app.services.html_splitter import HTMLSplitter


@pytest.fixture
def html_splitter():
    return HTMLSplitter()


def test_basic_html(html_splitter):
    """Test basic HTML translation."""
    html = "<p>Hello <strong>World</strong>!</p>"
    parts = html_splitter.split_content(html)

    # Should find three translatable parts
    translatable = [p for p in parts if p["type"] == "translatable"]
    assert len(translatable) == 3
    assert any("Hello" in p["content"] for p in translatable)
    assert any("World" in p["content"] for p in translatable)
    assert any("!" in p["content"] for p in translatable)


def test_untranslatable_tags(html_splitter):
    """Test that untranslatable tags are preserved."""
    html = "<div>Text <code>some_code()</code> more text</div>"
    parts = html_splitter.split_content(html)

    # Check untranslatable parts
    untranslatable = [p for p in parts if p["type"] == "untranslatable"]
    assert len(untranslatable) == 1
    assert "some_code()" in untranslatable[0]["content"]

    # Check translatable parts
    translatable = [p for p in parts if p["type"] == "translatable"]
    assert len(translatable) == 2
    assert any("Text" in p["content"] for p in translatable)
    assert any("more text" in p["content"] for p in translatable)


def test_whitespace_preservation(html_splitter):
    """Test that whitespace is preserved correctly."""
    html = "<div>  Text with spaces  </div>"
    parts = html_splitter.split_content(html)
    translatable = [p for p in parts if p["type"] == "translatable"]
    assert len(translatable) == 1
    assert translatable[0]["content"] == "  Text with spaces  "


def test_complex_xhtml(html_splitter):
    """Test handling of complex XHTML with nested structures."""
    with open("tests/test.xhtml", "r", encoding="utf-8") as f:
        complex_html = f.read()

    # Split content
    parts = html_splitter.split_content(complex_html)

    # Basic structure checks
    translatable_parts = [p for p in parts if p["type"] == "translatable"]
    untranslatable_parts = [p for p in parts if p["type"] == "untranslatable"]
    assert len(translatable_parts) > 0, "Should find translatable parts"
    assert len(untranslatable_parts) > 0, "Should find untranslatable parts"

    # Check specific content is found and can be translated
    title_found = False
    for part in translatable_parts:
        if "Why Retrieval Augmented Generation?" in part["content"]:
            title_found = True
            # Test translation
            part["content"] = "Test Translation"
            break
    assert title_found, "Should find the title text"

    # Reassemble and verify
    reassembled = html_splitter.reassemble_content(parts, complex_html)

    # Parse both versions for comparison
    original_soup = BeautifulSoup(complex_html, "html.parser")
    reassembled_soup = BeautifulSoup(reassembled, "html.parser")

    # Structure preservation checks
    assert len(original_soup.find_all()) == len(reassembled_soup.find_all())

    # Check untranslatable elements
    for tag in html_splitter.untranslatable_tags:
        orig_count = len(original_soup.find_all(tag))
        new_count = len(reassembled_soup.find_all(tag))
        assert orig_count == new_count, f"Mismatch in {tag} tag count"

    # Verify translation was applied
    assert "Test Translation" in reassembled

    # Check koboSpan preservation
    orig_spans = original_soup.find_all("span", class_="koboSpan")
    new_spans = reassembled_soup.find_all("span", class_="koboSpan")
    assert len(orig_spans) == len(new_spans), "koboSpan count should match"

    # Verify namespace preservation
    assert 'xmlns="http://www.w3.org/1999/xhtml"' in reassembled
    assert 'xmlns:epub="http://www.idpf.org/2007/ops"' in reassembled


def test_nested_translatable(html_splitter):
    """Test handling of nested translatable content."""
    html = """
    <div class="wrapper">
        <p>Start 
            <span class="nested">Middle
                <em>Emphasized</em>
            End</span>
        </p>
    </div>
    """
    parts = html_splitter.split_content(html)
    translatable = [p for p in parts if p["type"] == "translatable"]

    # Check all text parts are found
    found_text = set()
    for part in translatable:
        text = part["content"].strip()
        if text:
            found_text.add(text)

    assert "Start" in found_text
    assert "Middle" in found_text
    assert "Emphasized" in found_text
    assert "End" in found_text


def test_attribute_preservation(html_splitter):
    """Test that HTML attributes are preserved."""
    html = '<div class="test" id="main"><span data-custom="value">Text</span></div>'
    parts = html_splitter.split_content(html)

    # Translate the text
    for part in parts:
        if part["type"] == "translatable" and "Text" in part["content"]:
            part["content"] = "Translated"

    reassembled = html_splitter.reassemble_content(parts, html)
    soup = BeautifulSoup(reassembled, "html.parser")

    # Check attributes are preserved
    div = soup.find("div")
    assert div["class"] == ["test"]
    assert div["id"] == "main"

    span = soup.find("span")
    assert span["data-custom"] == "value"


def test_deep_nested_translatable(html_splitter):
    """Test deeply nested translatable content with multiple levels."""
    html = """
    <div class="level1">
        <div class="level2">First
            <p class="level3">Second
                <span class="level4">Third
                    <em class="level5">Fourth
                        <strong class="level6">Fifth</strong>
                    </em>
                </span>
            </p>
        </div>
    </div>
    """
    parts = html_splitter.split_content(html)
    translatable = [p for p in parts if p["type"] == "translatable"]

    # Check all nested text is found
    found_text = {p["content"].strip() for p in translatable if p["content"].strip()}
    expected_text = {"First", "Second", "Third", "Fourth", "Fifth"}
    assert found_text == expected_text

    # Test translation of nested content
    for part in parts:
        if part["type"] == "translatable":
            part["content"] = f"Trans_{part['content'].strip()}"

    reassembled = html_splitter.reassemble_content(parts, html)
    for text in [
        "Trans_First",
        "Trans_Second",
        "Trans_Third",
        "Trans_Fourth",
        "Trans_Fifth",
    ]:
        assert text in reassembled


def test_mixed_nested_tags(html_splitter):
    """Test nested content with mix of translatable and untranslatable tags."""
    html = """
    <div>Start text
        <pre>
            def code():
                pass
        </pre>
        Middle text
        <code>more_code()</code>
        <p>End text with <em>emphasis</em> and <script>alert('test');</script></p>
    </div>
    """
    parts = html_splitter.split_content(html)

    translatable = [p for p in parts if p["type"] == "translatable"]
    untranslatable = [p for p in parts if p["type"] == "untranslatable"]

    # Check translatable content
    found_text = {p["content"].strip() for p in translatable if p["content"].strip()}
    assert "Start text" in found_text
    assert "Middle text" in found_text
    assert "End text with" in found_text
    assert "emphasis" in found_text

    # Check untranslatable content
    found_untranslatable = {p["context"]["tag"] for p in untranslatable}
    assert "pre" in found_untranslatable
    assert "code" in found_untranslatable
    assert "script" in found_untranslatable


def test_nested_with_entities(html_splitter):
    """Test nested content with HTML entities and special characters."""
    html = """
    <div>Text with &amp; symbol
        <p>Paragraph with &lt;tags&gt; 
            <span>More &quot;quoted&quot; text
                <em>Special chars: &copy; &reg; &trade;</em>
            </span>
        </p>
    </div>
    """
    parts = html_splitter.split_content(html)
    translatable = [p for p in parts if p["type"] == "translatable"]

    # Check entity preservation in translatable content
    found_text = {p["content"].strip() for p in translatable if p["content"].strip()}
    assert "Text with & symbol" in found_text
    assert "Paragraph with <tags>" in found_text
    assert 'More "quoted" text' in found_text
    assert any("Special chars:" in text for text in found_text)

    # Test translation with entities
    for part in parts:
        if part["type"] == "translatable":
            # Keep the entities in the translation
            text = part["content"].strip()
            if "Special chars:" in text:
                part["content"] = "Trans_Special: &copy; &reg; &trade;"
            else:
                part["content"] = f"Trans_{text}"

    reassembled = html_splitter.reassemble_content(parts, html)
    soup = BeautifulSoup(reassembled, "html.parser")

    # Check text content after parsing
    text = soup.get_text()
    assert "Trans_Text with & symbol" in text
    assert "Trans_Paragraph with <tags>" in text
    assert 'Trans_More "quoted" text' in text
    assert "Trans_Special: " in text
    assert "Trans_Text with &" in text
    assert "Trans_Paragraph with <tags>" in text
    assert 'Trans_More "quoted" text' in text

    # Check HTML structure is preserved
    assert "<div>" in reassembled
    assert "<p>" in reassembled
    assert "<span>" in reassembled
    assert "<em>" in reassembled


def test_nested_table_content(html_splitter):
    """Test nested content within table structures."""
    html = """
    <table>
        <thead>
            <tr>
                <th>Header 1</th>
                <th>Header <em>2</em></th>
            </tr>
        </thead>
        <tbody>
            <tr>
                <td>Cell <strong>1</strong></td>
                <td>Cell <code>var</code> 2</td>
            </tr>
            <tr>
                <td><p>Nested <em>paragraph</em></p></td>
                <td>Final <span>cell</span></td>
            </tr>
        </tbody>
    </table>
    """
    parts = html_splitter.split_content(html)
    translatable = [p for p in parts if p["type"] == "translatable"]
    untranslatable = [p for p in parts if p["type"] == "untranslatable"]

    # Check table content
    found_text = {p["content"].strip() for p in translatable if p["content"].strip()}
    assert "Header 1" in found_text
    assert "Header" in found_text
    assert "2" in found_text
    assert "Cell" in found_text
    assert "1" in found_text
    assert "Cell" in found_text
    assert "2" in found_text
    assert "Nested" in found_text
    assert "paragraph" in found_text
    assert "Final" in found_text
    assert "cell" in found_text

    # Check code preservation
    assert any("var" in p["content"] for p in untranslatable)


def test_nested_list_content(html_splitter):
    """Test nested content within list structures."""
    html = """
    <ul>
        <li>First level 1
            <ul>
                <li>Second <em>level</em> 1</li>
                <li>Second level <code>2</code></li>
            </ul>
        </li>
        <li>First level 2
            <ol>
                <li>Numbered <strong>item</strong> 1</li>
                <li>Numbered item <pre>2</pre></li>
            </ol>
        </li>
    </ul>
    """
    parts = html_splitter.split_content(html)
    translatable = [p for p in parts if p["type"] == "translatable"]
    untranslatable = [p for p in parts if p["type"] == "untranslatable"]

    # Check list content
    found_text = {p["content"].strip() for p in translatable if p["content"].strip()}
    assert "First level 1" in found_text
    assert "Second" in found_text
    assert "level" in found_text
    assert "1" in found_text
    assert "Second level" in found_text
    assert "First level 2" in found_text
    assert "Numbered" in found_text
    assert "item" in found_text
    assert "Numbered item" in found_text

    # Check untranslatable content
    found_untranslatable = {p["context"]["tag"] for p in untranslatable}
    assert "code" in found_untranslatable
    assert "pre" in found_untranslatable


def test_nested_with_attributes(html_splitter):
    """Test nested content with various HTML attributes."""
    html = """
    <div class="container" id="main">
        <p data-test="value" class="text">First
            <span class="nested" data-custom="test">Second
                <em style="color: red;" class="emphasis">Third
                    <strong id="bold" class="weight">Fourth</strong>
                </em>
            </span>
        </p>
    </div>
    """
    parts = html_splitter.split_content(html)

    # Test translation
    for part in parts:
        if part["type"] == "translatable":
            part["content"] = f"Trans_{part['content'].strip()}"

    reassembled = html_splitter.reassemble_content(parts, html)
    soup = BeautifulSoup(reassembled, "html.parser")

    # Check attribute preservation
    div = soup.find("div")
    assert div["class"] == ["container"]
    assert div["id"] == "main"

    p = soup.find("p")
    assert p["data-test"] == "value"
    assert p["class"] == ["text"]

    span = soup.find("span")
    assert span["class"] == ["nested"]
    assert span["data-custom"] == "test"

    em = soup.find("em")
    assert em["style"] == "color: red;"
    assert em["class"] == ["emphasis"]

    strong = soup.find("strong")
    assert strong["id"] == "bold"
    assert strong["class"] == ["weight"]

    # Check translations
    for text in ["Trans_First", "Trans_Second", "Trans_Third", "Trans_Fourth"]:
        assert text in reassembled
