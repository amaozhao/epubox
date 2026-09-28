from __future__ import annotations

from engine.item.extractor import extract_document as extract_document_v23
from engine.item.extractor_v25 import extract_document
from engine.item.source_views import validate_source_views
from engine.schemas.v25 import DOCUMENT_FORMAT, DocumentPlan


def _source(body: str) -> str:
    return (
        '<?xml version="1.0"?>\r\n<!DOCTYPE html>\r\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Example book</title></head>'
        f"<body>{body}</body></html>"
    )


def test_adapter_preserves_source_plan_and_adds_replayable_views() -> None:
    source = _source(
        '<p>Use pro<em>cess</em>or <code>run()</code> safely.<img src="chart.png" alt="System chart"/></p>'
    )
    old = extract_document_v23(source, "OPS/chapter.xhtml", "source-sha")
    document = extract_document(source, "OPS/chapter.xhtml", "source-sha")

    assert isinstance(document, DocumentPlan)
    assert document.format == DOCUMENT_FORMAT
    assert document.source_markup == old.source_markup == source
    assert document.document_id == old.document_id
    assert document.adapter_version == old.adapter_version
    assert document.extractor_version == old.extractor_version
    assert document.nodes.keys() == old.nodes.keys()
    assert {
        key: (
            slot.node_key,
            slot.field,
            slot.source_value,
            [part.model_dump(mode="json") for part in slot.ranges],
            slot.attribute_name,
        )
        for key, slot in document.source_slots.items()
    } == {
        key: (
            slot.node_key,
            slot.field,
            slot.source_value,
            [part.model_dump(mode="json") for part in slot.ranges],
            slot.attribute_name,
        )
        for key, slot in old.source_slots.items()
    }
    assert [(unit.unit_id, unit.source_projection, unit.slot_ids) for unit in document.units] == [
        (unit.unit_id, unit.source_projection, unit.slot_ids) for unit in old.units
    ]

    paragraph = next(unit for unit in document.units if unit.kind == "paragraph")
    texts = [document.source_views[view_id].text for view_id in paragraph.source_view_ids]
    assert texts == ["Use processor ", " safely."]
    assert all("run()" not in text for text in texts)
    assert any(unit.kind == "attribute" and unit.source_projection == "System chart" for unit in document.units)
    validate_source_views(document)


def test_adapter_is_deterministic_and_ignores_pre_freeze_term_configuration() -> None:
    source = _source("<p>The control plane uses a scheduler.</p>")
    first = extract_document(source, "OPS/chapter.xhtml", "source-sha")
    second = extract_document(
        source,
        "OPS/chapter.xhtml",
        "source-sha",
        config={
            "terms": [
                {
                    "source": "control plane",
                    "target": "控制平面",
                    "scope": "book",
                    "mode": "required",
                    "note": "must not enter DocumentPlan",
                }
            ]
        },
    )

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert all("logical_hash" not in type(unit).model_fields for unit in first.units)


def test_adapter_keeps_comment_and_pi_tails_without_structural_whitespace_views() -> None:
    for inline in ("Before<!--keep-->after", "Before<?keep data?>after"):
        document = extract_document(_source(f"  \n<p>{inline}</p>\n  "), "OPS/chapter.xhtml", "source-sha")
        paragraph = next(unit for unit in document.units if unit.kind == "paragraph")

        assert [document.source_views[view_id].text for view_id in paragraph.source_view_ids] == [
            "Before",
            "after",
        ]
        assert all(view.text.strip() for view in document.source_views.values())
        assert any(boundary["kind"] == "non_element_tail" for boundary in document.boundaries)
