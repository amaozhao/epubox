from __future__ import annotations

from collections.abc import Mapping
from itertools import pairwise

from engine.item.extractor import extract_document
from engine.schemas.contracts import (
    DocumentPlan,
    ExtractionItem,
    NodeRecord,
    ResourceRecord,
    SlotRange,
    SourceRef,
    SourceSlot,
    SourceTextView,
    TermScope,
    Unit,
    UserTerm,
    canonical_hash,
    source_view_hash_payload,
)
from engine.services.term_planning import plan_term_extraction

EXTRACTION_IDENTITY = {
    "strategy": "full-primary-coverage",
    "prompt_version": "epubox-terms-1",
    "model": "test-model",
    "target_language": "zh-Hans",
}


def bound(primary_range: Mapping[str, object], key: str) -> int:
    value = primary_range[key]
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def relation_edge(left: str, right: str, kind: str) -> dict[str, str]:
    return {"from_unit_id": left, "to_unit_id": right, "kind": kind}


def document(document_id: str, texts: tuple[str, ...]) -> DocumentPlan:
    views: dict[str, SourceTextView] = {}
    nodes: dict[str, NodeRecord] = {}
    slots: dict[str, SourceSlot] = {}
    units: list[Unit] = []
    for index, text in enumerate(texts):
        unit_id, view_id, node_id, slot_id = (
            f"u-{document_id}-{index}",
            f"v-{document_id}-{index}",
            f"n-{index}",
            f"s-{index}",
        )
        ref = SourceRef(slot_id=slot_id, start=0, end=len(text))
        view_data = {
            "unit_id": unit_id,
            "document_id": document_id,
            "text": text,
            "source_refs": (ref,),
            "view_kind": "primary",
        }
        views[view_id] = SourceTextView(
            view_id=view_id,
            view_hash=canonical_hash(source_view_hash_payload(**view_data)),
            **view_data,
        )
        nodes[node_id] = NodeRecord(node_key=node_id, element_path=(index,), qname="p")
        slots[slot_id] = SourceSlot(
            slot_id=slot_id,
            node_key=node_id,
            field="text",
            source_value=text,
            ranges=(SlotRange(start=0, end=len(text), owner_kind="unit", owner_unit_id=unit_id),),
        )
        units.append(
            Unit(
                unit_id=unit_id,
                document_id=document_id,
                kind="paragraph",
                source_projection=text,
                node_key=node_id,
                slot_ids=(slot_id,),
                source_view_ids=(view_id,),
            )
        )
    return DocumentPlan(
        document_id=document_id,
        source_hash="source-sha",
        resource=ResourceRecord(
            path=f"OPS/{document_id}.xhtml", media_type="application/xhtml+xml", source_sha256="sha"
        ),
        adapter_version="adapter-1",
        extractor_version="extractor-1",
        source_markup="<html/>",
        nodes=nodes,
        source_slots=slots,
        source_views=views,
        units=tuple(units),
        boundaries=tuple(
            {
                "kind": "narrative_adjacent",
                "unit_ids": [left.unit_id, right.unit_id],
                "relation_edges": [relation_edge(left.unit_id, right.unit_id, "narrative")],
            }
            for left, right in pairwise(units)
        ),
    )


def term(
    term_id: str,
    source: str,
    *,
    scope: TermScope | None = None,
    aliases: tuple[str, ...] = (),
    match_policy: str = "exact",
) -> UserTerm:
    return UserTerm.model_validate(
        {
            "term_id": term_id,
            "source": source,
            "target": f"译-{source}",
            "aliases": aliases,
            "scope": scope or TermScope(kind="book"),
            "match_policy": match_policy,
        }
    )


def test_plan_covers_every_primary_view_once_with_stable_budget_and_identity() -> None:
    documents = (document("d1", ("alpha", "bravo", "charlie")), document("d2", ("delta",)))

    first = plan_term_extraction(
        documents,
        (),
        source_hash="source-sha",
        preparation_hash="prep-sha",
        extraction_identity=EXTRACTION_IDENTITY,
        max_primary_chars=10,
    )
    second = plan_term_extraction(
        documents,
        (),
        source_hash="source-sha",
        preparation_hash="prep-sha",
        extraction_identity=EXTRACTION_IDENTITY,
        max_primary_chars=10,
    )

    assert first == second
    assert first.extraction_status == "planned"
    assert first.primary_view_count == 4
    covered = [view_id for item in first.plan.items for view_id in item.view_ids]
    assert covered == ["v-d1-0", "v-d1-1", "v-d1-2", "v-d2-0"]
    assert len(covered) == len(set(covered))
    assert first.plan.extraction_http_limit == 6 * len(first.plan.items) + 3 * 20


