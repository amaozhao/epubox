from __future__ import annotations

import codecs
import hashlib

import pytest
from pydantic import ValidationError

from engine.epub.fill import fill_resource
from engine.epub.ranges import RangeError
from engine.item.atoms import _LEGACY_EXTRACTOR_VERSION, EXTRACTOR_VERSION, extract_resource
from engine.item.inline import parse_projection, validate_projection
from engine.item.views import validate_source_views
from engine.schemas.bridge import AtomicDocument, ByteSpan
from engine.schemas.contracts import canonical_json_bytes, parse_contract

XHTML = "http://www.w3.org/1999/xhtml"
DC = "http://purl.org/dc/elements/1.1/"
OPF = "http://www.idpf.org/2007/opf"
NCX = "http://www.daisy.org/z3986/2005/ncx/"


def source(body: str, head: str = "") -> bytes:
    return f'<html xmlns="{XHTML}"><head>{head}</head><body>{body}</body></html>'.encode()


def texts(result) -> list[str]:
    return [
        "".join(
            event.value if event.kind == "text" else item.registry[event.value[1:]].source_text
            for event in parse_projection(item.source_projection)
            if event.kind == "text"
            or event.value.startswith("=x")
            and item.registry[event.value[1:]].boundary_type == "whitespace"
        )
        for item in result.items
    ]


def test_resource_stem_head_title_is_preserved_locally_without_a_model_item() -> None:
    raw = source("<p>Translate this body.</p>", "<title>chapter</title>")
    result = extract_resource(raw, "OPS/chapter.xhtml", "source")
    version_three = extract_resource(raw, "OPS/chapter.xhtml", "source", extractor_version="epubox-atomic-3")

    assert all(item.kind != "head_title" for item in result.items)
    assert all(item.kind != "head_title" for item in version_three.items)
    body = next(item for item in result.items if item.kind == "p")
    rendered = fill_resource(raw, result, {body.item_id: body.source_projection.replace("Translate", "翻译")})
    assert b"<title>chapter</title>" in rendered
    assert "翻译" in rendered.decode()


def test_prose_head_title_remains_translatable_and_v2_replay_is_unchanged() -> None:
    raw = source("<p>Body.</p>", "<title>Chapter One</title>")
    current = extract_resource(raw, "OPS/chapter.xhtml", "source")
    legacy = extract_resource(
        source("<p>Body.</p>", "<title>chapter</title>"),
        "OPS/chapter.xhtml",
        "source",
        extractor_version=_LEGACY_EXTRACTOR_VERSION,
    )

    assert any(item.kind == "head_title" for item in current.items)
    assert any(item.kind == "head_title" for item in legacy.items)


def test_three_nested_containers_keep_paragraphs_whole_and_inline_ranges_inside_their_owner() -> None:
    raw = source(
        '<div><div><div><p id="one">First <em>complete</em> <i>range</i> and <a href="#one">link</a>.</p>'
        "<p>Second paragraph.</p><p>Third paragraph.</p></div></div></div>"
    )
    result = extract_resource(raw, "OPS/chapter.xhtml", "book-source")
    assert [item.atomic_tag for item in result.items] == ["p", "p", "p"]
    assert texts(result) == ["First complete range and link.", "Second paragraph.", "Third paragraph."]
    assert all("member_node_keys" not in item.region for item in result.items)
    assert result.document.extractor_version == EXTRACTOR_VERSION
    for item in result.items:
        assert raw[item.source_span.byte_start : item.source_span.byte_end].startswith(b"<p")
        assert raw[item.source_span.byte_start : item.source_span.byte_end].endswith(b"</p>")
        validate_projection(item)
    validate_source_views(result.document)


@pytest.mark.parametrize("version", [f"epubox-atomic-{number}" for number in range(1, 7)])
@pytest.mark.parametrize("separator", [" ", "&#9;", "&#10;", "&#160;"])
def test_protected_text_gaps_round_trip_with_translation(version: str, separator: str) -> None:
    raw = source(f"<p>Reviewers (中田{separator}秀基) (Japanese). Use <code>fn main() {{}}</code> safely.</p>")
    result = extract_resource(raw, "OPS/chapter.xhtml", "source", extractor_version=version)
    validate_source_views(result.document)
    assert fill_resource(raw, result, {item.item_id: item.source_projection for item in result.items}) == raw
    targets = {
        item.item_id: item.source_projection.replace("Reviewers", "审阅者").replace("Use", "使用")
        for item in result.items
    }
    rendered = fill_resource(raw, result, targets)
    assert f"中田{separator}秀基".encode() in rendered
    assert b"<code>fn main() {}</code>" in rendered
    assert "审阅者".encode() in rendered
    assert all("fn main" not in view.text for view in result.document.source_views.values())


