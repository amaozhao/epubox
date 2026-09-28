from __future__ import annotations

from engine.schemas.v25 import (
    DocumentPlan,
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
        max_primary_chars=10,
    )
    second = plan_term_extraction(
        documents,
        (),
        source_hash="source-sha",
        preparation_hash="prep-sha",
        max_primary_chars=10,
    )

    assert first == second
    assert first.extraction_status == "planned"
    assert first.primary_view_count == 4
    covered = [view_id for item in first.plan.items for view_id in item.view_ids]
    assert covered == ["v-d1-0", "v-d1-1", "v-d1-2", "v-d2-0"]
    assert len(covered) == len(set(covered))
    assert first.plan.extraction_http_limit == 6 * len(first.plan.items) + 3 * 20


def test_windows_use_reading_order_neighbors_as_read_only_context() -> None:
    documents = (document("d1", ("one", "two")), document("d2", ("three",)))
    result = plan_term_extraction(
        documents,
        (),
        source_hash="source-sha",
        preparation_hash="prep-sha",
        max_primary_chars=5,
    )

    assert [item.context_refs for item in result.plan.items] == [
        ("v-d1-1",),
        ("v-d1-0", "v-d2-0"),
        ("v-d1-1",),
    ]


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
    )

    assert result.plan.items[0].user_term_ids == ("t-cpp", "t-ram")
    assert result.plan.items[1].user_term_ids == ("t-agent",)


def test_term_scope_and_occurrence_must_match_same_view() -> None:
    documents = (document("d1", ("Alpha", "memory")),)
    rule = term("t-memory", "memory", scope=TermScope(kind="units", unit_ids=("u-d1-0",)))
    result = plan_term_extraction(documents, (rule,), source_hash="source-sha", preparation_hash="prep-sha")
    assert len(result.plan.items) == 1
    assert result.plan.items[0].user_term_ids == ()


def test_zero_primary_views_are_explicitly_not_required() -> None:
    result = plan_term_extraction(
        (),
        (),
        source_hash="source-sha",
        preparation_hash="prep-sha",
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
        auto_extract=False,
    )
    assert result.extraction_status == "disabled"
    assert result.primary_view_count == 1
    assert result.plan.auto_extract is False
    assert result.plan.items == ()


def test_oversized_primary_view_is_rejected_before_any_request() -> None:
    try:
        plan_term_extraction(
            (document("d1", ("too long",)),),
            (),
            source_hash="source-sha",
            preparation_hash="prep-sha",
            max_primary_chars=3,
        )
    except ValueError as exc:
        assert "exceeds the extraction window budget" in str(exc)
    else:
        raise AssertionError("oversized primary view was accepted")