def test_primary_transport_groups_disconnected_views_by_document_lane_and_budget() -> None:
    source = document("d1", ("one", "two", "three")).model_copy(update={"boundaries": ()})
    result = plan_term_extraction(
        (source,),
        (),
        source_hash="source-sha",
        preparation_hash="prep-sha",
        extraction_identity=EXTRACTION_IDENTITY,
        max_primary_chars=11,
    )
    assert [item.view_ids for item in result.plan.items] == [("v-d1-0", "v-d1-1", "v-d1-2")]
    assert result.plan.items[0].context_refs == ()


def test_primary_transport_keeps_narrative_table_note_and_navigation_in_separate_windows() -> None:
    base = document("d1", ("body", "cell1", "cell2", "note", "nav"))
    kinds = ("paragraph", "table_cell", "table_cell", "note", "navigation")
    units = tuple(unit.model_copy(update={"kind": kind}) for unit, kind in zip(base.units, kinds, strict=True))
    source = base.model_copy(update={"units": units, "boundaries": ()})
    result = plan_term_extraction(
        (source,),
        (),
        source_hash="source-sha",
        preparation_hash="prep-sha",
        extraction_identity=EXTRACTION_IDENTITY,
        max_primary_chars=100,
    )
    assert [item.view_ids for item in result.plan.items] == [
        ("v-d1-0",),
        ("v-d1-1", "v-d1-2"),
        ("v-d1-3",),
        ("v-d1-4",),
    ]


def test_cross_document_context_requires_an_explicit_reading_edge() -> None:
    documents = (document("d1", ("one", "two")), document("d2", ("three",)))
    without_edge = plan_term_extraction(
        documents,
        (),
        source_hash="source-sha",
        preparation_hash="prep-sha",
        extraction_identity=EXTRACTION_IDENTITY,
        max_primary_chars=5,
    )
    with_edge = plan_term_extraction(
        documents,
        (),
        source_hash="source-sha",
        preparation_hash="prep-sha",
        extraction_identity=EXTRACTION_IDENTITY,
        max_primary_chars=5,
        reading_edges=(("d1", "d2"),),
    )

    assert [item.context_refs for item in without_edge.plan.items] == [
        ("v-d1-1",),
        ("v-d1-0",),
        (),
    ]
    assert [item.context_refs for item in with_edge.plan.items] == [
        ("v-d1-1",),
        ("v-d1-0", "v-d2-0"),
        ("v-d1-1",),
    ]


def test_invalid_or_ambiguous_reading_edges_are_rejected() -> None:
    documents = (document("d1", ("one",)), document("d2", ("two",)), document("d3", ("three",)))
    for edges in ((("d1", "missing"),), (("d1", "d2"), ("d1", "d3"))):
        try:
            plan_term_extraction(
                documents,
                (),
                source_hash="source-sha",
                preparation_hash="prep-sha",
                extraction_identity=EXTRACTION_IDENTITY,
                reading_edges=edges,
            )
        except ValueError as exc:
            assert "reading edge" in str(exc)
        else:
            raise AssertionError("invalid reading edges were accepted")


def test_only_matching_in_scope_user_terms_are_attached() -> None:
    documents = (document("d1", ("C++ uses RAM in this category.",)), document("d2", ("Agent memory",)))
    terms = (
        term("t-cpp", "C++"),
        term("t-cat", "cat"),
        term("t-ram", "ram", match_policy="casefold"),
        term("t-memory-wrong-doc", "memory", scope=TermScope(kind="documents", document_ids=("d1",))),
        term("t-agent", "agent", aliases=("Agent",), scope=TermScope(kind="units", unit_ids=("u-d2-0",))),
    )

    result = plan_term_extraction(
        documents,
        terms,
        source_hash="source-sha",
        preparation_hash="prep-sha",
        extraction_identity=EXTRACTION_IDENTITY,
    )

    assert result.plan.items[0].user_term_ids == ("t-cpp", "t-ram")
    assert result.plan.items[1].user_term_ids == ("t-agent",)