def test_entire_tables_and_nested_lists_have_one_outer_owner() -> None:
    raw = source(
        '<table><caption>Results</caption><tr><th scope="col">Name</th><th>Count</th></tr>'
        '<tr><td rowspan="2"><p>First entry</p></td><td colspan="2">Ten</td></tr>'
        "<tr><td><ul><li>Inside table</li></ul></td></tr></table>"
        "<ul><li>First <p>Full paragraph</p><ol><li>Nested <em>emphasis</em></li></ol></li>"
        "<li>Second</li></ul>"
    )
    result = extract_resource(raw, "OPS/chapter.xhtml", "book-source")
    assert [item.atomic_tag for item in result.items] == ["table", "ul"]
    assert not any(item.kind in {"table_cell", "list_item", "paragraph_group"} for item in result.items)
    assert texts(result) == ["ResultsNameCountFirst entryTenInside table", "First Full paragraphNested emphasisSecond"]
    table_views = [result.document.source_views[view].text for view in result.items[0].source_view_ids]
    assert table_views == ["Results", "Name", "Count", "First entry", "Ten", "Inside table"]
    assert raw[result.items[0].source_span.byte_start : result.items[0].source_span.byte_end].endswith(b"</table>")
    assert raw[result.items[1].source_span.byte_start : result.items[1].source_span.byte_end].endswith(b"</ul>")
    validate_source_views(result.document)


def test_epub_navigation_extracts_anchor_labels_without_owning_list_markup() -> None:
    raw = source(
        '<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc" id="toc" class="nav">'
        '<h2>Contents</h2><ol class="outer"><li><a href="one.xhtml#top" class="entry">'
        '<img src="icon.png" class="icon"/>Chapter one</a>'
        '<ol><li><a href="two.xhtml"><span>Chapter two</span></a></li></ol></li></ol></nav>'
        "<ol><li>Ordinary body list</li></ol>",
        "<style>.entry { color: red; }</style>",
    )

    result = extract_resource(raw, "OPS/nav.xhtml", "book-source")

    navigation = [item for item in result.items if item.channel == "navigation"]
    assert texts(result) == ["Chapter one", "Chapter two", "Ordinary body list"]
    assert len(navigation) == 2
    assert all(item.kind == "navigation" and item.atomic_tag is None for item in navigation)
    assert [raw[item.source_span.byte_start : item.source_span.byte_end] for item in navigation] == [
        b'<img src="icon.png" class="icon"/>Chapter one',
        b"<span>Chapter two</span>",
    ]
    assert result.items[-1].atomic_tag == "ol"
    assert b'<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc" id="toc" class="nav">' in raw
    validate_source_views(result.document)

    targets = {item.item_id: item.source_projection for item in result.items}
    targets[navigation[0].item_id] = navigation[0].source_projection.replace("Chapter one", "第一章")
    targets[navigation[1].item_id] = navigation[1].source_projection.replace("Chapter two", "第二章")
    filled = fill_resource(raw, result, targets)
    assert b'<a href="one.xhtml#top" class="entry"><img src="icon.png" class="icon"/>' in filled
    assert b'<a href="two.xhtml"><span>' in filled
    assert b"<style>.entry { color: red; }</style>" in filled
    assert "第一章" in filled.decode() and "第二章" in filled.decode()


def test_atomic_one_replay_keeps_historical_navigation_inventory() -> None:
    raw = source(
        '<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc">'
        '<ol><li><a href="one.xhtml">Chapter one</a></li></ol></nav>'
    )

    current = extract_resource(raw, "OPS/nav.xhtml", "book-source")
    version_three = extract_resource(
        raw,
        "OPS/nav.xhtml",
        "book-source",
        extractor_version="epubox-atomic-3",
    )
    legacy = extract_resource(
        raw,
        "OPS/nav.xhtml",
        "book-source",
        extractor_version="epubox-atomic-1",
    )

    assert [item.kind for item in current.items] == ["navigation"]
    assert [item.kind for item in version_three.items] == ["navigation"]
    assert [item.atomic_tag for item in legacy.items] == ["ol"]
    assert legacy.document.extractor_version == "epubox-atomic-1"


@pytest.mark.parametrize("navigation_type", ["page-list", "landmarks"])
def test_other_epub_navigation_types_extract_anchor_labels(navigation_type: str) -> None:
    raw = source(
        f'<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="{navigation_type}">'
        '<ol><li><a href="one.xhtml">Label</a></li></ol></nav>'
        "<nav><ol><li>Ordinary navigation content</li></ol></nav>"
    )

    result = extract_resource(raw, "OPS/nav.xhtml", "book-source")

    assert [item.kind for item in result.items] == ["navigation", "ol"]
    assert texts(result) == ["Label", "Ordinary navigation content"]


