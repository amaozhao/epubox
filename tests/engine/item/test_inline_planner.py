from typing import Literal

import pytest

from engine.core.styles import ReorderPolicy, scan_css, scan_inline_style, scan_stylesheets, selector_policy
from engine.item.inline import (
    Event,
    ProjectionError,
    escape_text,
    events_to_projection,
    parse_projection,
    plain_text,
    projection_identities,
    validate_projection,
)
from engine.item.planner import (
    PlannerConfig,
    PlanningError,
    batch_request,
    estimate_request_tokens,
    initial_coherence_windows,
    input_hash,
    merge_segments,
    plan_unit,
    recommended_output_tokens,
    validate_cut_plan,
)
from engine.item.structural_extractor import extract_document
from engine.schemas.source_internal import (
    DocumentPlan,
    ItemRecord,
    JsonValue,
    NodeRecord,
    RegistryEntry,
    ResourceRecord,
    SlotRange,
    SourceSlot,
    Unit,
    UnitRecord,
    canonical_hash,
)


def make_unit(
    projection: str,
    *,
    registry: dict[str, RegistryEntry] | None = None,
    unit_id: str = "unit-1",
    kind: str = "paragraph",
    context: dict[str, str] | None = None,
    node_key: str = "n1",
    region: dict[str, JsonValue] | None = None,
) -> Unit:
    return Unit(
        unit_id=unit_id,
        document_id="doc-1",
        kind=kind,
        source_projection=projection,
        node_key=node_key,
        slot_ids=(f"s-{unit_id}",),
        registry=registry or {},
        context=context or {},
        region=region or {},
        logical_hash="logical-hash",
    )


def ref(
    ref_id: str,
    kind: Literal["g", "x", "b"],
    *,
    movement: Literal["same_parent", "locked", "fixed"] = "same_parent",
    boundary_type: str | None = None,
):
    return RegistryEntry(
        ref_id=ref_id,
        kind=kind,
        source_node_key=f"node-{ref_id}",
        parent_ref="n1",
        movement=movement,
        reorder_allowed=movement == "same_parent",
        boundary_type=boundary_type,
    )


def make_record(unit: Unit, plan, *, candidate: str | None = None) -> UnitRecord:
    items = {
        segment.item_id: ItemRecord(
            item_id=segment.item_id,
            segment_id=segment.segment_id,
            target_projection=segment.source_projection,
            target_hash=canonical_hash(segment.source_projection),
        )
        for segment in plan.segments
    }
    return UnitRecord(
        unit_id=unit.unit_id,
        document_id=unit.document_id,
        source_hash="source-hash",
        logical_hash=unit.logical_hash,
        input_hash=input_hash(unit, plan),
        plan_epoch=plan.plan_epoch,
        cut_plan=plan,
        items=items,
        candidate=candidate,
        target_hash=canonical_hash(candidate) if candidate is not None else None,
    )


def test_projection_codec_escapes_literals_and_rejects_loose_marker_repairs():
    literal = r"Use \ and ⟦literal⟧ plus <tag>."
    encoded = events_to_projection((Event(kind="text", value=literal), Event(kind="marker", value="=x1")))
    assert encoded == escape_text(literal) + "⟦=x1⟧"
    assert plain_text(encoded) == literal
    assert events_to_projection(parse_projection(encoded)) == encoded

    for malformed in ("⟦+z1⟧", "⟦+g1", "plain⟧", "dangling\\", r"bad\q"):
        with pytest.raises(ProjectionError):
            parse_projection(malformed)


def test_projection_allows_same_parent_reorder_but_preserves_identity_and_nesting():
    registry = {"g1": ref("g1", "g"), "g2": ref("g2", "g")}
    unit = make_unit("A ⟦+g1⟧SSD⟦-g1⟧ and ⟦+g2⟧HDD⟦-g2⟧.", registry=registry)
    target = "⟦+g2⟧机械硬盘⟦-g2⟧比⟦+g1⟧固态硬盘⟦-g1⟧慢。"

    validate_projection(unit, target)
    assert projection_identities(target) == ("g2", "g1")
    assert plain_text(target) == "机械硬盘比固态硬盘慢。"

    with pytest.raises(ProjectionError, match="parent or boundary"):
        validate_projection(unit, "⟦+g1⟧固态⟦+g2⟧机械⟦-g2⟧⟦-g1⟧")