def test_term_scope_and_occurrence_must_match_same_view() -> None:
    documents = (document("d1", ("Alpha", "memory")),)
    rule = term("t-memory", "memory", scope=TermScope(kind="units", unit_ids=("u-d1-0",)))
    result = plan_term_extraction(
        documents,
        (rule,),
        source_hash="source-sha",
        preparation_hash="prep-sha",
        extraction_identity=EXTRACTION_IDENTITY,
    )
    assert len(result.plan.items) == 1
    assert result.plan.items[0].user_term_ids == ()


def test_context_term_matches_are_separate_read_only_rules() -> None:
    documents = (document("d1", ("RAM", "memory")),)
    terms = (term("t-ram", "RAM"), term("t-memory", "memory"))
    result = plan_term_extraction(
        documents,
        terms,
        source_hash="source-sha",
        preparation_hash="prep-sha",
        extraction_identity=EXTRACTION_IDENTITY,
        max_primary_chars=6,
    )
    assert result.plan.items[0].user_term_ids == ("t-ram",)
    assert result.plan.items[0].context_user_term_ids == ("t-memory",)
    assert result.plan.items[1].user_term_ids == ("t-memory",)
    assert result.plan.items[1].context_user_term_ids == ("t-ram",)


def test_only_narrative_units_and_explicit_structural_relations_become_neighbors() -> None:
    base = document("d1", ("body1", "alt", "cell1", "cell2", "note", "nav", "body2"))
    kinds = ("paragraph", "attribute", "table_cell", "table_cell", "note", "navigation", "paragraph")
    units = tuple(unit.model_copy(update={"kind": kind}) for unit, kind in zip(base.units, kinds, strict=True))
    table_ids = (units[2].unit_id, units[3].unit_id)
    planned = base.model_copy(
        update={
            "units": units,
            "boundaries": (
                {
                    "kind": "table_row",
                    "unit_ids": list(table_ids),
                    "relation_edges": [relation_edge(*table_ids, "table_row")],
                },
            ),
        }
    )
    result = plan_term_extraction(
        (planned,),
        (),
        source_hash="source-sha",
        preparation_hash="prep-sha",
        extraction_identity=EXTRACTION_IDENTITY,
        max_primary_chars=5,
    )
    by_view = {item.view_ids[0]: item for item in result.plan.items}
    assert by_view["v-d1-0"].context_refs == ()
    assert by_view["v-d1-1"].context_refs == ()
    assert by_view["v-d1-2"].context_refs == ("v-d1-3",)
    assert by_view["v-d1-3"].context_refs == ("v-d1-2",)
    assert by_view["v-d1-4"].context_refs == ()
    assert by_view["v-d1-5"].context_refs == ()
    assert by_view["v-d1-6"].context_refs == ()


def test_frozen_table_note_and_narrative_relations_are_used_without_global_pairing() -> None:
    base = document("d1", ("body1", "note", "cell1", "cell2", "body2"))
    kinds = ("paragraph", "note", "table_cell", "table_cell", "paragraph")
    units = tuple(unit.model_copy(update={"kind": kind}) for unit, kind in zip(base.units, kinds, strict=True))
    planned = base.model_copy(
        update={
            "units": units,
            "boundaries": (
                {
                    "kind": "footnote_reference",
                    "unit_ids": [units[0].unit_id, units[1].unit_id],
                    "relation_edges": [relation_edge(units[0].unit_id, units[1].unit_id, "footnote_reference")],
                },
                {
                    "kind": "table_row",
                    "unit_ids": [units[2].unit_id, units[3].unit_id],
                    "relation_edges": [relation_edge(units[2].unit_id, units[3].unit_id, "table_row")],
                },
                {
                    "kind": "narrative_adjacent",
                    "unit_ids": [units[0].unit_id, units[4].unit_id],
                    "relation_edges": [relation_edge(units[0].unit_id, units[4].unit_id, "narrative")],
                },
            ),
        }
    )
    result = plan_term_extraction(
        (planned,),
        (),
        source_hash="source-sha",
        preparation_hash="prep-sha",
        extraction_identity=EXTRACTION_IDENTITY,
        max_primary_chars=5,
    )
    by_view = {item.view_ids[0]: item for item in result.plan.items}
    assert by_view["v-d1-0"].context_refs == ("v-d1-1", "v-d1-4")
    assert by_view["v-d1-1"].context_refs == ("v-d1-0",)
    assert by_view["v-d1-2"].context_refs == ("v-d1-3",)
    assert by_view["v-d1-3"].context_refs == ("v-d1-2",)
    assert by_view["v-d1-4"].context_refs == ("v-d1-0",)


