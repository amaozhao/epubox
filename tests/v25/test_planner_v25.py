from __future__ import annotations

import regex

from engine.item.extractor_v25 import extract_document
from engine.item.inline import parse_projection
from engine.item.planner_v25 import plan_unit_v25, select_terms
from engine.schemas.v25 import FrozenTerm, GlossarySnapshot, TermScope, canonical_hash


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
    context_view_id = memory_unit.source_view_ids[0]
    cache_unit = cache_unit.model_copy(update={"context_view_ids": (context_view_id,)})
    document = document.model_copy(
        update={"units": tuple(cache_unit if unit.unit_id == cache_unit.unit_id else unit for unit in document.units)}
    )
    terms = (
        frozen_term("t-cache", "cache"),
        frozen_term("t-code", "C++", scope=TermScope(kind="units", unit_ids=(cache_unit.unit_id,))),
        frozen_term("t-memory", "memory", scope=TermScope(kind="units", unit_ids=(memory_unit.unit_id,))),
        frozen_term("t-wrong", "memory", scope=TermScope(kind="units", unit_ids=(cache_unit.unit_id,))),
    )
    selection = select_terms(cache_unit, document, glossary(*terms))

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

    split_document = extract_document(
        source("foo<code>x</code>bar"),
        "split.xhtml",
        "source-sha",
    )
    split_unit = paragraph(split_document, "foo")
    assert select_terms(split_unit, split_document, glossary(frozen_term("t-cross", "foobar"))).terms == ()


def test_more_than_fifty_matching_terms_are_never_truncated() -> None:
    words = tuple(f"term{index}" for index in range(60))
    document = extract_document(source(" ".join(words)), "chapter.xhtml", "source-sha")
    unit = paragraph(document, "term0")
    snapshot = glossary(*(frozen_term(f"t-{index:02d}", word) for index, word in enumerate(words)))
    selection = select_terms(unit, document, snapshot)
    planned = plan_unit_v25(
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
    first = plan_unit_v25(unit, document, snapshot, config)
    second = plan_unit_v25(unit, document, snapshot, config)

    assert first == second
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
    assert plan_unit_v25(unit, document, snapshot, config | {"model": "model-b"}).logical_hash != first.logical_hash
    assert (
        plan_unit_v25(unit, document, snapshot.model_copy(update={"freeze_id": "freeze-2"}), config).logical_hash
        != first.logical_hash
    )
