from __future__ import annotations

from copy import deepcopy

import pytest
from lxml import etree  # type: ignore[attr-defined]
from pydantic import ValidationError

from engine.epub.fill import FillError, fill_resource
from engine.epub.parsing import parse_resource
from engine.item.atoms import extract_resource
from engine.schemas.bridge import AtomicDocument

XHTML = "http://www.w3.org/1999/xhtml"


def source(body: str, head: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\r\n'
        f'<html xmlns="{XHTML}" xmlns:z="urn:test"><head>{head}</head><body>{body}</body></html>'
    ).encode()


def targets(inventory: AtomicDocument) -> dict[str, str]:
    return {item.item_id: item.source_projection for item in inventory.items}


def test_parent_and_attribute_are_one_nonoverlapping_patch() -> None:
    raw = source(
        '<p id="p1">Use <em>this</em>, <i>that</i><img src="a.png" alt="Image &amp; label"/>.</p>'
        "<!--keep--><?go value?>Tail",
        '<style>.x::before{content:"&amp;"}</style><script>let x = "&lt;";</script>',
    )
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    values = targets(inventory)
    paragraph = next(item for item in inventory.items if item.atomic_tag == "p")
    attribute = next(item for item in inventory.items if item.channel == "attribute")
    tail = next(item for item in inventory.items if "Tail" in item.source_projection)
    values[paragraph.item_id] = (
        paragraph.source_projection.replace("Use ", "使用").replace("this", "这个").replace("that", "那个")
    )
    values[attribute.item_id] = '图像 "A" & B'
    values[tail.item_id] = tail.source_projection.replace("Tail", "结尾")

    result = fill_resource(raw, inventory, values)

    assert b'<style>.x::before{content:"&amp;"}</style><script>let x = "&lt;";</script>' in result
    assert b"<!--keep--><?go value?>" in result
    tree = etree.fromstring(result)
    paragraph_node = tree.find(f".//{{{XHTML}}}p")
    image = tree.find(f".//{{{XHTML}}}img")
    assert paragraph_node is not None and "使用" in "".join(paragraph_node.itertext())
    assert image is not None and image.get("alt") == '图像 "A" & B'
    assert image.get("src") == "a.png" and paragraph_node.get("id") == "p1"
    assert result.count(b'alt="') == 1


def test_unchanged_nested_attribute_keeps_its_original_entity_bytes() -> None:
    raw = source('<p>Source<img src="a.png" alt="A&#38;B"/></p>')
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    values = targets(inventory)
    paragraph = next(item for item in inventory.items if item.atomic_tag == "p")
    values[paragraph.item_id] = paragraph.source_projection.replace("Source", "来源")

    result = fill_resource(raw, inventory, values)

    assert b'alt="A&#38;B"' in result


def test_identity_validates_every_input_and_preserves_bytes() -> None:
    raw = source("<p>A &amp; B</p>")
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    values = targets(inventory)

    assert fill_resource(raw, inventory, values, identity=True) == raw
    with pytest.raises(FillError, match="identity"):
        changed = values | {inventory.items[0].item_id: "changed"}
        fill_resource(raw, inventory, changed, identity=True)
    with pytest.raises(FillError, match="target IDs"):
        fill_resource(raw, inventory, {})
    with pytest.raises(FillError, match="marker"):
        marked = extract_resource(source("<p>Use <em>this</em></p>"), "OPS/chapter.xhtml", "book")
        item = marked.items[0]
        fill_resource(raw=source("<p>Use <em>this</em></p>"), inventory=marked, targets={item.item_id: "broken"})


def test_utf16_cdata_keeps_its_shell_and_safely_splits_cdata_end() -> None:
    markup = (
        '<?xml version="1.0" encoding="UTF-16LE"?>'
        f'<html xmlns="{XHTML}"><head/><body><p><![CDATA[Source < text]]></p></body></html>'
    )
    raw = b"\xff\xfe" + markup.encode("utf-16-le")
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    item = inventory.items[0]
    wanted = "中文 ]]> 继续"

    result = fill_resource(raw, inventory, {item.item_id: wanted})

    assert result.startswith(b"\xff\xfe")
    decoded = result[2:].decode("utf-16-le")
    assert "<![CDATA[中文 ]]]]><![CDATA[> 继续]]>" in decoded
    parsed = parse_resource(result, "application/xhtml+xml")
    paragraph = parsed.tree.find(f".//{{{XHTML}}}p") if parsed.tree is not None else None
    assert paragraph is not None and paragraph.text == wanted


def test_cdata_literal_marker_is_restored_inside_cdata() -> None:
    raw = source("<p><![CDATA[Source ⟦=x9⟧ text]]></p>")
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    item = inventory.items[0]
    value = item.source_projection.replace("Source", "来源").replace("text", "文本")

    result = fill_resource(raw, inventory, {item.item_id: value})

    assert "<![CDATA[来源 ⟦=x9⟧ 文本]]>" in result.decode()


def test_inline_leaf_cdata_keeps_its_lexical_wrapper() -> None:
    raw = source("<p>Use <em><![CDATA[English]]></em> tail</p>")
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    item = inventory.items[0]
    value = item.source_projection.replace("Use", "使用").replace("English", "英文").replace("tail", "结尾")

    result = fill_resource(raw, inventory, {item.item_id: value})

    assert "<em><![CDATA[英文]]></em>" in result.decode()


