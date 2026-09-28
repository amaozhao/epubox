"""Pure planning for deterministic, full-coverage terminology extraction."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from engine.schemas.v25 import (
    DocumentPlan,
    ExtractionItem,
    SourceTextView,
    TermExtractionPlan,
    UserTerm,
    canonical_hash,
    term_plan_hash,
    validate_term_scopes,
)

TERM_PLANNER_VERSION = "epubox-term-planner-1"


@dataclass(frozen=True)
class TermPlanningResult:
    plan: TermExtractionPlan
    extraction_status: Literal["planned", "not_required", "disabled"]
    primary_view_count: int


def plan_term_extraction(
    documents: Sequence[DocumentPlan],
    user_terms: tuple[UserTerm, ...],
    *,
    source_hash: str,
    preparation_hash: str,
    auto_extract: bool = True,
    max_primary_chars: int = 12_000,
    adjacent_context_views: int = 1,
    item_http_limit: int = 6,
    resolution_group_limit: int = 20,
) -> TermPlanningResult:
    """Cover every persisted primary source view exactly once without I/O or model calls."""
    if max_primary_chars <= 0:
        raise ValueError("max_primary_chars must be positive")
    if adjacent_context_views < 0:
        raise ValueError("adjacent_context_views cannot be negative")
    if item_http_limit < 0 or resolution_group_limit < 0:
        raise ValueError("term preparation limits cannot be negative")
    if any(document.source_hash != source_hash for document in documents):
        raise ValueError("all documents must belong to the planned source")
    document_ids = {document.document_id for document in documents}
    if len(document_ids) != len(documents):
        raise ValueError("document IDs must be unique in reading order")
    validate_term_scopes(
        user_terms,
        document_ids,
        {unit.unit_id for document in documents for unit in document.units},
    )

    ordered_views = _ordered_primary_views(documents)
    if not auto_extract:
        plan = _plan(
            source_hash=source_hash,
            preparation_hash=preparation_hash,
            auto_extract=False,
            items=(),
            resolution_group_limit=0,
        )
        return TermPlanningResult(plan, "disabled", len(ordered_views))

    view_positions = {view.view_id: index for index, view in enumerate(ordered_views)}
    items: list[ExtractionItem] = []
    for document in documents:
        primary = [view for view in ordered_views if view.document_id == document.document_id]
        for group in _groups(primary, max_primary_chars):
            primary_ids = tuple(view.view_id for view in group)
            context_ids = _context_ids(
                document,
                group,
                ordered_views,
                view_positions,
                adjacent_context_views,
            )
            selected_terms = tuple(
                sorted(
                    term.term_id
                    for term in user_terms
                    if any(_term_applies(term, view) and _term_occurs(term, view) for view in group)
                )
            )
            terms_by_id = {term.term_id: term for term in user_terms}
            ranges = tuple({"view_id": view.view_id, "start": 0, "end": len(view.text)} for view in group)
            item_id = (
                "te-"
                + canonical_hash(
                    {"version": TERM_PLANNER_VERSION, "document_id": document.document_id, "view_ids": primary_ids}
                )[:24]
            )
            input_payload = {
                "version": TERM_PLANNER_VERSION,
                "item_id": item_id,
                "document_id": document.document_id,
                "primary": [
                    {"view_id": view.view_id, "view_hash": view.view_hash, "start": 0, "end": len(view.text)}
                    for view in group
                ],
                "context": [
                    {"view_id": view_id, "view_hash": _view_by_id(documents, view_id).view_hash}
                    for view_id in context_ids
                ],
                "user_terms": [terms_by_id[term_id].model_dump(mode="json") for term_id in selected_terms],
            }
            items.append(
                ExtractionItem(
                    item_id=item_id,
                    document_id=document.document_id,
                    view_ids=primary_ids,
                    primary_ranges=ranges,
                    context_refs=context_ids,
                    user_term_ids=selected_terms,
                    extraction_input_hash=canonical_hash(input_payload),
                    http_limit=item_http_limit,
                )
            )

    status: Literal["planned", "not_required"] = "planned" if items else "not_required"
    group_limit = resolution_group_limit if items else 0
    plan = _plan(
        source_hash=source_hash,
        preparation_hash=preparation_hash,
        auto_extract=True,
        items=tuple(items),
        resolution_group_limit=group_limit,
    )
    return TermPlanningResult(plan, status, len(ordered_views))


def _plan(
    *,
    source_hash: str,
    preparation_hash: str,
    auto_extract: bool,
    items: tuple[ExtractionItem, ...],
    resolution_group_limit: int,
) -> TermExtractionPlan:
    data = {
        "source_hash": source_hash,
        "preparation_hash": preparation_hash,
        "auto_extract": auto_extract,
        "extraction_http_limit": sum(item.http_limit for item in items) + 3 * resolution_group_limit,
        "resolution_group_limit": resolution_group_limit,
        "items": items,
    }
    return TermExtractionPlan(**data, plan_hash=term_plan_hash(data))


def _ordered_primary_views(documents: Sequence[DocumentPlan]) -> tuple[SourceTextView, ...]:
    views: list[SourceTextView] = []
    seen: set[str] = set()
    for document in documents:
        for unit in document.units:
            for view_id in unit.source_view_ids:
                view = document.source_views[view_id]
                if view.view_kind != "primary":
                    continue
                if view_id in seen:
                    raise ValueError(f"primary view appears more than once: {view_id}")
                seen.add(view_id)
                views.append(view)
    return tuple(views)


def _groups(views: Sequence[SourceTextView], max_chars: int) -> tuple[tuple[SourceTextView, ...], ...]:
    groups: list[tuple[SourceTextView, ...]] = []
    current: list[SourceTextView] = []
    size = 0
    for view in views:
        if len(view.text) > max_chars:
            raise ValueError(f"primary view {view.view_id} exceeds the extraction window budget")
        if current and size + len(view.text) > max_chars:
            groups.append(tuple(current))
            current, size = [], 0
        current.append(view)
        size += len(view.text)
    if current:
        groups.append(tuple(current))
    return tuple(groups)


def _context_ids(
    document: DocumentPlan,
    group: tuple[SourceTextView, ...],
    ordered_views: tuple[SourceTextView, ...],
    positions: dict[str, int],
    adjacent_count: int,
) -> tuple[str, ...]:
    primary_ids = {view.view_id for view in group}
    explicit = {
        view_id
        for view in group
        for view_id in next(unit for unit in document.units if unit.unit_id == view.unit_id).context_view_ids
    }
    start, end = positions[group[0].view_id], positions[group[-1].view_id]
    adjacent = {
        ordered_views[index].view_id
        for index in range(max(0, start - adjacent_count), min(len(ordered_views), end + adjacent_count + 1))
        if index < start or index > end
    }
    return tuple(
        sorted(
            (explicit | adjacent) - primary_ids, key=lambda view_id: (positions.get(view_id, len(positions)), view_id)
        )
    )


def _view_by_id(documents: Sequence[DocumentPlan], view_id: str) -> SourceTextView:
    for document in documents:
        view = document.source_views.get(view_id)
        if view is not None:
            return view
    raise ValueError(f"unknown context view: {view_id}")


def _term_applies(term: UserTerm, view: SourceTextView) -> bool:
    if term.scope.kind == "book":
        return True
    if term.scope.kind == "documents":
        return view.document_id in term.scope.document_ids
    return view.unit_id in term.scope.unit_ids


def _term_occurs(term: UserTerm, view: SourceTextView) -> bool:
    return any(
        _contains(view.text, spelling, term.match_policy == "casefold") for spelling in (term.source, *term.aliases)
    )


def _contains(text: str, spelling: str, casefold: bool) -> bool:
    left = r"(?<!\w)" if spelling[0].isalnum() or spelling[0] == "_" else ""
    right = r"(?!\w)" if spelling[-1].isalnum() or spelling[-1] == "_" else ""
    return re.search(left + re.escape(spelling) + right, text, re.IGNORECASE if casefold else 0) is not None


__all__ = ["TERM_PLANNER_VERSION", "TermPlanningResult", "plan_term_extraction"]