@pytest.mark.parametrize(
    "target, message",
    [
        ("⟦+g1⟧A⟦-g1⟧⟦+g1⟧B⟦-g1⟧⟦=x1⟧", "duplicate"),
        ("⟦+g1⟧A⟦+g2⟧B⟦-g1⟧⟦-g2⟧⟦=x1⟧", "crossed"),
        ("⟦+g1⟧A⟦-g1⟧⟦+g2⟧B⟦-g2⟧⟦=x9⟧", "inventory"),
    ],
)
def test_projection_rejects_duplicate_crossed_and_unknown_references(target: str, message: str):
    registry = {"g1": ref("g1", "g"), "g2": ref("g2", "g"), "x1": ref("x1", "x")}
    unit = make_unit("⟦+g1⟧A⟦-g1⟧⟦+g2⟧B⟦-g2⟧⟦=x1⟧", registry=registry)
    with pytest.raises(ProjectionError, match=message):
        validate_projection(unit, target)


def test_projection_locks_css_sensitive_and_hard_atom_order_and_plain_units():
    locked = {
        "g1": ref("g1", "g", movement="locked"),
        "g2": ref("g2", "g", movement="locked"),
        "x1": ref("x1", "x", movement="fixed", boundary_type="footnote"),
        "x2": ref("x2", "x", movement="fixed", boundary_type="footnote"),
    }
    unit = make_unit("⟦+g1⟧A⟦-g1⟧⟦+g2⟧B⟦-g2⟧⟦=x1⟧⟦=x2⟧", registry=locked)
    with pytest.raises(ProjectionError, match="locked reference order"):
        validate_projection(unit, "⟦+g2⟧乙⟦-g2⟧⟦+g1⟧甲⟦-g1⟧⟦=x1⟧⟦=x2⟧")
    with pytest.raises(ProjectionError, match="locked reference order"):
        validate_projection(unit, "⟦+g1⟧甲⟦-g1⟧⟦+g2⟧乙⟦-g2⟧⟦=x2⟧⟦=x1⟧")

    attribute = make_unit("Original title", kind="attribute")
    with pytest.raises(ProjectionError):
        validate_projection(attribute, "标题⟦=x1⟧", {"x1": ref("x1", "x")})
    with pytest.raises(ProjectionError, match="XML-invalid"):
        validate_projection(attribute, "坏\u0001字")

    boundary_registry = {
        "b1": ref("b1", "b", movement="fixed"),
        "x1": ref("x1", "x", movement="fixed", boundary_type="br"),
        "b2": ref("b2", "b", movement="fixed"),
    }
    boundary = make_unit(
        "⟦+b1⟧Before⟦-b1⟧⟦=x1⟧⟦+b2⟧After⟦-b2⟧",
        registry=boundary_registry,
    )
    with pytest.raises(ProjectionError, match="text moved"):
        validate_projection(boundary, "AFTER⟦+b1⟧⟦-b1⟧⟦=x1⟧⟦+b2⟧⟦-b2⟧BEFORE")


def test_hard_boundary_tracks_ordered_root_domains_but_ignores_movable_g_inside_a_domain():
    boundary_registry = {
        "g1": ref("g1", "g", movement="fixed"),
        "b1": ref("b1", "b", movement="fixed"),
        "x1": ref("x1", "x", movement="fixed", boundary_type="br"),
        "b2": ref("b2", "b", movement="fixed"),
    }
    unit = make_unit(
        "Before ⟦+g1⟧⟦+b1⟧A⟦-b1⟧⟦=x1⟧⟦+b2⟧B⟦-b2⟧⟦-g1⟧ After",
        registry=boundary_registry,
    )
    with pytest.raises(ProjectionError, match="text moved"):
        validate_projection(
            unit,
            "After⟦+g1⟧⟦+b1⟧Before A⟦-b1⟧⟦=x1⟧⟦+b2⟧B⟦-b2⟧⟦-g1⟧",
        )

    movable_registry = {
        "b1": ref("b1", "b", movement="fixed"),
        "g1": ref("g1", "g"),
        "g2": ref("g2", "g"),
        "x1": ref("x1", "x", movement="fixed", boundary_type="br"),
        "b2": ref("b2", "b", movement="fixed"),
    }
    movable = make_unit(
        "⟦+b1⟧⟦+g1⟧A⟦-g1⟧⟦+g2⟧B⟦-g2⟧⟦-b1⟧⟦=x1⟧⟦+b2⟧Tail⟦-b2⟧",
        registry=movable_registry,
    )
    validate_projection(
        movable,
        "⟦+b1⟧⟦+g2⟧乙⟦-g2⟧⟦+g1⟧甲⟦-g1⟧⟦-b1⟧⟦=x1⟧⟦+b2⟧结尾⟦-b2⟧",
    )


