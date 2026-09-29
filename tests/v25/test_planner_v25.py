from __future__ import annotations

import regex

from engine.epub.derived_bindings import resolve_derived_navigation
from engine.item.extractor import extract_document
from engine.item.inline import parse_projection
from engine.item.unit_planner import (
    build_context,
    build_context_index,
    initial_derived_navigation,
    plan_unit,
    select_terms,
)
from engine.schemas.contracts import FrozenTerm, GlossarySnapshot, TermScope, canonical_hash


def source(*paragraphs: str) -> str:
    body = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
    return f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Book</title></head><body>{body}</body></html>'


def frozen_term(
    term_id: str,
    source_text: str,
    *,
    aliases: tuple[str, ...] = (),
    scope: TermScope | None = None,
    match_policy: str = "exact",
) -> FrozenTerm:
    return FrozenTerm.model_validate(
        {
            "term_id": term_id,
            "source": source_text,
            "target": f"译-{source_text}",
            "aliases": aliases,
            "scope": (scope or TermScope(kind="book")).model_dump(mode="json"),
            "mode": "preferred",
            "match_policy": match_policy,
            "note": f"rule-{term_id}",
            "origin": "user",
        }
    )


def glossary(*terms: FrozenTerm, freeze_id: str = "freeze-1") -> GlossarySnapshot:
    return GlossarySnapshot(
        source_hash="source-sha",
        freeze_id=freeze_id,
        extraction_config_hash="extraction-config",
        user_terms_hash=canonical_hash(terms),
        extraction_status="closed",
        warnings=() if terms else ("no terms",),
        terms=tuple(terms),
    )


def paragraph(document, contains: str):
    return next(unit for unit in document.units if unit.kind == "paragraph" and contains in unit.source_projection)


def test_derived_navigation_initializes_as_a_dependency_without_model_items() -> None:
    chapter = extract_document(source("<h1 id='intro'>Introduction</h1>"), "OPS/chapter.xhtml", "source-sha")
    navigation = extract_document(
        source("<nav><a href='chapter.xhtml#intro'>Introduction</a></nav>"),
        "OPS/nav.xhtml",
        "source-sha",
    )
    chapter, navigation = resolve_derived_navigation((chapter, navigation))
    binding = next(value for value in navigation.derived_bindings if value.get("kind") == "derived_navigation")
    unit = next(value for value in navigation.units if value.unit_id == binding["unit_id"])

    assert initial_derived_navigation(unit, navigation, documents=(chapter, navigation)) == {
        "state": "blocked_dependency",
        "source_unit_id": binding["source_unit_id"],
    }


def test_selector_handles_symbol_boundaries_case_possessive_aliases_and_overlap() -> None:
    document = extract_document(
        source("C++ and .NET classify a cat's data race; Cat is mixed case and category is separate."),
        "chapter.xhtml",
        "source-sha",
    )
    unit = paragraph(document, "C++")
    terms = (
        frozen_term("t-cpp", "C++"),
        frozen_term("t-dotnet", ".NET"),
        frozen_term("t-cat", "cat"),
        frozen_term("t-category", "category"),
        frozen_term("t-casefold", "CAT", match_policy="casefold"),
        frozen_term("t-exact-upper", "CAT"),
        frozen_term("t-alias", "feline", aliases=("cat",)),
        frozen_term("t-data", "data"),
        frozen_term("t-data-race", "data race"),
    )
    selected = select_terms(unit, document, glossary(*terms))

    assert selected.selected_term_ids == tuple(
        sorted(term.term_id for term in terms if term.term_id != "t-exact-upper")
    )
    assert set(selected.applicability.values()) == {"target"}
    category_only = select_terms(unit, document, glossary(*terms), source_projection="category")
    assert "t-category" in category_only.selected_term_ids
    assert "t-cat" not in category_only.selected_term_ids
    assert "t-alias" not in category_only.selected_term_ids
    with_unrelated = select_terms(
        unit,
        document,
        glossary(*terms, frozen_term("t-unrelated", "zebra")),
        source_projection="category",
    )
    assert with_unrelated.selected_term_ids == category_only.selected_term_ids
    assert with_unrelated.terms_hash == category_only.terms_hash


