from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest

from engine.epub.assembly import assemble_document
from engine.item.extractor import extract_document, validate_source_relations
from engine.item.inline import ProjectionError, validate_projection
from engine.item.planner import MAX_SOURCE_TOKENS, source_token_count
from engine.item.source_views import validate_source_views
from engine.item.structural_extractor import extract_document as extract_structure
from engine.schemas.contracts import DOCUMENT_FORMAT, DocumentPlan


def _source(body: str) -> str:
    return (
        '<?xml version="1.0"?>\r\n<!DOCTYPE html>\r\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        "<head><title>Example book</title></head>"
        f"<body>{body}</body></html>"
    )


def _boundary_units(boundary: Mapping[str, object]) -> list[str]:
    unit_ids = boundary["unit_ids"]
    assert isinstance(unit_ids, list) and all(isinstance(unit_id, str) for unit_id in unit_ids)
    return unit_ids


def _boundary_edges(boundary: Mapping[str, object]) -> list[dict[str, str]]:
    edges = boundary["relation_edges"]
    assert isinstance(edges, list)
    assert all(
        isinstance(edge, dict)
        and set(edge) == {"from_unit_id", "to_unit_id", "kind"}
        and all(isinstance(value, str) for value in edge.values())
        for edge in edges
    )
    return edges


def test_adapter_preserves_source_plan_and_adds_replayable_views() -> None:
    source = _source(
        '<p>Use pro<em>cess</em>or <code>run()</code> safely.<img src="chart.png" alt="System chart"/></p>'
    )
    old = extract_structure(source, "OPS/chapter.xhtml", "source-sha")
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


def test_adjacent_paragraphs_form_one_ordered_unit_with_separate_source_views() -> None:
    source = _source(
        "<h2>First section</h2><p>Alpha <em>one</em>.</p><p>Beta two.</p><p>Gamma three.</p>"
        "<h2>Second section</h2><p>Delta four.</p><p>Epsilon five.</p>"
    )
    document = extract_document(source, "OPS/chapter.xhtml", "source-sha")
    groups = [unit for unit in document.units if unit.kind == "paragraph_group"]

    assert [len(cast(list[str], unit.region["member_node_keys"])) for unit in groups] == [3, 2]
    assert [document.source_views[view_id].text for view_id in groups[0].source_view_ids] == [
        "Alpha one.",
        "Beta two.",
        "Gamma three.",
    ]
    assert source.split("<body>", 1)[1] in assemble_document(document, {}, identity=True).markup
    validate_source_views(document)

    first, second = tuple(entry.ref_id for entry in groups[0].registry.values() if entry.hints.get("element") == "p")[
        :2
    ]
    projection = groups[0].source_projection
    first_end = projection.index(f"⟦-{first}⟧") + len(f"⟦-{first}⟧")
    second_end = projection.index(f"⟦-{second}⟧") + len(f"⟦-{second}⟧")
    swapped = projection[first_end:second_end] + projection[:first_end] + projection[second_end:]
    with pytest.raises(ProjectionError):
        validate_projection(groups[0], swapped)


def test_grouping_stops_at_comments_and_translation_boundaries() -> None:
    source = _source(
        "<p>Alpha.</p><p>Beta.</p><!--keep--><p>Gamma.</p><p>Delta.</p>"
        '<p translate="no">Protected.</p><p>Epsilon.</p><p>Zeta.</p>'
    )
    document = extract_document(source, "OPS/chapter.xhtml", "source-sha")
    groups = [unit for unit in document.units if unit.kind == "paragraph_group"]

    assert len(groups) == 1
    assert [document.source_views[view_id].text for view_id in groups[0].source_view_ids] == [
        "Epsilon.",
        "Zeta.",
    ]
    assert source.split("<body>", 1)[1] in assemble_document(document, {}, identity=True).markup


def test_short_paragraph_run_becomes_one_unit() -> None:
    source = _source("".join(f"<p>Paragraph {index} text.</p>" for index in range(20)))
    document = extract_document(source, "OPS/chapter.xhtml", "source-sha")
    groups = [unit for unit in document.units if unit.kind == "paragraph_group"]

    assert [len(cast(list[str], unit.region["member_node_keys"])) for unit in groups] == [20]
    assert source.split("<body>", 1)[1] in assemble_document(document, {}, identity=True).markup


def test_long_paragraph_run_uses_exact_source_projection_limit() -> None:
    body = "\n".join(
        f"<p>Paragraph {index}. " + "Technical words about systems. " * 12 + "</p>" for index in range(60)
    )
    source = _source(body)
    document = extract_document(source, "OPS/chapter.xhtml", "source-sha")
    groups = [unit for unit in document.units if unit.kind == "paragraph_group"]

    assert len(groups) > 1
    assert sum(len(cast(list[str], unit.region["member_node_keys"])) for unit in groups) == 60
    assert all(source_token_count(unit.source_projection) <= MAX_SOURCE_TOKENS for unit in groups)
    assert body in assemble_document(document, {}, identity=True).markup


def test_group_singleton_group_preserves_intervening_newlines() -> None:
    paragraphs = [f"<p>Opening paragraph {index}.</p>" for index in range(5)]
    paragraphs.append("<p>" + "word " * 1300 + "</p>")
    paragraphs.extend(f"<p>Closing paragraph {index}.</p>" for index in range(5))
    body = "\n".join(paragraphs)
    document = extract_document(_source(body), "OPS/chapter.xhtml", "source-sha")

    assert sum(unit.kind == "paragraph_group" for unit in document.units) == 2
    assert sum(unit.kind == "paragraph" for unit in document.units) == 1
    assert body in assemble_document(document, {}, identity=True).markup