def test_css_scan_is_limited_conservative_and_follows_bounded_local_imports():
    assert selector_policy("p > a.link, #note em") == ReorderPolicy.REORDER_ALLOWED
    assert selector_policy("p > a:first-child") == ReorderPolicy.LOCKED
    assert selector_policy("a + em") == ReorderPolicy.LOCKED
    assert selector_policy("svg|a") == ReorderPolicy.UNKNOWN
    assert scan_inline_style("color: red").policy == ReorderPolicy.REORDER_ALLOWED
    assert scan_inline_style('display: ruby; content: "x"').policy == ReorderPolicy.LOCKED
    for display in ("inline-flex", "inline-grid", "inline-table"):
        assert scan_inline_style(f"display: {display}").policy == ReorderPolicy.LOCKED

    scan = scan_stylesheets(
        {
            "css/main.css": '@import "base.css"; p > a { color: red }',
            "css/base.css": "em:first-child { color: blue }",
        },
        roots=("css/main.css",),
    )
    assert scan.policy == ReorderPolicy.LOCKED
    assert scan.visited == ("css/main.css", "css/base.css")
    assert scan.locked_selectors == ("em:first-child",)
    assert (
        scan_stylesheets({"a.css": '@import "b.css"', "b.css": "p {}"}, max_import_depth=0).policy
        == ReorderPolicy.UNKNOWN
    )
    assert scan_css("@unknown thing { p { color: red } }").policy == ReorderPolicy.UNKNOWN
    assert (
        scan_css("p { color: red } /* unrelated data-trace values never enter CSS */").policy
        == ReorderPolicy.REORDER_ALLOWED
    )
    namespace = scan_css('@namespace epub "http://www.idpf.org/2007/ops"; a { color: red }')
    assert namespace.policy == ReorderPolicy.REORDER_ALLOWED
    assert not namespace.issues

    localized = scan_css(".danger > a:first-child { color: red } .rtl { direction: rtl }")
    assert [(item.selector, item.mode) for item in localized.constraints] == [
        (".danger > a:first-child", "group"),
        (".rtl", "descendants"),
    ]
    combined = scan_css("div[dir] { direction: rtl }")
    assert [(item.selector, item.mode) for item in combined.constraints] == [
        ("div[dir]", "group"),
        ("div[dir]", "descendants"),
    ]
    assert selector_policy("article:has(> p)") == ReorderPolicy.UNKNOWN
    assert selector_policy("article:is(:has(> p))") == ReorderPolicy.UNKNOWN


def test_short_unit_has_one_stable_segment_and_batching_does_not_change_identity():
    unit = make_unit("A short complete paragraph.")
    config = PlannerConfig(context_tokens=4096)
    plan = plan_unit(unit, config)

    assert len(plan.segments) == 1
    assert plan.segments[0].source_projection == unit.source_projection
    assert input_hash(unit, plan) == input_hash(unit, plan_unit(unit, config))
    assert batch_request((plan.segments[0],), config) == ((plan.segments[0],),)
    validate_cut_plan(unit, plan)


def test_code_hint_is_bounded_without_sending_the_full_protected_atom():
    code = "very_long_identifier = value\n" * 1000
    unit = make_unit(
        "Use ⟦=x1⟧ safely.",
        registry={
            "x1": RegistryEntry(
                ref_id="x1",
                kind="x",
                source_node_key="code-node",
                parent_ref="n1",
                movement="fixed",
                source_text=code,
                hints={"element": "code", "readonly": code[:160]},
                boundary_type="code",
            )
        },
    )
    assert len(plan_unit(unit, PlannerConfig(context_tokens=4096)).segments) == 1