def test_context_scope_uses_the_context_view_owner_and_hints_are_read_only() -> None:
    document = extract_document(
        source("human memory", "Use <code>C++</code> with cache."),
        "chapter.xhtml",
        "source-sha",
    )
    memory_unit = paragraph(document, "human memory")
    cache_unit = paragraph(document, "cache")
    assert cache_unit.context_view_ids == ()
    terms = (
        frozen_term("t-cache", "cache"),
        frozen_term("t-code", "C++", scope=TermScope(kind="units", unit_ids=(cache_unit.unit_id,))),
        frozen_term("t-memory", "memory", scope=TermScope(kind="units", unit_ids=(memory_unit.unit_id,))),
        frozen_term("t-wrong", "memory", scope=TermScope(kind="units", unit_ids=(cache_unit.unit_id,))),
    )
    selection = select_terms(cache_unit, document, glossary(*terms))

    assert build_context(cache_unit, document) == selection.context
    assert selection.applicability == {
        "t-cache": "target",
        "t-code": "context",
        "t-memory": "context",
    }
    assert "t-wrong" not in selection.selected_term_ids
    assert selection.context["views"][0]["unit_id"] == memory_unit.unit_id
    context_memory = select_terms(cache_unit, document, glossary(terms[2]))
    target_memory = select_terms(memory_unit, document, glossary(terms[2]))
    assert context_memory.selected_term_ids == target_memory.selected_term_ids == ("t-memory",)
    assert context_memory.terms_hash != target_memory.terms_hash
    without_relations = document.model_copy(update={"boundaries": ()})
    without_context = select_terms(cache_unit, without_relations, glossary(*terms))
    assert without_context.context["views"] == []
    assert without_context.context_hash != selection.context_hash
    config = {"target_language": "zh-Hans", "context_tokens": 8192, "max_output_tokens": 2048}
    planned = plan_unit(cache_unit, document, glossary(*terms), config)
    planned_without = plan_unit(cache_unit, without_relations, glossary(*terms), config)
    assert planned.cut_plan.segments[0].context_hash == selection.context_hash
    assert planned.logical_hash != planned_without.logical_hash

    split_document = extract_document(
        source("foo<code>x</code>bar"),
        "split.xhtml",
        "source-sha",
    )
    split_unit = paragraph(split_document, "foo")
    assert select_terms(split_unit, split_document, glossary(frozen_term("t-cross", "foobar"))).terms == ()


def test_frozen_narrative_table_header_and_footnote_edges_supply_bounded_context() -> None:
    markup = (
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>'
        '<p>Body reference<a epub:type="noteref" href="#n1">1</a>.</p>'
        '<table><thead><tr><th id="metric" scope="col">Metric</th><th id="value" scope="col">Value</th>'
        '</tr></thead><tbody><tr><td headers="metric">Latency</td><td headers="value">10 ms</td></tr>'
        "</tbody></table>"
        '<section epub:type="endnotes"><aside id="n1">Note one.</aside></section>'
        "<p>After body.</p></body></html>"
    )
    document = extract_document(markup, "relations.xhtml", "source-sha")
    empty = glossary()

    def context_texts(text: str) -> set[str]:
        unit = next(unit for unit in document.units if text in unit.source_projection)
        selection = select_terms(unit, document, empty)
        assert all(len(view["text"]) <= 400 and view["role"] == "context" for view in selection.context["views"])
        return {view["text"] for view in selection.context["views"]}

    assert context_texts("10 ms") == {"Value", "Latency"}
    assert "Note one." in context_texts("Body reference")
    assert any("Body reference" in text for text in context_texts("Note one."))
    assert "." in context_texts("After body")

    data_unit = next(unit for unit in document.units if "10 ms" in unit.source_projection)
    header_unit = next(unit for unit in document.units if unit.source_projection == "Value")
    table_terms = glossary(
        frozen_term("t-header", "Value", scope=TermScope(kind="units", unit_ids=(header_unit.unit_id,))),
        frozen_term("t-wrong-header", "Value", scope=TermScope(kind="units", unit_ids=(data_unit.unit_id,))),
    )
    assert select_terms(data_unit, document, table_terms).applicability == {"t-header": "context"}

    body_unit = next(unit for unit in document.units if "Body reference" in unit.source_projection)
    note_unit = next(unit for unit in document.units if "Note one." in unit.source_projection)
    note_term = frozen_term("t-note", "Note one", scope=TermScope(kind="units", unit_ids=(note_unit.unit_id,)))
    assert select_terms(body_unit, document, glossary(note_term)).applicability == {"t-note": "context"}