def test_paragraph_group_cannot_hide_a_following_heading_by_erasing_its_boundary() -> None:
    document = extract_document(_source("<p>Alpha.</p><p>Beta.</p><h2>Keep heading</h2>"), "chapter.xhtml", "sha")
    payload = document.model_dump(mode="python")
    group = next(unit for unit in payload["units"] if unit["kind"] == "paragraph_group")
    group["region"]["before_node_key"] = None

    with pytest.raises(ValueError, match="paragraph group boundaries"):
        DocumentPlan.model_validate(payload)


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


def test_adapter_persists_table_rows_with_explicit_header_context() -> None:
    document = extract_document(
        _source(
            '<table><thead><tr><th id="metric" scope="col">Metric</th>'
            '<th id="value" scope="col">Value</th></tr></thead><tbody>'
            '<tr><td headers="metric">Latency</td><td headers="value">10 ms</td></tr>'
            '<tr><td headers="metric">Throughput</td><td headers="value">20 ops</td></tr>'
            "</tbody></table>"
        ),
        "OPS/chapter.xhtml",
        "source-sha",
    )
    units = {unit.unit_id: unit.source_projection for unit in document.units}
    rows = [_boundary_units(boundary) for boundary in document.boundaries if boundary["kind"] == "table_row"]
    edges = [_boundary_edges(boundary) for boundary in document.boundaries if boundary["kind"] == "table_row"]
    rendered = [[units[unit_id] for unit_id in row] for row in rows]
    rendered_edges = [
        [[units[edge["from_unit_id"]], units[edge["to_unit_id"]], edge["kind"]] for edge in relation_edges]
        for relation_edges in edges
    ]

    assert rendered == [
        ["Metric", "Value"],
        ["Latency", "10 ms"],
        ["Throughput", "20 ops"],
    ]
    assert rendered_edges == [
        [["Metric", "Value", "table_row"]],
        [
            ["Latency", "10 ms", "table_row"],
            ["Metric", "Latency", "table_header"],
            ["Value", "10 ms", "table_header"],
        ],
        [
            ["Throughput", "20 ops", "table_row"],
            ["Metric", "Throughput", "table_header"],
            ["Value", "20 ops", "table_header"],
        ],
    ]


def test_adapter_persists_footnote_reference_and_skips_note_in_narrative_adjacency() -> None:
    document = extract_document(
        _source(
            '<p>Body claim<a epub:type="noteref" href="#note-1">1</a>'
            '<a epub:type="noteref" href="#note-2">2</a>.</p>'
            '<aside epub:type="footnote" id="note-1"><p>Note explanation.</p></aside>'
            '<aside epub:type="footnote" id="note-2"><p>Second note.</p></aside>'
            "<p>Following narrative.</p>"
        ),
        "OPS/chapter.xhtml",
        "source-sha",
    )
    units = {unit.unit_id: unit.source_projection for unit in document.units}
    footnotes = [
        [units[unit_id] for unit_id in _boundary_units(boundary)]
        for boundary in document.boundaries
        if boundary["kind"] == "footnote_reference"
    ]
    narrative = [
        [units[unit_id] for unit_id in _boundary_units(boundary)]
        for boundary in document.boundaries
        if boundary["kind"] == "narrative_adjacent"
    ]

    assert footnotes == [
        [
            next(value for value in units.values() if "Body claim" in value),
            "Note explanation.",
            "Second note.",
        ]
    ]
    footnote_boundary = next(boundary for boundary in document.boundaries if boundary["kind"] == "footnote_reference")
    assert [
        [units[edge["from_unit_id"]], units[edge["to_unit_id"]], edge["kind"]]
        for edge in _boundary_edges(footnote_boundary)
    ] == [
        [
            next(value for value in units.values() if "Body claim" in value),
            "Note explanation.",
            "footnote_reference",
        ],
        [
            next(value for value in units.values() if "Body claim" in value),
            "Second note.",
            "footnote_reference",
        ],
    ]
    assert narrative == [[next(value for value in units.values() if "Body claim" in value), "Following narrative."]]
    validate_source_relations(document)
    forged = document.model_copy(
        update={
            "boundaries": tuple(
                {**boundary, "unit_ids": list(reversed(_boundary_units(boundary)))}
                if boundary.get("kind") == "footnote_reference"
                else boundary
                for boundary in document.boundaries
            )
        }
    )
    try:
        validate_source_relations(forged)
    except ValueError as error:
        assert "do not match" in str(error)
    else:  # pragma: no cover - explicit assertion keeps the validator requirement visible.
        raise AssertionError("forged source relation was accepted")


def test_narrative_relations_do_not_cross_sibling_sections() -> None:
    document = extract_document(
        _source(
            "<section><h2>First</h2><p>First body.</p></section><section><h2>Second</h2><p>Second body.</p></section>"
        ),
        "OPS/chapter.xhtml",
        "source-sha",
    )
    units = {unit.unit_id: unit.source_projection for unit in document.units}
    narrative = [
        [units[unit_id] for unit_id in _boundary_units(boundary)]
        for boundary in document.boundaries
        if boundary["kind"] == "narrative_adjacent"
    ]

    assert narrative == [["First", "First body."], ["Second", "Second body."]]