@pytest.mark.parametrize("tag", ["a", "em"])
def test_real_extractor_long_g_uses_segment_excerpt_and_rebuilds_exactly(tag: str, monkeypatch):
    text = "Long range sentence. " * 550
    attributes = ' href="#target"' if tag == "a" else ""
    markup = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Book</title></head>'
        f"<body><p><{tag}{attributes}>{text}</{tag}> tail.</p></body></html>"
    )
    document = extract_document(markup, "chapter.xhtml", "source-hash")
    unit = next(unit for unit in document.units if "Long range" in unit.source_projection)
    assert max(len(entry.source_text) for entry in unit.registry.values() if entry.kind == "g") > 10_000

    from engine.agents.runtime import request_messages as real_request_messages

    checked_hints = 0

    def request_messages_with_bounded_g(kind, payload):
        nonlocal checked_hints
        for item in payload["items"]:
            for ref_id, hint in item.get("hints", {}).items():
                if unit.registry[ref_id].kind != "g":
                    continue
                checked_hints += 1
                assert "text" not in hint
                assert len(hint["excerpt"]) <= 400
                assert hint["excerpt_truncated"] == "true"
        return real_request_messages(kind, payload)

    monkeypatch.setattr("engine.item.planner.request_messages", request_messages_with_bounded_g)
    config = PlannerConfig(context_tokens=4096, max_output_tokens=256, review_output_tokens=160)
    plan = plan_unit(unit, config)
    validate_cut_plan(unit, plan)

    assert checked_hints
    assert len(plan.segments) > 1
    assert (
        merge_segments(unit, plan, {segment.item_id: segment.source_projection for segment in plan.segments})
        == unit.source_projection
    )


def test_long_unit_splits_on_graphemes_tracks_virtual_ranges_and_merges_exactly():
    family = "👨‍👩‍👧‍👦"
    source = "⟦+b1⟧⟦+g1⟧" + (f"Sentence {family} with detail. " * 24) + "⟦-g1⟧⟦-b1⟧"
    unit = make_unit(
        source,
        registry={"b1": ref("b1", "b", movement="fixed"), "g1": ref("g1", "g")},
    )
    config = PlannerConfig(
        context_tokens=1600,
        max_output_tokens=160,
        review_output_tokens=80,
        safety_margin=8,
        translation_overhead=8,
        review_overhead=8,
    )
    plan = plan_unit(unit, config)

    full_item = {"item_id": unit.unit_id, "source_projection": source}
    assert recommended_output_tokens((full_item,), config) > config.max_output_tokens
    assert recommended_output_tokens((full_item,), config, stage="review") > config.max_output_tokens
    assert len(plan.segments) > 2
    assert all(recommended_output_tokens((segment,), config) <= config.max_output_tokens for segment in plan.segments)
    assert all(
        recommended_output_tokens((segment,), config, stage="review") <= config.max_output_tokens
        for segment in plan.segments
    )
    assert any(segment.virtual_boundaries for segment in plan.segments)
    segment_text = [plain_text(segment.source_projection) for segment in plan.segments]
    assert sum(text.count(family) for text in segment_text) == 24
    assert all("\u200d" not in text.replace(family, "") for text in segment_text)
    assert (
        merge_segments(unit, plan, {segment.item_id: segment.source_projection for segment in plan.segments}) == source
    )

    partial = {segment.item_id: segment.source_projection for segment in plan.segments if segment != plan.segments[1]}
    with pytest.raises(PlanningError, match="missing target"):
        merge_segments(unit, plan, partial)


def test_planning_checks_review_budget_and_batch_packing_only_changes_transport():
    source = "word " * 160
    unit = make_unit(source)
    config = PlannerConfig(
        context_tokens=1500,
        max_output_tokens=512,
        review_output_tokens=128,
        safety_margin=16,
        translation_overhead=1,
        review_overhead=1,
    )
    payload = {"item_id": unit.unit_id, "source_projection": source}
    translation = estimate_request_tokens((payload,), config, stage="translation")
    review = estimate_request_tokens((payload,), config, stage="review")

    assert translation[1] == recommended_output_tokens((payload,), config)
    assert translation[1] == config.max_output_tokens
    assert (
        recommended_output_tokens(({"item_id": unit.unit_id, "source_projection": "short"},), config)
        == config.max_output_tokens
    )
    assert sum(translation) + config.safety_margin <= config.context_tokens
    assert sum(review) + config.safety_margin > config.context_tokens
    plan = plan_unit(unit, config)
    assert len(plan.segments) > 1
    expected_input_hash = input_hash(unit, plan)

    batches = batch_request(plan.segments, PlannerConfig(context_tokens=4096, max_batch_items=1))
    assert tuple(segment.item_id for batch in batches for segment in batch) == tuple(
        segment.item_id for segment in plan.segments
    )
    assert input_hash(unit, plan) == expected_input_hash