def test_mixed_cdata_is_refused_instead_of_normalized() -> None:
    raw = source("<p><![CDATA[English]]><em>word</em></p>")
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    item = inventory.items[0]
    value = item.source_projection.replace("English", "英文").replace("word", "词")

    with pytest.raises(FillError, match="mixed CDATA"):
        fill_resource(raw, inventory, {item.item_id: value})


def test_namespace_entities_and_special_nodes_remain_strict_xml() -> None:
    raw = source('<p z:role="fixed">A&#160;B</p><!--c--><?pi data?>After')
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    values = targets(inventory)
    paragraph = next(item for item in inventory.items if item.atomic_tag == "p")
    tail = next(item for item in inventory.items if "After" in item.source_projection)
    values[paragraph.item_id] = "甲 & 乙 < 丙"
    values[tail.item_id] = tail.source_projection.replace("After", "后来")

    result = fill_resource(raw, inventory, values)

    assert b'xmlns:z="urn:test"' in result and b'z:role="fixed"' in result
    assert b"<!--c--><?pi data?>" in result
    assert b"&amp;" in result and b"&lt;" in result
    assert parse_resource(result, "application/xhtml+xml").tree is not None


def test_only_whitelisted_head_title_and_description_change() -> None:
    head = (
        '<title>Source title</title><meta name="description" content="Source &amp; description"/>'
        '<meta name="fixed" content="fixed"/><style>.x{color:red}</style><script>let fixed = 1;</script>'
    )
    raw = source("<p>Body</p>", head)
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    values = targets(inventory)
    title = next(item for item in inventory.items if item.kind == "head_title")
    description = next(item for item in inventory.items if item.region.get("attribute_name") == "content")
    values[title.item_id] = "中文书名"
    values[description.item_id] = '中文 "说明" & 详情'

    result = fill_resource(raw, inventory, values)

    assert b'<meta name="fixed" content="fixed"/>' in result
    assert b"<style>.x{color:red}</style><script>let fixed = 1;</script>" in result
    tree = etree.fromstring(result)
    assert tree.findtext(f".//{{{XHTML}}}title") == "中文书名"
    meta = tree.find(f'.//{{{XHTML}}}meta[@name="description"]')
    assert meta is not None and meta.get("content") == '中文 "说明" & 详情'


def test_raw_identity_and_authoritative_spans_cannot_be_tampered() -> None:
    raw = source("Before sentence. Second sentence.<p>Paragraph</p>")
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    with pytest.raises(FillError, match="raw resource"):
        fill_resource(raw + b" ", inventory, targets(inventory))

    data = deepcopy(inventory.model_dump())
    first = data["items"][0]
    first["source_span"]["byte_start"] -= 1
    tampered = AtomicDocument.model_validate(data)
    with pytest.raises(FillError, match="authoritative"):
        fill_resource(raw, tampered, targets(tampered))


def test_registry_cannot_redirect_an_opaque_node_to_another_item() -> None:
    raw = source("<p>First <code>one</code>.</p><p>Second <code>two</code>.</p>")
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    first, second = inventory.items
    first_ref = next(ref for ref in first.registry if ref.startswith("x"))
    second_ref = next(ref for ref in second.registry if ref.startswith("x"))
    data = deepcopy(inventory.model_dump())
    redirected = data["items"][0]["registry"][first_ref]
    redirected["source_node_key"] = data["items"][1]["registry"][second_ref]["source_node_key"]
    data["document"]["units"][0]["registry"][first_ref] = deepcopy(redirected)
    tampered = AtomicDocument.model_validate(data)

    with pytest.raises(FillError, match="escapes|DOM parent"):
        fill_resource(raw, tampered, targets(tampered))


def test_registry_literal_marker_cannot_redirect_to_another_slot() -> None:
    raw = source("<p>First ⟦=x1⟧.</p><p>Second ⟦=x2⟧.</p>")
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    first, second = inventory.items
    first_ref = next(ref for ref in first.registry if ref.startswith("x"))
    second_ref = next(ref for ref in second.registry if ref.startswith("x"))
    data = deepcopy(inventory.model_dump())
    redirected = data["items"][0]["registry"][first_ref]
    evidence = data["items"][1]["registry"][second_ref]
    redirected["source_node_key"] = evidence["source_node_key"]
    redirected["source_text"] = evidence["source_text"]
    redirected["hints"] = deepcopy(evidence["hints"])
    data["document"]["units"][0]["registry"][first_ref] = deepcopy(redirected)
    tampered = AtomicDocument.model_validate(data)

    with pytest.raises(FillError, match="escapes|DOM parent"):
        fill_resource(raw, tampered, targets(tampered))


def test_invalid_saved_inventory_is_rejected_before_rendering() -> None:
    raw = source("<p>Paragraph</p>")
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    data = inventory.model_dump()
    data["source_map"]["document_hash"] = "0" * 64
    with pytest.raises(ValidationError):
        AtomicDocument.model_validate(data)
