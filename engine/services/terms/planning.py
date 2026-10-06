"""Pure planning for deterministic, full-coverage terminology extraction."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

import regex

from engine.core.markup import UnsafeMarkupError, find_by_element_path, parse_xml_safely, qname_local_name
from engine.schemas.bridge import AtomicDocument
from engine.schemas.contracts import (
    DocumentPlan,
    ExtractionItem,
    JsonValue,
    SourceTextView,
    TermExtractionPlan,
    Unit,
    UserTerm,
    canonical_hash,
    term_plan_hash,
    validate_term_scopes,
)

TERM_PLANNER_VERSION = "epubox-term-planner-2"
ATOMIC_TERM_PLANNER_VERSION = "epubox-term-planner-3"
type Lane = Literal["narrative", "table", "note", "navigation", "attribute", "metadata", "independent"]


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
    reading_edges: tuple[tuple[str, str], ...] = (),
    context_chars: int = 400,
    extraction_identity: Mapping[str, JsonValue],
    item_http_limit: int = 6,
    resolution_group_limit: int = 20,
) -> TermPlanningResult:
    """Plan a legacy saved DocumentPlan without changing its stable item identities."""
    return _plan_terms(
        documents,
        user_terms,
        source_hash=source_hash,
        preparation_hash=preparation_hash,
        auto_extract=auto_extract,
        max_primary_chars=max_primary_chars,
        adjacent_context_views=adjacent_context_views,
        reading_edges=reading_edges,
        context_chars=context_chars,
        extraction_identity=extraction_identity,
        item_http_limit=item_http_limit,
        resolution_group_limit=resolution_group_limit,
        planner_version=TERM_PLANNER_VERSION,
    )


def plan_atomic_terms(
    inventories: Sequence[AtomicDocument],
    user_terms: tuple[UserTerm, ...],
    *,
    source_hash: str,
    preparation_hash: str,
    auto_extract: bool = True,
    max_primary_chars: int = 12_000,
    adjacent_context_views: int = 2,
    context_chars: int = 400,
    extraction_identity: Mapping[str, JsonValue],
    item_http_limit: int = 6,
    resolution_group_limit: int = 20,
) -> TermPlanningResult:
    """Plan terminology windows from validated whole-atom inventories."""
    if adjacent_context_views > 2:
        raise ValueError("atomic terminology context allows at most two preceding fragments")
    if context_chars > 400:
        raise ValueError("atomic terminology context fragments cannot exceed 400 characters")
    documents = tuple(inventory.document for inventory in inventories)
    lanes = {inventory.document.document_id: _atomic_lanes(inventory) for inventory in inventories}
    return _plan_terms(
        documents,
        user_terms,
        source_hash=source_hash,
        preparation_hash=preparation_hash,
        auto_extract=auto_extract,
        max_primary_chars=max_primary_chars,
        adjacent_context_views=adjacent_context_views,
        reading_edges=(),
        context_chars=context_chars,
        extraction_identity=extraction_identity,
        item_http_limit=item_http_limit,
        resolution_group_limit=resolution_group_limit,
        planner_version=ATOMIC_TERM_PLANNER_VERSION,
        atomic_lanes=lanes,
    )


def _plan_terms(
    documents: Sequence[DocumentPlan],
    user_terms: tuple[UserTerm, ...],
    *,
    source_hash: str,
    preparation_hash: str,
    auto_extract: bool,
    max_primary_chars: int,
    adjacent_context_views: int,
    reading_edges: tuple[tuple[str, str], ...],
    context_chars: int,
    extraction_identity: Mapping[str, JsonValue],
    item_http_limit: int,
    resolution_group_limit: int,
    planner_version: str,
    atomic_lanes: Mapping[str, Mapping[str, Lane]] | None = None,
) -> TermPlanningResult:
    """Cover every persisted primary source view exactly once without I/O or model calls."""
    if max_primary_chars <= 0:
        raise ValueError("max_primary_chars must be positive")
    if adjacent_context_views < 0:
        raise ValueError("adjacent_context_views cannot be negative")
    if context_chars < 0:
        raise ValueError("context_chars cannot be negative")
    if item_http_limit < 0 or resolution_group_limit < 0:
        raise ValueError("term preparation limits cannot be negative")
    if auto_extract:
        required_identity = {"strategy", "prompt_version", "model", "target_language"}
        missing_identity = required_identity - extraction_identity.keys()
        if missing_identity or any(
            not isinstance(extraction_identity[key], str) or not extraction_identity[key]
            for key in required_identity - missing_identity
        ):
            raise ValueError(
                "extraction_identity requires non-empty strategy, prompt_version, model, and target_language"
            )
    if any(document.source_hash != source_hash for document in documents):
        raise ValueError("all documents must belong to the planned source")
    document_ids = {document.document_id for document in documents}
    if len(document_ids) != len(documents):
        raise ValueError("document IDs must be unique in reading order")
    document_successors = _reading_links(reading_edges, document_ids)
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

    views_by_id = {view.view_id: view for document in documents for view in document.source_views.values()}
    units_by_id = {unit.unit_id: unit for document in documents for unit in document.units}
    view_predecessors, view_successors = _view_links(documents, document_successors)
    items: list[ExtractionItem] = []
    for document in documents:
        primary = [view for view in ordered_views if view.document_id == document.document_id]
        unit_lanes = _unit_lanes(document)
        view_lanes: dict[str, Lane] = (
            dict(atomic_lanes[document.document_id])
            if atomic_lanes is not None
            else {
                view_id: unit_lanes[unit.unit_id]
                for unit in document.units
                for view_id in unit.source_view_ids
                if view_id in document.source_views
            }
        )
        for group in _groups(primary, max_primary_chars, view_lanes):
            primary_ids = tuple(dict.fromkeys(part.view.view_id for part in group))
            ranges = tuple({"view_id": part.view.view_id, "start": part.start, "end": part.end} for part in group)
            context_ranges = (
                _preceding_ranges(primary, group, view_lanes, adjacent_context_views, context_chars)
                if atomic_lanes is not None
                else _context_ranges(
                    document,
                    group,
                    views_by_id,
                    units_by_id,
                    view_predecessors,
                    view_successors,
                    adjacent_context_views,
                    reading_edges,
                    context_chars,
                )
            )
            context_ids = tuple(dict.fromkeys(str(context_range["view_id"]) for context_range in context_ranges))
            selected_terms = tuple(
                sorted(
                    term.term_id
                    for term in user_terms
                    if any(
                        _term_applies(term, part.view) and _term_occurs(term, part.view.text[part.start : part.end])
                        for part in group
                    )
                )
            )
            context_terms = tuple(
                sorted(
                    term.term_id
                    for term in user_terms
                    if term.term_id not in selected_terms
                    and any(
                        _term_applies(term, views_by_id[str(context_range["view_id"])])
                        and _term_occurs(
                            term,
                            views_by_id[str(context_range["view_id"])].text[
                                _range_int(context_range, "start") : _range_int(context_range, "end")
                            ],
                        )
                        for context_range in context_ranges
                    )
                )
            )
            terms_by_id = {term.term_id: term for term in user_terms}
            item_id = (
                "te-"
                + canonical_hash(
                    {"version": planner_version, "document_id": document.document_id, "primary_ranges": ranges}
                )[:24]
            )
            input_payload = {
                "version": planner_version,
                "item_id": item_id,
                "document_id": document.document_id,
                "primary": [
                    {
                        "view_id": part.view.view_id,
                        "view_hash": part.view.view_hash,
                        "start": part.start,
                        "end": part.end,
                    }
                    for part in group
                ],
                "context": [
                    {
                        "view_id": context_range["view_id"],
                        "view_hash": views_by_id[str(context_range["view_id"])].view_hash,
                        "start": context_range["start"],
                        "end": context_range["end"],
                    }
                    for context_range in context_ranges
                ],
                "target_user_terms": [terms_by_id[term_id].model_dump(mode="json") for term_id in selected_terms],
                "context_user_terms": [terms_by_id[term_id].model_dump(mode="json") for term_id in context_terms],
                "extraction_identity": dict(extraction_identity),
            }
            items.append(
                ExtractionItem(
                    item_id=item_id,
                    document_id=document.document_id,
                    view_ids=primary_ids,
                    primary_ranges=ranges,
                    context_refs=context_ids,
                    context_ranges=context_ranges,
                    user_term_ids=selected_terms,
                    context_user_term_ids=context_terms,
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


@dataclass(frozen=True)
class _PrimaryRange:
    view: SourceTextView
    start: int
    end: int


def _groups(
    views: Sequence[SourceTextView],
    max_chars: int,
    view_lanes: Mapping[str, Lane],
) -> tuple[tuple[_PrimaryRange, ...], ...]:
    groups: list[tuple[_PrimaryRange, ...]] = []
    current: list[_PrimaryRange] = []
    size = 0
    for view in views:
        for part in _split_view(view, max_chars):
            part_size = part.end - part.start
            if current and (
                size + part_size > max_chars
                or any(existing.view.view_id == view.view_id for existing in current)
                or view_lanes[view.view_id] != view_lanes[current[-1].view.view_id]
            ):
                groups.append(tuple(current))
                current, size = [], 0
            current.append(part)
            size += part_size
    if current:
        groups.append(tuple(current))
    return tuple(groups)


def _split_view(view: SourceTextView, max_chars: int) -> tuple[_PrimaryRange, ...]:
    if not view.text:
        raise ValueError(f"primary view {view.view_id} is empty")
    if len(view.text) <= max_chars:
        return (_PrimaryRange(view, 0, len(view.text)),)
    endpoints = [match.end() for match in regex.finditer(r"\X", view.text)]
    parts: list[_PrimaryRange] = []
    start = 0
    while start < len(view.text):
        fitting = [end for end in endpoints if start < end <= start + max_chars]
        if not fitting:
            raise ValueError(f"one grapheme in primary view {view.view_id} exceeds the extraction window budget")
        farthest = fitting[-1]
        threshold = start + max(1, (farthest - start) * 3 // 5)
        sentence = [end for end in fitting if end >= threshold and view.text[end - 1] in ".!?。！？"]
        whitespace = [end for end in fitting if end >= threshold and view.text[end - 1].isspace()]
        end = sentence[-1] if sentence else whitespace[-1] if whitespace else farthest
        parts.append(_PrimaryRange(view, start, end))
        start = end
    return tuple(parts)


def _atomic_lanes(inventory: AtomicDocument) -> dict[str, Lane]:
    structural = _unit_lanes(inventory.document)
    lanes: dict[str, Lane] = {}
    for item in inventory.items:
        lane: Lane
        if item.channel != "body":
            lane = item.channel
        elif item.atomic_tag == "table" or structural[item.unit_id] == "table":
            lane = "table"
        elif structural[item.unit_id] == "note":
            lane = "note"
        else:
            lane = "narrative"
        for view_id in item.source_view_ids:
            if view_id in inventory.document.source_views:
                lanes[view_id] = lane
    return lanes


def _preceding_ranges(
    views: Sequence[SourceTextView],
    group: tuple[_PrimaryRange, ...],
    view_lanes: Mapping[str, Lane],
    count: int,
    chars: int,
) -> tuple[dict[str, JsonValue], ...]:
    if not count or not chars:
        return ()
    primary_ids = {part.view.view_id for part in group}
    lane = view_lanes[group[0].view.view_id]
    first_index = next(index for index, view in enumerate(views) if view.view_id == group[0].view.view_id)
    ranges: list[dict[str, JsonValue]] = []
    if group[0].start:
        ranges.append(
            {
                "view_id": group[0].view.view_id,
                "start": _tail_start(group[0].view.text, group[0].start, chars),
                "end": group[0].start,
            }
        )
    preceding = [
        view
        for view in reversed(views[:first_index])
        if view.view_id not in primary_ids and view_lanes[view.view_id] == lane
    ][: count - len(ranges)]
    ranges[:0] = [
        {
            "view_id": view.view_id,
            "start": _tail_start(view.text, len(view.text), chars),
            "end": len(view.text),
        }
        for view in reversed(preceding)
        if view.text
    ]
    return tuple(ranges)


def _context_ranges(
    document: DocumentPlan,
    group: tuple[_PrimaryRange, ...],
    views_by_id: dict[str, SourceTextView],
    units_by_id: dict[str, Unit],
    predecessors: dict[str, tuple[str, ...]],
    successors: dict[str, tuple[str, ...]],
    adjacent_count: int,
    reading_edges: tuple[tuple[str, str], ...],
    context_chars: int,
) -> tuple[dict[str, JsonValue], ...]:
    if not adjacent_count or not context_chars:
        return ()
    primary_ids = {part.view.view_id for part in group}
    explicit = tuple(
        dict.fromkeys(view_id for part in group for view_id in units_by_id[part.view.unit_id].context_view_ids)
    )
    allowed_edges = set(reading_edges)
    for view_id in explicit:
        context = views_by_id[view_id]
        if context.document_id != document.document_id and not (
            (context.document_id, document.document_id) in allowed_edges
            or (document.document_id, context.document_id) in allowed_edges
        ):
            raise ValueError("cross-document context requires an explicit reading edge")

    ranges: list[dict[str, JsonValue]] = []
    first, last = group[0], group[-1]
    if first.start:
        start = _tail_start(first.view.text, first.start, context_chars)
        ranges.append({"view_id": first.view.view_id, "start": start, "end": first.start})
    for view_id in _walk_links(first.view.view_id, predecessors, adjacent_count):
        view = views_by_id[view_id]
        ranges.append(
            {"view_id": view_id, "start": _tail_start(view.text, len(view.text), context_chars), "end": len(view.text)}
        )
    ranges.reverse()

    for view_id in explicit:
        if view_id not in primary_ids:
            view = views_by_id[view_id]
            ranges.append({"view_id": view_id, "start": 0, "end": _head_end(view.text, 0, context_chars)})

    if last.end < len(last.view.text):
        end = _head_end(last.view.text, last.end, context_chars)
        ranges.append({"view_id": last.view.view_id, "start": last.end, "end": end})
    for view_id in _walk_links(last.view.view_id, successors, adjacent_count):
        view = views_by_id[view_id]
        ranges.append({"view_id": view_id, "start": 0, "end": _head_end(view.text, 0, context_chars)})
    return tuple(_unique_ranges(ranges))


def _walk_links(start: str, links: dict[str, tuple[str, ...]], depth: int) -> tuple[str, ...]:
    result: list[str] = []
    frontier = [start]
    seen = {start}
    for _ in range(depth):
        frontier = [neighbor for item in frontier for neighbor in links.get(item, ()) if neighbor not in seen]
        if not frontier:
            break
        seen.update(frontier)
        result.extend(frontier)
    return tuple(result)


def _unique_ranges(ranges: Sequence[dict[str, JsonValue]]) -> tuple[dict[str, JsonValue], ...]:
    unique: dict[tuple[str, int, int], dict[str, JsonValue]] = {}
    for item in ranges:
        key = (str(item["view_id"]), _range_int(item, "start"), _range_int(item, "end"))
        if key[1] < key[2]:
            unique[key] = item
    return tuple(unique.values())


def _range_int(item: dict[str, JsonValue], key: str) -> int:
    value = item[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"context range {key} must be an integer")
    return value


def _tail_start(text: str, end: int, limit: int) -> int:
    starts = [match.start() for match in regex.finditer(r"\X", text[:end])]
    return next((start for start in starts if end - start <= limit), end)


def _head_end(text: str, start: int, limit: int) -> int:
    endpoints = [match.end() for match in regex.finditer(r"\X", text[start:]) if match.end() <= limit]
    return start + (endpoints[-1] if endpoints else 0)


def _reading_links(reading_edges: tuple[tuple[str, str], ...], document_ids: set[str]) -> dict[str, str]:
    predecessors: dict[str, str] = {}
    successors: dict[str, str] = {}
    for left, right in reading_edges:
        if left not in document_ids or right not in document_ids or left == right:
            raise ValueError(f"invalid reading edge: {(left, right)}")
        if left in successors or right in predecessors:
            raise ValueError("reading edges must form an unambiguous chain")
        successors[left], predecessors[right] = right, left
    for start in document_ids:
        seen: set[str] = set()
        current = start
        while current in successors:
            if current in seen:
                raise ValueError("reading edges cannot contain a cycle")
            seen.add(current)
            current = successors[current]
    return successors


def _view_links(
    documents: Sequence[DocumentPlan],
    document_successors: dict[str, str],
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    predecessors: dict[str, list[str]] = {}
    successors: dict[str, list[str]] = {}
    narrative_by_document: dict[str, list[Unit]] = {}
    for document in documents:
        lanes = _unit_lanes(document)
        units_by_id = {unit.unit_id: unit for unit in document.units}
        for unit in document.units:
            own_views = [
                document.source_views[view_id] for view_id in unit.source_view_ids if view_id in document.source_views
            ]
            for left, right in pairwise(own_views):
                _link(successors, predecessors, left.view_id, right.view_id)
            lane = lanes[unit.unit_id]
            if lane == "narrative":
                narrative_by_document.setdefault(document.document_id, []).append(unit)

        for boundary in document.boundaries:
            if boundary.get("kind") not in {"narrative_adjacent", "table_row", "footnote_reference"}:
                continue
            unit_ids = boundary.get("unit_ids")
            if not isinstance(unit_ids, (list, tuple)) or not all(isinstance(unit_id, str) for unit_id in unit_ids):
                raise ValueError("source relation unit_ids must be a string array")
            if not unit_ids or any(unit_id not in units_by_id for unit_id in unit_ids):
                raise ValueError("source relation references an unknown Unit")
            relation_edges = boundary.get("relation_edges")
            if not isinstance(relation_edges, (list, tuple)):
                raise TypeError("source relation requires explicit relation_edges")
            allowed = set(units_by_id) if boundary.get("kind") == "table_row" else set(unit_ids)
            allowed_edge_kinds = {
                "narrative_adjacent": {"narrative"},
                "table_row": {"table_row", "table_header"},
                "footnote_reference": {"footnote_reference"},
            }[str(boundary.get("kind"))]
            for edge in relation_edges:
                if not isinstance(edge, dict):
                    raise TypeError("source relation edge must be an object")
                from_unit_id = edge.get("from_unit_id")
                to_unit_id = edge.get("to_unit_id")
                edge_kind = edge.get("kind")
                if (
                    set(edge) != {"from_unit_id", "to_unit_id", "kind"}
                    or not isinstance(from_unit_id, str)
                    or not isinstance(to_unit_id, str)
                    or edge_kind not in allowed_edge_kinds
                    or not {from_unit_id, to_unit_id}.issubset(allowed)
                    or from_unit_id == to_unit_id
                ):
                    raise ValueError("source relation contains an invalid explicit edge")
                row_units = set(unit_ids)
                if edge_kind == "table_row" and not {from_unit_id, to_unit_id}.issubset(row_units):
                    raise ValueError("table row edge must stay within the current row")
                if edge_kind == "table_header" and to_unit_id not in row_units:
                    raise ValueError("table header edge must target a current-row Unit")
                _link_units(
                    document,
                    units_by_id[from_unit_id],
                    units_by_id[to_unit_id],
                    successors,
                    predecessors,
                )

    for left_document, right_document in document_successors.items():
        left, right = narrative_by_document.get(left_document, []), narrative_by_document.get(right_document, [])
        if left and right:
            _link_units(
                next(document for document in documents if document.document_id == left_document),
                left[-1],
                right[0],
                successors,
                predecessors,
                right_document=next(document for document in documents if document.document_id == right_document),
            )
    return (
        {view_id: tuple(neighbors) for view_id, neighbors in predecessors.items()},
        {view_id: tuple(neighbors) for view_id, neighbors in successors.items()},
    )


def _link(
    successors: dict[str, list[str]], predecessors: dict[str, list[str]], left_view_id: str, right_view_id: str
) -> None:
    if right_view_id not in successors.setdefault(left_view_id, []):
        successors[left_view_id].append(right_view_id)
    if left_view_id not in predecessors.setdefault(right_view_id, []):
        predecessors[right_view_id].append(left_view_id)


def _link_units(
    document: DocumentPlan,
    left: Unit,
    right: Unit,
    successors: dict[str, list[str]],
    predecessors: dict[str, list[str]],
    *,
    right_document: DocumentPlan | None = None,
) -> None:
    right_document = right_document or document
    left_views = [view_id for view_id in left.source_view_ids if view_id in document.source_views]
    right_views = [view_id for view_id in right.source_view_ids if view_id in right_document.source_views]
    if left_views and right_views:
        _link(successors, predecessors, left_views[-1], right_views[0])


def _unit_lanes(
    document: DocumentPlan,
) -> dict[str, Lane]:
    independent = {
        "attribute",
        "metadata",
        "metadata_title",
        "metadata_description",
        "opf_title",
        "opf_description",
        "head_title",
    }
    try:
        tree = parse_xml_safely(document.source_markup)
    except UnsafeMarkupError:
        tree = None
    lanes: dict[str, Lane] = {}
    for unit in document.units:
        kind = unit.kind.casefold()
        if kind in {"nav", "navigation"}:
            lanes[unit.unit_id] = "navigation"
            continue
        if kind in independent or unit.region.get("attribute_name"):
            lanes[unit.unit_id] = "independent"
            continue
        if "table" in kind:
            lanes[unit.unit_id] = "table"
            continue
        if "footnote" in kind or kind in {"note", "endnote"}:
            lanes[unit.unit_id] = "note"
            continue
        if tree is None:
            lanes[unit.unit_id] = "narrative"
            continue
        path = document.nodes[unit.node_key].element_path
        try:
            ancestors = [find_by_element_path(tree, path[:size]) for size in range(len(path) + 1)]
        except (IndexError, KeyError):
            lanes[unit.unit_id] = "narrative"
            continue
        names = {qname_local_name(element.tag) for element in ancestors}
        tokens = {
            token.casefold()
            for element in ancestors
            for token in f"{element.get('{http://www.idpf.org/2007/ops}type') or element.get('epub:type') or ''} {element.get('role') or ''}".split()
        }
        if "nav" in names or tokens & {"toc", "index", "doc-toc", "doc-index"}:
            lanes[unit.unit_id] = "navigation"
        elif names & {"table", "tr", "td", "th"}:
            lanes[unit.unit_id] = "table"
        elif tokens & {"footnote", "endnote", "rearnote", "doc-footnote", "doc-endnote", "footnotes", "endnotes"}:
            lanes[unit.unit_id] = "note"
        else:
            lanes[unit.unit_id] = "narrative"
    return lanes


def _term_applies(term: UserTerm, view: SourceTextView) -> bool:
    if term.scope.kind == "book":
        return True
    if term.scope.kind == "documents":
        return view.document_id in term.scope.document_ids
    return view.unit_id in term.scope.unit_ids


def _term_occurs(term: UserTerm, text: str) -> bool:
    return any(_contains(text, spelling, term.match_policy == "casefold") for spelling in (term.source, *term.aliases))


def _contains(text: str, spelling: str, casefold: bool) -> bool:
    left = r"(?<!\w)" if spelling[0].isalnum() or spelling[0] == "_" else ""
    right = r"(?!\w)" if spelling[-1].isalnum() or spelling[-1] == "_" else ""
    return re.search(left + re.escape(spelling) + right, text, re.IGNORECASE if casefold else 0) is not None


__all__ = [
    "ATOMIC_TERM_PLANNER_VERSION",
    "TERM_PLANNER_VERSION",
    "TermPlanningResult",
    "plan_atomic_terms",
    "plan_term_extraction",
]