def test_virtual_boundaries_cannot_move_and_replanning_replaces_the_whole_plan():
    source = "⟦+g1⟧" + ("One clause, another clause; final sentence. " * 30) + "⟦-g1⟧"
    unit = make_unit(source, registry={"g1": ref("g1", "g")})
    config = PlannerConfig(
        context_tokens=1200,
        max_output_tokens=160,
        review_output_tokens=80,
        safety_margin=8,
        translation_overhead=8,
        review_overhead=8,
    )
    first = plan_unit(unit, config, epoch=0)
    upgraded = plan_unit(unit, config, epoch=1)

    assert first.plan_epoch == 0 and upgraded.plan_epoch == 1
    assert first.plan_hash != upgraded.plan_hash
    assert {segment.item_id for segment in first.segments}.isdisjoint(segment.item_id for segment in upgraded.segments)

    middle = next(segment for segment in first.segments if segment.events[0].virtual)
    marker = middle.events[0].value
    moved = middle.source_projection.replace(f"⟦{marker}⟧", "", 1) + f"⟦{marker}⟧"
    targets = {segment.item_id: segment.source_projection for segment in first.segments}
    targets[middle.item_id] = moved
    with pytest.raises((PlanningError, ProjectionError)):
        merge_segments(unit, first, targets)


def test_cut_plan_validation_recomputes_ranges_events_virtuals_and_hashes():
    unit = make_unit("⟦+g1⟧" + ("Sentence one. Sentence two. " * 20) + "⟦-g1⟧", registry={"g1": ref("g1", "g")})
    config = PlannerConfig(
        context_tokens=1200,
        max_output_tokens=160,
        review_output_tokens=80,
        safety_margin=8,
        translation_overhead=8,
        review_overhead=8,
    )
    plan = plan_unit(unit, config)
    validate_cut_plan(unit, plan)
    first = plan.segments[0]

    broken_segments = (
        first.model_copy(update={"source_start": 1}),
        first.model_copy(update={"events": first.events[1:]}),
        first.model_copy(update={"source_projection": first.source_projection + "tampered"}),
        first.model_copy(update={"virtual_boundaries": ()}),
        first.model_copy(update={"segment_hash": "bad"}),
    )
    for broken in broken_segments:
        with pytest.raises(PlanningError):
            validate_cut_plan(unit, plan.model_copy(update={"segments": (broken, *plan.segments[1:])}))
    with pytest.raises(PlanningError, match="plan hash"):
        validate_cut_plan(unit, plan.model_copy(update={"plan_hash": "bad"}))


