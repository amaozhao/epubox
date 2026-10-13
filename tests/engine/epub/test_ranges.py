from __future__ import annotations

import codecs
from itertools import pairwise

import pytest

from engine.epub.parsing import parse_resource
from engine.epub.ranges import RangeError, index_resource
from engine.item.atoms import extract_resource
from engine.item.extractor import extract_document

XHTML = "http://www.w3.org/1999/xhtml"


def _slot(index, path, field, attribute=None, special=None):
    return next(
        item
        for item in index.slots
        if item.path == path
        and item.field == field
        and item.attribute_name == attribute
        and item.special_index == special
    )


def test_expat_events_map_entities_cdata_special_tails_and_quoted_tags() -> None:
    source = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<!DOCTYPE html SYSTEM "xhtml11.dtd">'
        f'<html xmlns="{XHTML}"><head><script>const fake = "&lt;/head>";</script></head>'
        '<body><p title="x&gt;y">A&nbsp;B<![CDATA[<c>]]>\r\nZ</p>'
        '<!-- fake <head> -->tail<?keep value?>pi<img alt="chart"/></body></html>'
    )
    parsed = parse_resource(source.encode(), "application/xhtml+xml")

    index = index_resource(parsed)

    assert index.replay() is parsed.raw
    assert index.nodes[()].qname == f"{{{XHTML}}}html"
    assert index.nodes[(1, 0)].qname == f"{{{XHTML}}}p"
    assert index.nodes[(1, 1)].self_closing
    assert parsed.raw[index.nodes[(0, 0)].full.start : index.nodes[(0, 0)].full.end].startswith(b"<script>")

    attribute = _slot(index, (1, 0), "attribute", "title")
    assert attribute.text == "x>y"
    span = attribute.source_span(0, 3)
    assert parsed.raw[span.byte_start : span.byte_end] == b"x&gt;y"

    text = _slot(index, (1, 0), "text")
    assert text.text == "A\N{NO-BREAK SPACE}B<c>\nZ"
    entity = text.source_span(1, 2)
    assert parsed.raw[entity.byte_start : entity.byte_end] == b"&nbsp;"
    cdata = text.source_span(3, 6)
    assert parsed.raw[cdata.byte_start : cdata.byte_end] == b"<c>"
    with pytest.raises(RangeError, match="non-text XML syntax"):
        text.source_span(0, len(text.text))

    assert _slot(index, (1,), "tail", special=1).text == "tail"
    assert _slot(index, (1,), "tail", special=2).text == "pi"