def test_explicit_reading_edge_adds_bounded_cross_document_context_and_identity() -> None:
    left = extract_document(
        source(("prefix " * 80) + "memoryTail"),
        "left.xhtml",
        "source-sha",
    )
    right = extract_document(source("Next chapter."), "right.xhtml", "source-sha")
    left_unit = paragraph(left, "memoryTail")
    right_unit = paragraph(right, "Next chapter")
    inventory = {left.document_id: left, right.document_id: right}
    edges = ((left.document_id, right.document_id),)
    index = build_context_index(inventory, edges, 40)
    scoped = frozen_term(
        "t-memory-tail",
        "memoryTail",
        scope=TermScope(kind="units", unit_ids=(left_unit.unit_id,)),
    )

    selection = select_terms(
        right_unit,
        right,
        glossary(scoped),
        documents=inventory,
        reading_edges=edges,
        context_chars=40,
        context_index=index,
    )
    assert selection.applicability == {"t-memory-tail": "context"}
    assert selection.context["views"][0]["document_id"] == left.document_id
    assert selection.context["views"][0]["direction"] == "previous"
    assert len(selection.context["views"][0]["text"]) <= 40
    assert selection.context["views"][0]["text"].endswith("memoryTail")

    config = {"target_language": "zh-Hans", "context_tokens": 8192, "max_output_tokens": 2048}
    planned = plan_unit(
        right_unit,
        right,
        glossary(scoped),
        config,
        documents=inventory,
        reading_edges=edges,
        context_chars=40,
        context_index=index,
    )
    without_edge = plan_unit(
        right_unit,
        right,
        glossary(scoped),
        config,
        documents=inventory,
        context_chars=40,
    )
    assert planned.cut_plan.segments[0].context_hash == selection.context_hash
    assert planned.logical_hash != without_edge.logical_hash


def test_more_than_fifty_matching_terms_are_never_truncated() -> None:
    words = tuple(f"term{index}" for index in range(60))
    document = extract_document(source(" ".join(words)), "chapter.xhtml", "source-sha")
    unit = paragraph(document, "term0")
    snapshot = glossary(*(frozen_term(f"t-{index:02d}", word) for index, word in enumerate(words)))
    selection = select_terms(unit, document, snapshot)
    planned = plan_unit(
        unit,
        document,
        snapshot,
        {"target_language": "zh-Hans", "context_tokens": 32_768, "max_output_tokens": 4096},
    )

    assert len(selection.selected_term_ids) == 60
    assert planned.cut_plan.segments[0].selected_term_ids == selection.selected_term_ids
    assert len(planned.items[planned.cut_plan.segments[0].item_id].selected_term_ids) == 60


def test_initial_cut_plan_reuses_event_safe_splitting_and_hashes_frozen_inputs() -> None:
    long_text = "Long emphasized sentence. " * 120
    markup = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Book</title></head><body>'
        f"<p><em>{long_text}</em> tail.</p></body></html>"
    )
    document = extract_document(markup, "chapter.xhtml", "source-sha")
    unit = paragraph(document, "Long emphasized")
    snapshot = glossary(frozen_term("t-long", "Long"))
    config = {
        "target_language": "zh-Hans",
        "model": "model-a",
        "context_tokens": 4096,
        "max_output_tokens": 256,
        "review_output_tokens": 160,
    }
    first = plan_unit(unit, document, snapshot, config)
    second = plan_unit(unit, document, snapshot, config)
    upgraded = plan_unit(unit, document, snapshot, config, epoch=1)
    stricter = plan_unit(unit, document, snapshot, config, epoch=1, planning_target_ratio=3.2)

    assert first == second
    assert upgraded.logical_hash == first.logical_hash
    assert upgraded.cut_plan.plan_hash != first.cut_plan.plan_hash
    assert upgraded.input_hash != first.input_hash
    assert stricter.logical_hash == first.logical_hash
    assert len(stricter.cut_plan.segments) > len(first.cut_plan.segments)
    assert len(first.cut_plan.segments) > 1
    event_count = sum(
        1 if event.kind == "marker" else len(regex.findall(r"\X", event.value))
        for event in parse_projection(unit.source_projection)
    )
    assert first.cut_plan.segments[-1].source_end == event_count
    assert any(segment.virtual_boundaries for segment in first.cut_plan.segments)
    assert all(first.items[segment.item_id].terms_hash == segment.terms_hash for segment in first.cut_plan.segments)
    assert all(
        first.items[segment.item_id].context_hash == segment.context_hash for segment in first.cut_plan.segments
    )
    assert plan_unit(unit, document, snapshot, config | {"model": "model-b"}).logical_hash != first.logical_hash
    assert (
        plan_unit(unit, document, snapshot.model_copy(update={"freeze_id": "freeze-2"}), config).logical_hash
        != first.logical_hash
    )
