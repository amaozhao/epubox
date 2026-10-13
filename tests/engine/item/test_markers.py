from __future__ import annotations

import json

import pytest

from engine.agents.protocol import validate_translation_response
from engine.item.atoms import extract_resource
from engine.item.inline import (
    Event,
    ProjectionError,
    events_to_projection,
    normalize_empty_closes,
    parse_projection,
    validate_item_target,
)

XHTML = "http://www.w3.org/1999/xhtml"


def source(body: str) -> bytes:
    return f'<html xmlns="{XHTML}"><head/><body>{body}</body></html>'.encode()


def test_literal_control_markers_become_local_protected_objects() -> None:
    result = extract_resource(
        source('<p>Before ⟦+g1⟧ and ⟦=x1⟧<img src="a.png" alt="Image ⟦=x9⟧"/> tail.</p>'),
        "OPS/chapter.xhtml",
        "source",
    )
    paragraph, attribute = result.items

    assert paragraph.atomic_tag == "p"
    assert "\\⟦+g1\\⟧" not in paragraph.source_projection
    assert "\\⟦=x1\\⟧" not in paragraph.source_projection
    literals = [entry for entry in paragraph.registry.values() if entry.boundary_type == "literal_marker"]
    assert [entry.source_text for entry in literals] == ["⟦+g1⟧", "⟦=x1⟧"]
    assert all({"slot_id", "start", "end"} <= entry.hints.keys() for entry in literals)

    assert attribute.channel == "attribute"
    literal = next(entry for entry in attribute.registry.values() if entry.boundary_type == "literal_marker")
    assert literal.source_text == "⟦=x9⟧"
    view = result.document.source_views[attribute.source_view_ids[0]]
    assert view.text == "Image "
    assert (
        "".join(
            result.document.source_slots[ref.slot_id].source_value[ref.start : ref.end] for ref in view.source_refs
        )
        == view.text
    )
    assert validate_item_target(paragraph, paragraph.source_projection)
    assert validate_item_target(attribute, attribute.source_projection)
    with pytest.raises(ProjectionError, match="literal marker boundary"):
        validate_item_target(attribute, "⟦=x1⟧图像")

    bad = literal.model_copy(update={"hints": literal.hints | {"end": "0"}})
    with pytest.raises(ProjectionError, match="invalid source hints"):
        validate_item_target(attribute.model_copy(update={"registry": {"x1": bad}}), attribute.source_projection)


def test_item_target_rejects_broken_or_foreign_marker_structure() -> None:
    item = extract_resource(
        source("<p>Use <em>this</em> and <code>pip install demo</code>.</p>"),
        "OPS/chapter.xhtml",
        "source",
    ).items[0]
    refs = tuple(item.registry)
    group = next(ref for ref in refs if ref.startswith("g"))
    atom = next(ref for ref in refs if ref.startswith("x"))
    source_target = item.source_projection

    candidates = (
        source_target.replace(f"⟦={atom}⟧", ""),
        source_target.replace(f"⟦={atom}⟧", f"⟦={atom}⟧⟦={atom}⟧"),
        source_target.replace(f"⟦={atom}⟧", "⟦=x999⟧"),
        f"⟦+{group}⟧译文⟦={atom}⟧⟦-{group}⟧",
    )
    for target in candidates:
        with pytest.raises(ProjectionError):
            validate_item_target(item, target)


def test_batch_identity_owns_local_marker_names() -> None:
    items = extract_resource(
        source("<p>First ⟦=x1⟧.</p><p>Second ⟦=x1⟧.</p>"),
        "OPS/chapter.xhtml",
        "source",
    ).items
    assert all(tuple(item.registry) == ("x1",) for item in items)
    by_id = {item.item_id: item for item in items}
    request_id = "request-1"
    response = json.dumps(
        {
            "protocol": "epubox-text-1",
            "request_id": request_id,
            "items": [{"item_id": item.item_id, "target": item.source_projection} for item in reversed(items)],
        },
        ensure_ascii=False,
    )
    parsed = validate_translation_response(response, request_id, set(by_id))
    assert not parsed.errors and not parsed.missing and not parsed.unknown
    for item_id, value in parsed.accepted.items():
        validate_item_target(by_id[item_id], value["target"])

    foreign = json.dumps(
        {
            "protocol": "epubox-text-1",
            "request_id": request_id,
            "items": [{"item_id": "foreign", "target": items[0].source_projection}],
        },
        ensure_ascii=False,
    )
    rejected = validate_translation_response(foreign, request_id, set(by_id))
    assert rejected.unknown == ("foreign",)
    assert set(rejected.missing) == set(by_id)


def test_literal_marker_protection_keeps_atomic_owner_and_external_tail() -> None:
    result = extract_resource(
        source("<p>Whole ⟦+g7⟧ paragraph.</p>Outside tail."),
        "OPS/chapter.xhtml",
        "source",
    )
    paragraph, tail = result.items
    assert paragraph.atomic_tag == "p"
    assert paragraph.source_span == result.source_map.node_spans[paragraph.node_key]
    assert tail.atomic_tag is None
    assert any(entry.source_text == "⟦+g7⟧" for entry in paragraph.registry.values())


def test_pure_protected_content_needs_no_model_item_and_xml_controls_are_rejected() -> None:
    assert not extract_resource(
        source("<p><code>pip install demo</code></p>"),
        "OPS/chapter.xhtml",
        "source",
    ).items

    attribute = extract_resource(
        source('<img src="a.png" alt="Image ⟦=x9⟧"/>'),
        "OPS/chapter.xhtml",
        "source",
    ).items[0]
    target = events_to_projection(
        Event(kind=event.kind, value=event.value if event.kind == "marker" else event.value + "\x01")
        for event in parse_projection(attribute.source_projection)
    )
    with pytest.raises(ProjectionError, match="XML-invalid"):
        validate_item_target(attribute, target)


def test_only_redundant_closes_for_source_empty_groups_are_normalized() -> None:
    source_projection = "Before ⟦=x1⟧⟦+g1⟧⟦-g1⟧ after."
    target = "之前 ⟦=x1⟧\\⟦-g1\\⟧⟦+g1⟧⟦-g1⟧ 之后。⟦-g1⟧"
    assert normalize_empty_closes(source_projection, target) == target.removesuffix("⟦-g1⟧")

    unchanged = (
        ("⟦+g1⟧source⟦-g1⟧", "⟦+g1⟧译文⟦-g1⟧⟦-g1⟧"),
        (
            "⟦+g1⟧⟦-g1⟧⟦+g2⟧source⟦-g2⟧",
            "⟦+g1⟧⟦-g1⟧⟦+g2⟧⟦-g1⟧译文⟦-g2⟧",
        ),
        (source_projection, "译文⟦-g1⟧"),
        ("⟦+b1⟧⟦-b1⟧", "⟦+b1⟧⟦-b1⟧⟦-b1⟧"),
        (source_projection, "译文⟦+g1⟧内容⟦-g1⟧⟦-g1⟧"),
        ("⟦+g1⟧⟦+g2⟧⟦-g1⟧⟦-g2⟧", target),
    )
    for source_value, target_value in unchanged:
        assert normalize_empty_closes(source_value, target_value) == target_value