def test_one_referrer_connects_directly_to_every_frozen_note() -> None:
    base = document("d1", ("body", "note1", "note2"))
    units = tuple(
        unit.model_copy(update={"kind": "paragraph" if index == 0 else "note"})
        for index, unit in enumerate(base.units)
    )
    planned = base.model_copy(
        update={
            "units": units,
            "boundaries": (
                {
                    "kind": "footnote_reference",
                    "unit_ids": [unit.unit_id for unit in units],
                    "relation_edges": [
                        relation_edge(units[0].unit_id, units[1].unit_id, "footnote_reference"),
                        relation_edge(units[0].unit_id, units[2].unit_id, "footnote_reference"),
                    ],
                },
            ),
        }
    )
    result = plan_term_extraction(
        (planned,),
        (),
        source_hash="source-sha",
        preparation_hash="prep-sha",
        extraction_identity=EXTRACTION_IDENTITY,
        max_primary_chars=5,
    )
    by_view = {item.view_ids[0]: item for item in result.plan.items}
    assert by_view["v-d1-0"].context_refs == ("v-d1-1", "v-d1-2")
    assert by_view["v-d1-1"].context_refs == ("v-d1-0",)
    assert by_view["v-d1-2"].context_refs == ("v-d1-0",)


def test_relation_without_explicit_edges_is_rejected() -> None:
    base = document("d1", ("left", "right"))
    broken = base.model_copy(
        update={"boundaries": ({"kind": "narrative_adjacent", "unit_ids": [unit.unit_id for unit in base.units]},)}
    )
    try:
        plan_term_extraction(
            (broken,),
            (),
            source_hash="source-sha",
            preparation_hash="prep-sha",
            extraction_identity=EXTRACTION_IDENTITY,
        )
    except TypeError as exc:
        assert "explicit relation_edges" in str(exc)
    else:
        raise AssertionError("ambiguous source relation was accepted")


def test_real_extractor_table_and_footnote_relations_drive_planner_context() -> None:
    markup = (
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>'
        '<p>Body reference<a epub:type="noteref" href="#n1">1</a> and '
        '<a epub:type="noteref" href="#n2">2</a>.</p>'
        '<table><thead><tr><th id="metric" scope="col">Metric</th><th id="value" scope="col">Value</th>'
        '</tr></thead><tbody><tr><td headers="metric">Latency</td><td headers="value">10 ms</td></tr>'
        "</tbody></table>"
        '<section epub:type="endnotes"><aside id="n1">Note one.</aside><aside id="n2">Note two.</aside></section>'
        "<p>After.</p></body></html>"
    )
    source = extract_document(markup, "OPS/chapter.xhtml", "source-sha")
    result = plan_term_extraction(
        (source,),
        (),
        source_hash="source-sha",
        preparation_hash="prep-sha",
        extraction_identity=EXTRACTION_IDENTITY,
        max_primary_chars=10,
    )
    views = source.source_views

    def primary_text(item: ExtractionItem) -> str:
        return "".join(
            views[str(primary_range["view_id"])].text[bound(primary_range, "start") : bound(primary_range, "end")]
            for primary_range in item.primary_ranges
        )

    by_text = {primary_text(item): item for item in result.plan.items}
    assert {views[view_id].text for view_id in by_text["Metric"].context_refs} == {"Value", "Latency"}
    assert {views[view_id].text for view_id in by_text["Value"].context_refs} == {"Metric", "10 ms"}
    assert {views[view_id].text for view_id in by_text["Latency"].context_refs} == {"Metric", "10 ms"}
    assert {views[view_id].text for view_id in by_text["10 ms"].context_refs} == {"Value", "Latency"}
    assert {views[view_id].text for view_id in by_text["Note one."].context_refs} == {"."}
    assert {views[view_id].text for view_id in by_text["Note two."].context_refs} == {"."}
    body_tail_view_id = next(view_id for view_id, view in views.items() if view.text == ".")
    body_item = next(item for item in result.plan.items if body_tail_view_id in item.view_ids)
    assert {"Note one.", "Note two.", "After."}.issubset({views[view_id].text for view_id in body_item.context_refs})
    assert {views[view_id].text for view_id in by_text["After."].context_refs} == {"."}