@pytest.mark.parametrize(
    ("encoding", "bom"),
    [("utf-8", codecs.BOM_UTF8), ("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE)],
)
def test_utf_encodings_bom_crlf_and_multibyte_positions(encoding: str, bom: bytes) -> None:
    source = f'<?xml version="1.0" encoding="{encoding}"?><r><p>中文\r\nA&amp;B</p></r>'
    raw = bom + source.encode(encoding)
    parsed = parse_resource(raw, "application/xml")

    index = index_resource(parsed)
    text = _slot(index, (0,), "text")

    assert text.text == "中文\nA&B"
    span = text.source_span(0, len(text.text))
    assert raw[span.byte_start : span.byte_end].decode(encoding) == "中文\r\nA&amp;B"
    assert index.nodes[()].full.start == len(bom) + len(source[: source.index("<r>")].encode(encoding))


def test_document_binding_maps_only_owned_ranges_and_protects_the_complement() -> None:
    source = (
        f'<html xmlns="{XHTML}"><head><title>Book</title><style>.x{{color:red}}</style>'
        '<script>const fake = "&lt;head>";</script></head><body>'
        '<p title="Tip">Alpha &amp; beta.</p></body></html>'
    )
    parsed = parse_resource(source.encode(), "application/xhtml+xml")
    index = index_resource(parsed)
    document = extract_document(parsed.text, "OPS/chapter.xhtml", "book-source")

    mapping = index.bind_document(document)

    assert mapping.document_hash
    assert mapping.source_size == len(parsed.raw)
    assert mapping.locations
    assert any(item.field == "attribute" and item.attribute_name == "title" for item in mapping.locations)
    spans = sorted(
        [item.byte_span for item in mapping.locations] + list(mapping.protected_spans),
        key=lambda item: item.byte_start,
    )
    assert spans[0].byte_start == 0
    assert spans[-1].byte_end == len(parsed.raw)
    assert all(left.byte_end == right.byte_start for left, right in pairwise(spans))
    style = b".x{color:red}"
    style_start = parsed.raw.index(style)
    assert any(
        span.byte_start <= style_start and style_start + len(style) <= span.byte_end
        for span in mapping.protected_spans
    )


def test_long_utf16_cdata_normalizes_lines_without_changing_replay_bytes() -> None:
    script = "let value = 1;\r\n" * 300
    source = (
        '<?xml version="1.0" encoding="utf-16-le"?>'
        f'<html xmlns="{XHTML}"><head><script><![CDATA[{script}]]></script></head><body/></html>'
    )
    raw = codecs.BOM_UTF16_LE + source.encode("utf-16-le")
    parsed = parse_resource(raw, "application/xhtml+xml")

    index = index_resource(parsed)
    text = _slot(index, (0, 0), "text")

    assert text.text == script.replace("\r\n", "\n")
    assert index.replay() is raw
    document = extract_document(parsed.text, "OPS/chapter.xhtml", "book-source")
    mapping = index.bind_document(document)
    script_bytes = script.encode("utf-16-le")
    script_start = raw.index(script_bytes)
    assert any(
        span.byte_start <= script_start and script_start + len(script_bytes) <= span.byte_end
        for span in mapping.protected_spans
    )


@pytest.mark.parametrize(
    ("body", "expected_items"),
    [
        ('<p>Text<img alt="" src="x.png"/></p>', 1),
        ("<p><![CDATA[]]></p>", 0),
        ("<p><!--keep--><![CDATA[]]></p>", 0),
    ],
)
def test_explicit_empty_slots_replay_without_creating_model_items(body: str, expected_items: int) -> None:
    raw = f'<html xmlns="{XHTML}"><head/><body>{body}</body></html>'.encode()

    result = extract_resource(raw, "OPS/chapter.xhtml", "book-source")

    assert len(result.items) == expected_items
    assert all(item.source_projection for item in result.items)
    parsed = parse_resource(raw, "application/xhtml+xml")
    index = index_resource(parsed)
    assert index.replay() is raw
    assert any(slot.text == "" for slot in index.slots)


def test_html_and_semantic_tree_mismatch_are_refused() -> None:
    html = parse_resource(b"<!doctype html><html><body><p>one<p>two</body></html>", "text/html")
    with pytest.raises(RangeError, match="genuine HTML"):
        index_resource(html)

    source = f'<html xmlns="{XHTML}"><body><p>text</p></body></html>'
    parsed = parse_resource(source.encode(), "application/xhtml+xml")
    index = index_resource(parsed)
    document = extract_document(parsed.text, "OPS/chapter.xhtml", "book-source")
    paragraph = next(record for record in document.nodes.values() if record.element_path == (0, 0))
    changed = paragraph.model_copy(update={"qname": "wrong"})
    nodes = document.nodes | {paragraph.node_key: changed}

    with pytest.raises(RangeError, match="QName differs"):
        index.bind_document(document.model_copy(update={"nodes": nodes}))


def test_comment_and_processing_instruction_have_verified_raw_spans() -> None:
    raw = f'<html xmlns="{XHTML}"><body>A<!--keep--><?go value?>B</body></html>'.encode()
    index = index_resource(parse_resource(raw, "application/xhtml+xml"))

    assert [raw[span.start : span.end] for span in index.specials.values()] == [b"<!--keep-->", b"<?go value?>"]