def test_standalone_emphasis_and_parent_text_and_tails_follow_byte_reading_order() -> None:
    raw = source(
        "Before<div>Lead<span>Before emphasis<em>Standalone emphasis</em>after emphasis</span>"
        "<p>Middle</p>After paragraph<i>Standalone italics</i>End</div>Outside"
    )
    result = extract_resource(raw, "OPS/chapter.xhtml", "book-source")
    assert texts(result) == [
        "Before",
        "Lead",
        "Before emphasis",
        "Standalone emphasis",
        "after emphasis",
        "Middle",
        "After paragraph",
        "Standalone italics",
        "End",
        "Outside",
    ]
    assert [item.atomic_tag for item in result.items if item.atomic_tag] == ["em", "p", "i"]
    starts = [item.source_span.byte_start for item in result.items]
    assert starts == sorted(starts)
    validate_source_views(result.document)


def test_translation_islands_stay_inside_the_outer_atomic_element() -> None:
    raw = source(
        '<p translate="no" title="Keep tooltip">Keep source <span translate="yes">Translate this '
        '<em>whole emphasis</em><img src="image.png" alt="Translated image"/></span>Keep tail</p>'
    )
    result = extract_resource(raw, "OPS/chapter.xhtml", "book-source")
    assert [item.atomic_tag for item in result.items] == ["p", None]
    assert "Keep source" not in texts(result)[0] and "Keep tail" not in texts(result)[0]
    assert texts(result) == ["Translate this whole emphasis", "Translated image"]
    assert result.items[1].region["patch_owner_id"] == result.items[0].unit_id
    assert "Keep tooltip" not in texts(result)
    validate_source_views(result.document)


def test_head_whitelist_and_body_attributes_leave_scripts_styles_and_paths_protected() -> None:
    head = (
        '<title>Book title</title><meta name="description" content="Book summary &amp; details"/>'
        '<meta name="keywords" content="Keep keywords"/><style>.a { content: "&lt;head>"; }</style>'
        '<script><![CDATA[const fake = "</head><p>ignore</p>";]]></script>'
    )
    raw = source('<p id="fixed">Text<img src="fixed.png" alt="Image label" title="Image tooltip"/></p>', head)
    result = extract_resource(raw, "OPS/chapter.xhtml", "book-source")
    assert texts(result) == ["Book title", "Book summary & details", "Text", "Image label", "Image tooltip"]
    assert [item.channel for item in result.items] == ["metadata", "metadata", "body", "attribute", "attribute"]
    assert {location.attribute_name for location in result.source_map.locations if location.field == "attribute"} == {
        "content",
        "alt",
        "title",
    }
    for marker in (b"fixed.png", b'id="fixed"', b"Keep keywords", b"const fake", b".a {"):
        start = raw.index(marker)
        assert any(
            span.byte_start <= start and start + len(marker) <= span.byte_end
            for span in result.source_map.protected_spans
        )
    assert all(item.region["patch_owner_id"] == result.items[2].unit_id for item in result.items[3:])


def test_opf_and_ncx_use_exact_namespaces_and_preserve_machine_fields_and_author_names() -> None:
    opf = (
        f'<o:package xmlns:o="{OPF}" xmlns:d="{DC}" xmlns:f="urn:foreign"><o:metadata>'
        '<d:title id="title">The book</d:title><d:description>Readable summary</d:description>'
        "<d:creator>Author Name</d:creator><d:identifier>fixed-id</d:identifier>"
        "<f:title>Foreign machine title</f:title></o:metadata>"
        '<o:manifest><o:item id="ch" href="chapter.xhtml" media-type="application/xhtml+xml"/></o:manifest>'
        '<o:spine><o:itemref idref="ch"/></o:spine></o:package>'
    ).encode()
    result = extract_resource(opf, "OPS/package.opf", "book-source", "application/oebps-package+xml")
    assert texts(result) == ["The book", "Readable summary"]
    assert all(item.channel == "metadata" for item in result.items)
    ncx = (
        f'<n:ncx xmlns:n="{NCX}" xmlns:f="urn:foreign"><n:docTitle><n:text>The book</n:text></n:docTitle>'
        '<n:navMap><n:navPoint id="fixed"><n:navLabel><n:text>Chapter one</n:text></n:navLabel>'
        '<n:content src="chapter.xhtml#fixed"/></n:navPoint></n:navMap>'
        "<f:navLabel><f:text>Keep foreign text</f:text></f:navLabel></n:ncx>"
    ).encode()
    navigation = extract_resource(ncx, "OPS/toc.ncx", "book-source", "application/x-dtbncx+xml")
    assert texts(navigation) == ["The book", "Chapter one"]
    assert all(item.channel == "navigation" for item in navigation.items)
    machine = extract_resource(b"<root><p>Machine text</p></root>", "OPS/data.xml", "book-source", "application/xml")
    assert not machine.items and not machine.source_map.locations