def test_extraction_identity_changes_input_hash_but_not_stable_item_identity() -> None:
    documents = (document("d1", ("memory",)),)
    first = plan_term_extraction(
        documents,
        (),
        source_hash="source-sha",
        preparation_hash="prep-sha",
        extraction_identity=EXTRACTION_IDENTITY,
    )
    changed = plan_term_extraction(
        documents,
        (),
        source_hash="source-sha",
        preparation_hash="prep-sha",
        extraction_identity=EXTRACTION_IDENTITY | {"prompt_version": "epubox-terms-2"},
    )
    assert first.plan.items[0].item_id == changed.plan.items[0].item_id
    assert first.plan.items[0].extraction_input_hash != changed.plan.items[0].extraction_input_hash


def test_paid_extraction_requires_complete_frozen_identity() -> None:
    try:
        plan_term_extraction(
            (document("d1", ("memory",)),),
            (),
            source_hash="source-sha",
            preparation_hash="prep-sha",
            extraction_identity={"prompt_version": "epubox-terms-1"},
        )
    except ValueError as exc:
        assert "extraction_identity requires" in str(exc)
    else:
        raise AssertionError("incomplete extraction identity was accepted")


def test_zero_primary_views_are_explicitly_not_required() -> None:
    result = plan_term_extraction(
        (),
        (),
        source_hash="source-sha",
        preparation_hash="prep-sha",
        extraction_identity=EXTRACTION_IDENTITY,
    )
    assert result.extraction_status == "not_required"
    assert result.primary_view_count == 0
    assert result.plan.items == ()
    assert result.plan.extraction_http_limit == 0
    assert result.plan.resolution_group_limit == 0


def test_disabled_plan_is_distinct_from_not_required() -> None:
    result = plan_term_extraction(
        (document("d1", ("text",)),),
        (),
        source_hash="source-sha",
        preparation_hash="prep-sha",
        extraction_identity=EXTRACTION_IDENTITY,
        auto_extract=False,
    )
    assert result.extraction_status == "disabled"
    assert result.primary_view_count == 1
    assert result.plan.auto_extract is False
    assert result.plan.items == ()


def test_long_primary_view_splits_on_sentence_or_grapheme_boundaries_without_gaps() -> None:
    text = "First sentence. e\u0301e\u0301e\u0301 tail"
    result = plan_term_extraction(
        (document("d1", (text,)),),
        (),
        source_hash="source-sha",
        preparation_hash="prep-sha",
        extraction_identity=EXTRACTION_IDENTITY,
        max_primary_chars=16,
    )
    ranges = [primary_range for item in result.plan.items for primary_range in item.primary_ranges]
    assert bound(ranges[0], "end") == len("First sentence.")
    assert [bound(primary_range, "start") for primary_range in ranges] == [
        0,
        *[bound(primary_range, "end") for primary_range in ranges[:-1]],
    ]
    assert bound(ranges[-1], "end") == len(text)
    assert "".join(text[bound(r, "start") : bound(r, "end")] for r in ranges) == text
    assert all(len(text[bound(r, "start") : bound(r, "end")]) <= 16 for r in ranges)
    assert all(not text[bound(r, "start") : bound(r, "end")].startswith("\u0301") for r in ranges)
    context_ranges = [context_range for item in result.plan.items for context_range in item.context_ranges]
    assert context_ranges
    assert all(context_range["view_id"] == "v-d1-0" for context_range in context_ranges)
    assert all(bound(context_range, "end") - bound(context_range, "start") <= 400 for context_range in context_ranges)
    for item in result.plan.items:
        primary = item.primary_ranges[0]
        for context in item.context_ranges:
            assert bound(context, "end") <= bound(primary, "start") or bound(context, "start") >= bound(primary, "end")