def test_initial_coherence_windows_freeze_only_valid_source_relationships_and_seams():
    normal_config = PlannerConfig(context_tokens=4096)
    split_config = PlannerConfig(
        context_tokens=1200,
        max_output_tokens=160,
        review_output_tokens=80,
        safety_margin=8,
        translation_overhead=8,
        review_overhead=8,
    )
    units = (
        make_unit("First paragraph.", unit_id="u1", context={"section": "s"}, node_key="n1"),
        make_unit("Second paragraph.", unit_id="u2", context={"section": "s"}, node_key="n2"),
        make_unit(
            "Cell one.",
            unit_id="t1",
            kind="table_cell",
            context={"section": "s", "table": "table-1", "row": "row-1"},
            node_key="n3",
        ),
        make_unit(
            "Cell two.",
            unit_id="t2",
            kind="table_cell",
            context={"section": "s", "table": "table-1", "row": "row-1"},
            node_key="n4",
        ),
        make_unit(
            "Other table.",
            unit_id="t3",
            kind="table_cell",
            context={"section": "s", "table": "table-2", "row": "row-1"},
            node_key="n5",
        ),
        make_unit("Long text. " * 100, unit_id="long", context={"section": "s"}, node_key="n6"),
        make_unit("Image title", unit_id="attr", kind="attribute", context={"section": "s"}, node_key="n7"),
    )
    plans = {
        unit.unit_id: plan_unit(unit, split_config if unit.unit_id == "long" else normal_config) for unit in units
    }
    records = {
        unit.unit_id: make_record(unit, plans[unit.unit_id], candidate=f"target-{unit.unit_id}") for unit in units
    }
    document = DocumentPlan(
        document_id="doc-1",
        source_hash="source-hash",
        resource=ResourceRecord(path="chapter.xhtml", media_type="application/xhtml+xml", source_sha256="hash"),
        adapter_version="1",
        extractor_version="1",
        source_markup="<html/>",
        nodes={
            unit.node_key: NodeRecord(node_key=unit.node_key, element_path=(index,), qname="p")
            for index, unit in enumerate(units)
        },
        source_slots={
            unit.slot_ids[0]: SourceSlot(
                slot_id=unit.slot_ids[0],
                node_key=unit.node_key,
                field="text",
                source_value=unit.source_projection,
                ranges=(
                    SlotRange(
                        start=0,
                        end=len(unit.source_projection),
                        owner_kind="unit",
                        owner_unit_id=unit.unit_id,
                    ),
                ),
                owner_kind="unit",
                owner_unit_id=unit.unit_id,
            )
            for unit in units
        },
        units=units,
    )

    windows = initial_coherence_windows(document, records)
    unit_pairs = [window["unit_ids"] for window in windows]
    assert ["u1", "u2"] in unit_pairs
    assert ["t1", "t2"] in unit_pairs
    assert ["t2", "t3"] not in unit_pairs
    assert all("attr" not in ids for ids in unit_pairs)
    assert sum(ids == ["long"] for ids in unit_pairs) == len(plans["long"].segments) - 1
    assert all(value and len(value) <= 800 for window in windows for value in window["source"])
    assert all(window["target"] for window in windows)
    assert [window["item_id"] for window in windows] == [
        window["item_id"] for window in initial_coherence_windows(document, records)
    ]


def test_coherence_windows_exclude_semantic_toc_and_index_regions_but_keep_their_seams():
    long_toc = "Long table of contents entry. " * 100
    long_index = "Long index entry. " * 100
    document = extract_document(
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
        f'<section epub:type="toc"><div><p>Contents.</p></div><div><p>{long_toc}</p></div></section>'
        f'<section role="doc-index"><div><p>Index.</p></div><div><p>{long_index}</p></div></section>'
        "<div><p>Outside navigation one.</p></div><div><p>Outside navigation two.</p></div>"
        "</body></html>",
        "OPS/navigation.xhtml",
        "source-hash",
        "application/xhtml+xml",
    )
    split_config = PlannerConfig(
        context_tokens=1200,
        max_output_tokens=160,
        review_output_tokens=80,
        safety_margin=8,
        translation_overhead=8,
        review_overhead=8,
    )
    normal_config = PlannerConfig(context_tokens=4096)
    semantic_units = {
        unit.unit_id
        for unit in document.units
        if plain_text(unit.source_projection).startswith(("Contents", "Long table", "Index", "Long index"))
    }
    long_units = {
        unit.unit_id
        for unit in document.units
        if plain_text(unit.source_projection).startswith(("Long table", "Long index"))
    }
    plans = {
        unit.unit_id: plan_unit(unit, split_config if unit.unit_id in long_units else normal_config)
        for unit in document.units
    }
    records = {
        unit.unit_id: make_record(unit, plans[unit.unit_id], candidate=f"target-{unit.unit_id}")
        for unit in document.units
    }

    windows = initial_coherence_windows(document, records)
    narrative = [window for window in windows if window["relation"] == "narrative_adjacent"]

    assert all(semantic_units.isdisjoint(window["unit_ids"]) for window in narrative)
    assert len(narrative) == 1
    assert all(len(plans[unit_id].segments) > 1 for unit_id in long_units)
    assert all(
        any(
            window["unit_ids"] == [unit_id] and window["relation"] == "seam" and window["scope"] == "unit"
            for window in windows
        )
        for unit_id in long_units
    )