@pytest.mark.parametrize(
    ("encoding", "bom"),
    [("utf-8", codecs.BOM_UTF8), ("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE)],
)
def test_raw_encodings_original_hash_and_json_round_trip(encoding: str, bom: bytes) -> None:
    declared = "UTF-8" if encoding == "utf-8" else "UTF-16"
    markup = f'<?xml version="1.0" encoding="{declared}"?><html xmlns="{XHTML}"><head/><body><p>Text 中文.</p></body></html>'
    raw = bom + markup.encode(encoding)
    first = extract_resource(raw, "OPS/chapter.xhtml", "book-source")
    second = extract_resource(raw, "OPS/chapter.xhtml", "book-source")
    assert first == second
    assert first.document.resource.source_sha256 == hashlib.sha256(raw).hexdigest()
    assert first.source_map.encoding == encoding
    assert parse_contract(canonical_json_bytes(first), AtomicDocument, "epubox-atoms-1") == first


def test_comment_pi_tails_and_protected_only_content_have_complete_source_ownership() -> None:
    result = extract_resource(
        source("Lead<!--keep-->tail<?keep value?>after<p>Paragraph</p>End"), "OPS/chapter.xhtml", "source"
    )
    assert texts(result) == ["Leadtailafter", "Paragraph", "End"]
    assert len([boundary for boundary in result.document.boundaries if boundary["kind"] == "non_element_tail"]) == 2
    validate_source_views(result.document)
    protected = extract_resource(source("<p><code>" + "a" * 20_000 + "</code></p>"), "OPS/chapter.xhtml", "source")
    assert not protected.items and not protected.source_map.locations
    punctuated = extract_resource(
        source('<p>...<span translate="no">Keep</span>...</p>'), "OPS/chapter.xhtml", "source"
    )
    assert not punctuated.items


def test_program_code_div_is_protected_only_by_the_current_extractor() -> None:
    raw = source(
        '<div class="ProgramCode"><div class="LineGroup"><div class="FixedLine">'
        'const index = pc.index("document-search");'
        '</div></div><div class="Caption"><p>Listing caption.</p></div></div>'
        "<p>Translate prose.</p>"
    )

    current = extract_resource(raw, "OPS/chapter.xhtml", "source")
    version_three = extract_resource(
        raw,
        "OPS/chapter.xhtml",
        "source",
        extractor_version="epubox-atomic-3",
    )
    version_four = extract_resource(
        raw,
        "OPS/chapter.xhtml",
        "source",
        extractor_version="epubox-atomic-4",
    )
    version_five = extract_resource(
        raw,
        "OPS/chapter.xhtml",
        "source",
        extractor_version="epubox-atomic-5",
    )

    assert texts(current) == ["Listing caption.", "Translate prose."]
    assert texts(version_four) == texts(current)
    assert texts(version_five) == texts(current)
    assert texts(version_three) == [
        'const index = pc.index("document-search");',
        "Listing caption.",
        "Translate prose.",
    ]
    code_start = raw.index(b"const index")
    code_end = code_start + len(b'const index = pc.index("document-search");')
    assert any(
        span.byte_start <= code_start and code_end <= span.byte_end for span in current.source_map.protected_spans
    )


def test_styled_preformatted_monospace_paragraph_is_protected_only_by_atomic_five() -> None:
    style = (
        "margin:0;white-space:pre-wrap;text-align:left;font-family:Menlo,Consolas,monospace;"
        "font-size:0.75em;line-height:1.4;"
    )
    code = (
        f'<p style="{style}" title="Rust listing"><span class="pub">pub</span> '
        '<span class="fn">fn</span> main() {\n    <span class="if">if</span> count '
        '<span class="op">/=</span> <span class="number">2</span>;\n}</p>'
    )
    raw = source(code + "<p>Translate nearby prose.</p>")

    current = extract_resource(raw, "OPS/rust.xhtml", "source")
    version_four = extract_resource(
        raw,
        "OPS/rust.xhtml",
        "source",
        extractor_version="epubox-atomic-4",
    )
    version_five = extract_resource(
        raw,
        "OPS/rust.xhtml",
        "source",
        extractor_version="epubox-atomic-5",
    )

    assert texts(current) == ["Translate nearby prose."]
    assert current.document.extractor_version == EXTRACTOR_VERSION
    assert texts(version_five) == texts(current)
    assert version_five.document.extractor_version == "epubox-atomic-5"
    assert texts(version_four) == [
        "pub fn main() {\n    if count /= 2;\n}",
        "Rust listing",
        "Translate nearby prose.",
    ]
    assert version_four.document.extractor_version == "epubox-atomic-4"
    code_bytes = code.encode()
    code_start = raw.index(code_bytes)
    code_end = code_start + len(code_bytes)
    assert any(
        span.byte_start <= code_start and code_end <= span.byte_end for span in current.source_map.protected_spans
    )
    prose = current.items[0]
    filled = fill_resource(
        raw,
        current,
        {prose.item_id: prose.source_projection.replace("Translate nearby prose.", "翻译相邻正文。")},
    )
    assert code_bytes in filled
    assert "翻译相邻正文。" in filled.decode()


def test_partial_code_styling_does_not_hide_natural_language_paragraphs() -> None:
    raw = source(
        '<p style="font-family:Menlo,Consolas,monospace">Natural monospace paragraph.</p>'
        '<p style="white-space:pre-wrap">Natural pre-wrapped paragraph.</p>'
        '<p>Natural sentence with <span style="font-family:monospace">inline code font words</span> inside.</p>'
    )

    result = extract_resource(raw, "OPS/prose.xhtml", "source", extractor_version="epubox-atomic-5")

    assert texts(result) == [
        "Natural monospace paragraph.",
        "Natural pre-wrapped paragraph.",
        "Natural sentence with inline code font words inside.",
    ]
    assert [item.atomic_tag for item in result.items] == ["p", "p", "p"]


def test_atomic_six_protects_plain_preformatted_code_and_preserves_raw_bytes() -> None:
    code = (
        '<p id="logs" title="Runtime output" '
        'style="margin:0;white-space:pre-wrap;font-family:Menlo,Consolas,monospace">'
        "INFO ready\r\n\r\nWARN retry<!-- keep log comment -->\r\n</p>"
    )
    raw = source(code + "<p>Translate nearby prose.</p>")

    current = extract_resource(raw, "OPS/logs.xhtml", "source")
    version_five = extract_resource(
        raw,
        "OPS/logs.xhtml",
        "source",
        extractor_version="epubox-atomic-5",
    )

    assert EXTRACTOR_VERSION == "epubox-atomic-6"
    assert texts(current) == ["Translate nearby prose."]
    assert texts(version_five) == ["INFO ready\n\nWARN retry\n", "Runtime output", "Translate nearby prose."]
    assert not any(item.kind == "attribute" for item in current.items)
    code_bytes = code.encode()
    code_start = raw.index(code_bytes)
    code_end = code_start + len(code_bytes)
    assert any(
        span.byte_start <= code_start and code_end <= span.byte_end for span in current.source_map.protected_spans
    )
    prose = current.items[0]
    filled = fill_resource(
        raw,
        current,
        {prose.item_id: prose.source_projection.replace("Translate nearby prose.", "翻译相邻正文。")},
    )
    assert code_bytes in filled and "翻译相邻正文。" in filled.decode()


def test_atomic_six_uses_an_opaque_atom_for_complex_inline_code() -> None:
    code = (
        '<span id="identifier" title="Code tooltip" style="font-family:monospace">'
        'OutPoint::<span style="color:#795da3">new</span>("quoted\tvalue")'
        "\r\n<!-- exact inline comment --></span>"
    )
    raw = source(f"<p>Explain {code} clearly.</p>")

    current = extract_resource(raw, "OPS/inline.xhtml", "source")
    version_five = extract_resource(
        raw,
        "OPS/inline.xhtml",
        "source",
        extractor_version="epubox-atomic-5",
    )

    assert len(current.items) == 1 and "⟦=x" in current.items[0].source_projection
    assert "OutPoint" not in current.items[0].source_projection
    assert texts(current) == ["Explain  clearly."]
    assert texts(version_five) == ['Explain OutPoint::new("quoted\tvalue")\n clearly.', "Code tooltip"]
    assert not any(item.kind == "attribute" for item in current.items)
    item = current.items[0]
    target = item.source_projection.replace("Explain ", "说明").replace(" clearly.", "。")
    filled = fill_resource(raw, current, {item.item_id: target})
    assert code.encode() in filled and "说明".encode() in filled


def test_atomic_six_protects_semantic_code_program_code_and_code_classes() -> None:
    raw = source(
        '<pre id="semantic">fn semantic() {}</pre>'
        '<div class="ProgramCode"><div class="FixedLine">fn program() {}</div></div>'
        '<p class="language-rust">fn classified() {}</p>'
        '<link rel="stylesheet" href="code.css"/>'
        '<p class="external-code">fn external() {}</p>'
        "<style>.embedded-code { white-space: pre-wrap; font-family: monospace; }</style>"
        '<p class="embedded-code">fn embedded() {}</p>'
        "<p>Translate prose.</p>"
    )
    styles = {"OPS/code.css": ".external-code { white-space: pre-wrap; font-family: monospace; }"}

    current = extract_resource(raw, "OPS/code.xhtml", "source", styles=styles)
    version_five = extract_resource(
        raw,
        "OPS/code.xhtml",
        "source",
        extractor_version="epubox-atomic-5",
    )

    assert texts(current) == ["Translate prose."]
    assert texts(version_five) == [
        "fn classified() {}",
        "fn external() {}",
        "fn embedded() {}",
        "Translate prose.",
    ]


def test_atomic_six_resolves_namespaced_external_css_code_blocks_without_exposing_source() -> None:
    code = '<p class="listing" title="Rust listing">pub fn external() {\r\n    ready();\r\n}</p>'
    raw = source('<link rel="stylesheet" href="code.css"/>' + code + "<p>Translate prose.</p>")
    styles = {
        "OPS/code.css": (
            '@namespace h "http://www.w3.org/1999/xhtml"; '
            "h|p.listing { white-space: pre-wrap; font-family: monospace; }"
        )
    }

    result = extract_resource(raw, "OPS/chapter.xhtml", "source", styles=styles)

    assert texts(result) == ["Translate prose."]
    assert not any(item.kind == "attribute" for item in result.items)
    code_bytes = code.encode()
    start = raw.index(code_bytes)
    end = start + len(code_bytes)
    assert any(span.byte_start <= start and end <= span.byte_end for span in result.source_map.protected_spans)
    prose = result.items[0]
    filled = fill_resource(
        raw,
        result,
        {prose.item_id: prose.source_projection.replace("Translate prose.", "翻译正文。")},
    )
    assert code_bytes in filled and "翻译正文。".encode() in filled


def test_atomic_six_protects_line_numbered_code_lists_and_tables_as_complete_containers() -> None:
    listing = (
        '<ol class="code" id="listing"><li><span class="line-number">1</span>'
        '<span>pub fn list() {}</span></li><li><span class="line-number">2</span>'
        '<span>println!("ready");</span></li></ol>'
    )
    table = (
        '<table class="code" id="table-code"><tr><td>1</td><td>pub fn table() {}</td></tr>'
        "<tr><td>2</td><td>return;</td></tr></table>"
    )
    raw = source(listing + table + "<p>Translate prose.</p>")

    result = extract_resource(raw, "OPS/numbered.xhtml", "source")

    assert texts(result) == ["Translate prose."]
    for code in (listing, table):
        data = code.encode()
        start = raw.index(data)
        end = start + len(data)
        assert any(span.byte_start <= start and end <= span.byte_end for span in result.source_map.protected_spans)


def test_atomic_six_keeps_figure_caption_editable_while_protecting_its_code() -> None:
    code = '<pre id="figure-code"><code>pub fn figure() {\r\n    return;\r\n}</code></pre>'
    raw = source(
        '<figure class="code" id="example">'
        + code
        + "<figcaption>Translate this listing caption.</figcaption></figure>"
        + "<p>Translate prose.</p>"
    )

    result = extract_resource(raw, "OPS/figure.xhtml", "source")

    assert texts(result) == ["Translate this listing caption.", "Translate prose."]
    data = code.encode()
    start = raw.index(data)
    end = start + len(data)
    assert any(span.byte_start <= start and end <= span.byte_end for span in result.source_map.protected_spans)
    targets = {item.item_id: item.source_projection.replace("Translate", "翻译") for item in result.items}
    filled = fill_resource(raw, result, targets)
    assert data in filled
    assert "翻译 this listing caption.".encode() in filled and "翻译 prose.".encode() in filled


@pytest.mark.parametrize(
    ("style", "body", "protected"),
    [
        (
            "white-space:pre-wrap;white-space:normal;font-family:monospace",
            "Natural sentence after the final whitespace declaration.",
            False,
        ),
        (
            "white-space:pre-wrap!important;white-space:normal;font-family:monospace",
            '<span style="color:#a71d5d">let</span> value = 1;',
            True,
        ),
        (
            "white-space:pre-wrap;font:12px monospace;font-family:monospace",
            "Natural sentence with a font shorthand.",
            False,
        ),
        (
            "all:initial;white-space:pre-wrap;font-family:monospace",
            "Natural sentence with an all declaration.",
            False,
        ),
        (
            "white-space:pre-wrap;font-family:'monospace'",
            "Natural sentence using a font whose quoted name is monospace.",
            False,
        ),
        (
            "white-space:pre-wrap;font-family:monospace",
            "Natural sentence with <em>non-span markup</em> inside.",
            False,
        ),
        (
            "white-space:pre-wrap;font-family:monospace",
            "This is an ordinary English paragraph without any syntax highlighting spans.",
            False,
        ),
        (
            "white-space:pre-wrap;font-family:var(--body-font),monospace",
            '<span style="color:#a71d5d">Dynamic font fallback is not proven code.</span>',
            False,
        ),
        (
            "white-space:pre-wrap;font-family:monospace serif",
            '<span style="color:#a71d5d">Invalid font family syntax is not proven code.</span>',
            False,
        ),
    ],
    ids=(
        "last-declaration-wins",
        "important-declaration-wins",
        "font-shorthand-is-ambiguous",
        "all-declaration-is-ambiguous",
        "quoted-monospace-is-a-family-name",
        "non-span-descendant",
        "ordinary-prose-without-syntax-spans",
        "dynamic-font-family",
        "invalid-generic-family-sequence",
    ),
)
def test_styled_code_detection_requires_unambiguous_css_and_span_structure(
    style: str, body: str, protected: bool
) -> None:
    raw = source(f'<p style="{style}">{body}</p>')

    result = extract_resource(
        raw,
        "OPS/styled.xhtml",
        "source",
        extractor_version="epubox-atomic-5",
    )
    assert result.document.extractor_version == "epubox-atomic-5"

    if protected:
        assert not result.items
        block_start = raw.index(b"<p ")
        block_end = raw.index(b"</p>") + len(b"</p>")
        assert any(
            span.byte_start <= block_start and block_end <= span.byte_end for span in result.source_map.protected_spans
        )
    else:
        assert len(result.items) == 1
        assert result.items[0].atomic_tag == "p"


def test_atomic_inventory_rejects_partial_and_changed_source_items_and_refuses_unproven_html() -> None:
    result = extract_resource(source("<p>Whole</p>"), "OPS/chapter.xhtml", "source")
    with pytest.raises(ValidationError):
        AtomicDocument.model_validate(result.model_dump() | {"items": ()})
    item = result.items[0].model_copy(update={"source_projection": "Changed"})
    with pytest.raises(ValidationError, match="authoritative source"):
        AtomicDocument.model_validate(result.model_dump() | {"items": (item,)})
    item = result.items[0].model_copy(update={"source_span": ByteSpan(byte_start=1, byte_end=2)})
    with pytest.raises(ValidationError):
        AtomicDocument.model_validate(result.model_dump() | {"items": (item.model_dump(),)})
    with pytest.raises(RangeError, match="genuine HTML"):
        extract_resource(b"<html><body><p>one<p>two</body></html>", "OPS/chapter.html", "source", "text/html")


def test_large_paragraphs_are_never_cut_by_the_historical_1200_cap() -> None:
    text = "word " * 2600
    result = extract_resource(source(f"<p>{text}</p>"), "OPS/chapter.xhtml", "source")
    assert len(result.items) == 1
    assert result.items[0].atomic_tag == "p"
    assert texts(result) == [text]


def test_opaque_media_and_foreign_xml_have_no_descendant_tasks() -> None:
    raw = source(
        '<video title="Keep media"><div title="Keep fallback"><p>Keep fallback</p></div></video>'
        "<p>Body <svg><text>Keep vector</text></svg> tail</p>"
        '<f:p xmlns:f="urn:foreign">Keep foreign XML</f:p><P>Keep XML case</P>'
    )
    result = extract_resource(raw, "OPS/chapter.xhtml", "source")
    assert [item.atomic_tag for item in result.items] == ["p"]
    assert texts(result) == ["Body  tail"]
    assert not any(item.channel == "attribute" for item in result.items)


def test_virtual_safe_boundaries_are_source_coordinates_and_never_cut_atomic_or_inline_ranges() -> None:
    raw = source("First sentence. Second sentence.\n\nThird sentence.<p>Atomic sentence. Never cut.</p>")
    result = extract_resource(raw, "OPS/chapter.xhtml", "source")
    virtual, paragraph = result.items
    assert virtual.atomic_tag is None
    boundaries = virtual.region["safe_boundaries"]
    assert isinstance(boundaries, list) and len(boundaries) == 2
    assert "safe_boundaries" not in paragraph.region
    for boundary in boundaries:
        assert isinstance(boundary, dict)
        assert isinstance(boundary["byte_offset"], int)
        assert raw[: boundary["byte_offset"]].endswith((b". ", b"\n\n"))
        assert boundary["slot_id"] in virtual.slot_ids
    inline = extract_resource(source("<span>Inline sentence. Never cut.</span>"), "OPS/chapter.xhtml", "source")
    assert not inline.items[0].region.get("safe_boundaries")
    abbreviation = extract_resource(source("Mr. Jones lives here. Next sentence."), "OPS/chapter.xhtml", "source")
    cuts = abbreviation.items[0].region["safe_boundaries"]
    assert isinstance(cuts, list) and len(cuts) == 1


@pytest.mark.parametrize(
    ("data", "media"),
    [
        (b"<html><body><p>Missing namespace</p></body></html>", "application/xhtml+xml"),
        (b"<ncx><docTitle><text>Missing namespace</text></docTitle></ncx>", "application/x-dtbncx+xml"),
        (b"<package><metadata><title>Missing namespace</title></metadata></package>", "application/oebps-package+xml"),
        (b"<div><p>Genuine HTML fragment</p></div>", "text/html"),
    ],
)
def test_required_resource_diagnostics_cannot_become_empty_success(data: bytes, media: str) -> None:
    with pytest.raises(RangeError, match="namespace|genuine HTML"):
        extract_resource(data, "OPS/resource.xml", "source", media)


def test_saved_map_location_fields_and_attribute_patch_owners_are_authoritative() -> None:
    result = extract_resource(source('<p>Body<img alt="Image" src="image.png"/></p>'), "OPS/chapter.xhtml", "source")
    data = result.model_dump()
    location = data["source_map"]["locations"][0]
    location.update(node_key="n-FAKE", field="attribute", attribute_name="alt")
    with pytest.raises(ValidationError, match="authoritative source slot"):
        AtomicDocument.model_validate(data)
    for owner in ["missing", None]:
        data = result.model_dump()
        data["items"][1]["region"]["patch_owner_id"] = owner
        data["document"]["units"][1]["region"]["patch_owner_id"] = owner
        with pytest.raises(ValidationError, match="containing source body"):
            AtomicDocument.model_validate(data)


def test_saved_map_cannot_drop_protection_or_downgrade_whole_atomic_elements() -> None:
    result = extract_resource(source("<p>Whole paragraph</p>"), "OPS/chapter.xhtml", "source")
    data = result.model_dump()
    data["source_map"]["protected_spans"] = ()
    with pytest.raises(ValidationError, match="cover every"):
        AtomicDocument.model_validate(data)
    data = result.model_dump()
    data["items"][0]["kind"] = "paragraph"
    data["items"][0]["atomic_tag"] = None
    data["document"]["units"][0]["kind"] = "paragraph"
    with pytest.raises(ValidationError, match="retain their atomic tag"):
        AtomicDocument.model_validate(data)


def test_virtual_and_attribute_spans_cannot_point_away_from_their_actual_owned_bytes() -> None:
    result = extract_resource(source('Before<p>Paragraph<img alt="Image"/></p>After'), "OPS/chapter.xhtml", "source")
    data = result.model_dump()
    data["items"][0]["source_span"] = {"byte_start": 1, "byte_end": 2}
    with pytest.raises(ValidationError, match="authoritative source node"):
        AtomicDocument.model_validate(data)
    data = result.model_dump()
    virtual = result.items[0].source_span
    data["items"][0]["source_span"] = {"byte_start": virtual.byte_start + 1, "byte_end": virtual.byte_start + 2}
    with pytest.raises(ValidationError, match="actual mapped source"):
        AtomicDocument.model_validate(data)
    data = result.model_dump()
    paragraph = result.items[1].source_span
    data["items"][2]["source_span"] = {"byte_start": paragraph.byte_start + 1, "byte_end": paragraph.byte_start + 2}
    with pytest.raises(ValidationError, match="authoritative source node"):
        AtomicDocument.model_validate(data)


def test_expanded_attribute_span_cannot_cancel_the_required_parent_patch() -> None:
    result = extract_resource(source('<p>Body<img alt="Image"/></p><p>After</p>'), "OPS/chapter.xhtml", "source")
    data = result.model_dump()
    parent = result.items[0].source_span
    data["items"][1]["source_span"] = {"byte_start": parent.byte_start, "byte_end": parent.byte_end + 1}
    del data["items"][1]["region"]["patch_owner_id"]
    del data["document"]["units"][1]["region"]["patch_owner_id"]
    with pytest.raises(ValidationError, match="authoritative source node"):
        AtomicDocument.model_validate(data)
