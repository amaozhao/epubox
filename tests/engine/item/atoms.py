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

    assert all(item.kind != "head_title" for item in result.items)
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
    legacy = extract_resource(
        raw,
        "OPS/nav.xhtml",
        "book-source",
        extractor_version="epubox-atomic-1",
    )

    assert [item.kind for item in current.items] == ["navigation"]
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