def test_frozen_document_relations_cover_table_rows_notes_and_independent_seams():
    long_text = "Long independent field. " * 180
    markup = (
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>'
        f"{long_text}</title></head><body>"
        '<div><p id="p1">Body one<a epub:type="noteref" href="#n1">1</a>.</p></div>'
        '<div><p id="p2">Body two<a role="doc-noteref" href="chapter.xhtml#n%32">2</a>.</p></div>'
        '<div><p>Read the <a href="#topic">topic</a> and '
        '<a href="https://example.invalid/chapter.xhtml#n1">external note</a>.</p></div>'
        '<h2 id="topic">Topic</h2>'
        "<table>"
        + "".join(
            f'<tr><td><p title="cell label">R{row}C1</p></td><td><p title="cell label">R{row}C2</p></td></tr>'
            for row in range(1, 7)
        )
        + "</table>"
        '<section epub:type="endnotes"><ol id="notes"><li id="n1">Note one.'
        "<ul><li>Nested detail.</li></ul></li>"
        '<li id="n2">Note two.</li></ol></section>'
        f'<img src="cover.png" alt="{long_text}"/></body></html>'
    )
    extracted = extract_document(
        markup,
        "OPS/chapter.xhtml",
        "source-hash",
        "application/xhtml+xml",
    )
    document = DocumentPlan.model_validate_json(extracted.model_dump_json())
    original_units = tuple(unit.unit_id for unit in document.units)
    config = PlannerConfig(
        context_tokens=1600,
        max_output_tokens=160,
        review_output_tokens=80,
        safety_margin=8,
        translation_overhead=8,
        review_overhead=8,
    )
    normal_config = PlannerConfig(context_tokens=16_384)
    plans = {
        unit.unit_id: plan_unit(
            unit,
            config if unit.kind in {"attribute", "head_title"} else normal_config,
        )
        for unit in document.units
    }
    records = {
        unit.unit_id: make_record(unit, plans[unit.unit_id], candidate=f"target-{unit.unit_id}")
        for unit in document.units
    }
    windows = initial_coherence_windows(document, records)
    units_by_text = {plain_text(unit.source_projection): unit for unit in document.units}

    table_windows = [window for window in windows if window["relation"] == "table_row"]
    assert len(table_windows) == 6
    assert all(len(window["unit_ids"]) == 2 and window["scope"] == "chapter" for window in table_windows)

    body_one = units_by_text["Body one."]
    body_two = units_by_text["Body two."]
    note_one = units_by_text["Note one."]
    nested_note = units_by_text["Nested detail."]
    note_two = units_by_text["Note two."]
    pairs = {tuple(window["unit_ids"]) for window in windows}
    assert (body_one.unit_id, body_two.unit_id) in pairs
    assert (body_one.unit_id, note_one.unit_id, nested_note.unit_id) in pairs
    assert (body_two.unit_id, note_two.unit_id) in pairs
    assert (body_two.unit_id, note_one.unit_id) not in pairs
    assert (note_one.unit_id, note_two.unit_id) not in pairs
    assert sum(window["relation"] == "footnote_reference" for window in windows) == 2
    assert all(
        window["scope"] == "chapter"
        for window in windows
        if window["relation"] in {"narrative_adjacent", "footnote_reference"}
    )

    all_independent = [unit for unit in document.units if unit.kind in {"attribute", "head_title"}]
    independent = [unit for unit in all_independent if len(unit.source_projection) > 1_000]
    assert all(len(plans[unit.unit_id].segments) > 1 for unit in independent)
    assert all(
        any(
            window["unit_ids"] == [unit.unit_id] and window["relation"] == "seam" and window["scope"] == "unit"
            for window in windows
        )
        for unit in independent
    )
    independent_ids = {unit.unit_id for unit in all_independent}
    assert all(window["scope"] == "unit" for window in windows if independent_ids.intersection(window["unit_ids"]))
    assert tuple(unit.unit_id for unit in document.units) == original_units

    opf = extract_document(
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Title</dc:title>'
        f"<dc:description>{long_text}</dc:description></metadata></package>",
        "content.opf",
        "source-hash",
        "application/oebps-package+xml",
    )
    description = next(unit for unit in opf.units if unit.kind == "metadata_description")
    description_plan = plan_unit(description, config)
    opf_windows = initial_coherence_windows(
        opf,
        {description.unit_id: make_record(description, description_plan, candidate="translated description")},
    )
    assert len(description_plan.segments) > 1
    assert all(window["scope"] == "unit" and window["relation"] == "seam" for window in opf_windows)
