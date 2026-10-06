"""Pure frozen-glossary selection and Unit cut planning."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

import regex

from engine.core.markup import find_by_element_path, parse_xml_safely, qname_local_name
from engine.item.inline import parse_projection
from engine.item.planner import MAX_SOURCE_TOKENS, PlannerConfig, validate_cut_plan
from engine.item.planner import plan_unit as plan_projection_segments
from engine.schemas import internal
from engine.schemas.contracts import (
    CutPlan,
    DocumentPlan,
    FrozenTerm,
    GlossarySnapshot,
    ItemRecord,
    JsonValue,
    Segment,
    Unit,
    canonical_hash,
    compute_input_hash,
    cut_plan_hash,
    glossary_rules_hash,
    segment_hash,
    validate_cut_plan_coverage,
)

PLANNER_VERSION = "epubox-unit-planner-5"
type TermRole = Literal["target", "context"]


@dataclass(frozen=True)
class TermSelection:
    terms: tuple[FrozenTerm, ...]
    applicability: dict[str, TermRole]
    terms_hash: str
    context_hash: str
    context: dict[str, Any]

    @property
    def selected_term_ids(self) -> tuple[str, ...]:
        return tuple(term.term_id for term in self.terms)


@dataclass(frozen=True)
class UnitPlanInitialization:
    logical_hash: str
    input_hash: str
    cut_plan: CutPlan
    items: dict[str, ItemRecord]


@dataclass(frozen=True)
class _ContextRange:
    view_id: str
    unit_id: str
    document_id: str
    view_hash: str
    text: str
    start: int
    end: int
    relation_kind: str
    direction: Literal["previous", "next"]


@dataclass(frozen=True)
class ContextIndex:
    source_hash: str
    document_hashes: dict[str, str]
    document_identities: dict[str, tuple[str, str, str, str, int, int, int]]
    reading_edges: tuple[tuple[str, str], ...]
    context_chars: int
    ranges_by_unit: dict[str, tuple[_ContextRange, ...]]


def initial_derived_navigation(
    unit: Unit,
    document: DocumentPlan,
    *,
    documents: Mapping[str, DocumentPlan] | Sequence[DocumentPlan] | None = None,
) -> dict[str, JsonValue] | None:
    """Return the exact initial dependency state for one frozen derived navigation Unit."""
    bindings = [
        binding
        for binding in document.derived_bindings
        if binding.get("kind") == "derived_navigation" and binding.get("unit_id") == unit.unit_id
    ]
    if not bindings:
        return None
    inventory = _document_inventory(document, documents)
    if len(bindings) != 1:
        raise ValueError(f"derived navigation Unit has ambiguous bindings: {unit.unit_id}")
    source_unit_id = bindings[0].get("source_unit_id")
    source_units = {candidate.unit_id for source in inventory.values() for candidate in source.units}
    if not isinstance(source_unit_id, str) or source_unit_id == unit.unit_id or source_unit_id not in source_units:
        raise ValueError(f"derived navigation Unit has an invalid source binding: {unit.unit_id}")
    return {"state": "blocked_dependency", "source_unit_id": source_unit_id}


def select_terms(
    unit: Unit,
    document: DocumentPlan,
    glossary: GlossarySnapshot,
    *,
    source_projection: str | None = None,
    documents: Mapping[str, DocumentPlan] | Sequence[DocumentPlan] | None = None,
    reading_edges: tuple[tuple[str, str], ...] = (),
    context_chars: int = 400,
    context_index: ContextIndex | None = None,
) -> TermSelection:
    """Select every matching frozen rule without truncation or cross-scope promotion."""
    _validate_inputs(unit, document, glossary)
    projection = unit.source_projection if source_projection is None else source_projection
    target_text = "".join(
        event.value
        if event.kind == "text"
        else "\n"
        if event.value.startswith("-")
        and (entry := unit.registry.get(event.value[1:])) is not None
        and entry.hints.get("source_view_boundary") == "paragraph"
        else ""
        for event in parse_projection(projection)
    )
    context = build_context(
        unit,
        document,
        documents=documents,
        reading_edges=reading_edges,
        context_chars=context_chars,
        context_index=context_index,
    )
    index = context_index or build_context_index(documents or (document,), reading_edges, context_chars)
    _validate_context_index(index, document)
    context_ranges = index.ranges_by_unit.get(unit.unit_id, ())
    target_views = [document.source_views[view_id] for view_id in unit.source_view_ids]
    hint_text = "\n".join(
        value
        for hint in context["hints"].values()
        if isinstance(hint, dict)
        for value in hint.values()
        if isinstance(value, str)
    )
    selected: list[FrozenTerm] = []
    roles: dict[str, TermRole] = {}
    for term in sorted(glossary.terms, key=lambda value: value.term_id):
        role: TermRole | None = None
        if (
            _scope_applies(term, unit.unit_id, unit.document_id)
            and _term_occurs(term, target_text)
            and any(_term_occurs(term, view.text) for view in target_views)
        ):
            role = "target"
        elif (_scope_applies(term, unit.unit_id, unit.document_id) and _term_occurs(term, hint_text)) or any(
            _scope_applies(term, context_range.unit_id, context_range.document_id)
            and _term_occurs(term, context_range.text)
            for context_range in context_ranges
        ):
            role = "context"
        if role is not None:
            selected.append(term)
            roles[term.term_id] = role

    terms = tuple(selected)
    terms_payload = {"terms": [_term_payload(term, roles[term.term_id]) for term in terms]}
    return TermSelection(
        terms=terms,
        applicability=roles,
        terms_hash=canonical_hash(terms_payload),
        context_hash=canonical_hash(context),
        context=context,
    )


def build_context(
    unit: Unit,
    document: DocumentPlan,
    *,
    documents: Mapping[str, DocumentPlan] | Sequence[DocumentPlan] | None = None,
    reading_edges: tuple[tuple[str, str], ...] = (),
    context_chars: int = 400,
    context_index: ContextIndex | None = None,
) -> dict[str, Any]:
    """Materialize the exact frozen source context used by planning and requests."""
    _validate_unit_document(unit, document)
    index = context_index or build_context_index(documents or (document,), reading_edges, context_chars)
    _validate_context_index(index, document)
    return _context_payload(unit, index.ranges_by_unit.get(unit.unit_id, ()))


def plan_unit(
    unit: Unit,
    document: DocumentPlan,
    glossary: GlossarySnapshot,
    translation_config: Mapping[str, JsonValue],
    *,
    epoch: int = 0,
    documents: Mapping[str, DocumentPlan] | Sequence[DocumentPlan] | None = None,
    reading_edges: tuple[tuple[str, str], ...] = (),
    context_chars: int | None = None,
    context_index: ContextIndex | None = None,
    planning_target_ratio: float | None = None,
) -> UnitPlanInitialization:
    """Build immutable v2.5 Segment identities and the hashes needed by UnitRecord."""
    _validate_inputs(unit, document, glossary)
    planner_config = _planner_config(translation_config)
    if planning_target_ratio is not None:
        if planning_target_ratio <= 0:
            raise ValueError("planning_target_ratio must be positive")
        planner_config = replace(planner_config, target_ratio=planning_target_ratio)
    context_limit = _integer(translation_config, "context_chars", 400) if context_chars is None else context_chars
    unit_selection = select_terms(
        unit,
        document,
        glossary,
        documents=documents,
        reading_edges=reading_edges,
        context_chars=context_limit,
        context_index=context_index,
    )
    temporary_logical_hash = canonical_hash(
        {"version": PLANNER_VERSION, "unit_id": unit.unit_id, "source_projection": unit.source_projection}
    )
    legacy_unit = internal.Unit(
        unit_id=unit.unit_id,
        document_id=unit.document_id,
        kind=unit.kind,
        source_projection=unit.source_projection,
        node_key=unit.node_key,
        slot_ids=unit.slot_ids,
        registry={
            ref_id: internal.RegistryEntry.model_validate(entry.model_dump(mode="python"))
            for ref_id, entry in unit.registry.items()
        },
        context={"source_context": json.dumps(unit_selection.context, ensure_ascii=False, sort_keys=True)},
        terms=tuple(_term_payload(term, unit_selection.applicability[term.term_id]) for term in unit_selection.terms),
        checks=unit.checks,
        region=unit.region,
        logical_hash=temporary_logical_hash,
    )
    legacy_plan = plan_projection_segments(legacy_unit, planner_config, epoch=epoch)
    validate_cut_plan(legacy_unit, legacy_plan)

    segments: list[Segment] = []
    selections: list[TermSelection] = []
    for legacy in legacy_plan.segments:
        selection = select_terms(
            unit,
            document,
            glossary,
            source_projection=legacy.source_projection,
            documents=documents,
            reading_edges=reading_edges,
            context_chars=context_limit,
            context_index=context_index,
        )
        selections.append(selection)
        data = {
            "segment_id": legacy.segment_id,
            "item_id": legacy.item_id,
            "source_start": legacy.source_start,
            "source_end": legacy.source_end,
            "source_projection": legacy.source_projection,
            "selected_term_ids": selection.selected_term_ids,
            "term_applicability": selection.applicability,
            "terms_hash": selection.terms_hash,
            "context_hash": selection.context_hash,
            "virtual_boundaries": legacy.virtual_boundaries,
        }
        segments.append(Segment(**data, segment_hash=segment_hash(data)))

    plan_data = {"plan_epoch": epoch, "segments": tuple(segments)}
    cut_plan = CutPlan(**plan_data, plan_hash=cut_plan_hash(plan_data))
    validate_cut_plan_coverage(cut_plan, _event_count(unit.source_projection))
    if tuple(segment.source_projection for segment in cut_plan.segments) != tuple(
        segment.source_projection for segment in legacy_plan.segments
    ):
        raise ValueError("v2.5 conversion changed a planned source projection")

    rules_hash = glossary_rules_hash(glossary.terms)
    logical_hash = canonical_hash(
        {
            "version": PLANNER_VERSION,
            "source_hash": document.source_hash,
            "unit": unit.model_dump(mode="json"),
            "source_views": [
                document.source_views[view_id].model_dump(mode="json") for view_id in unit.source_view_ids
            ],
            "context_hash": unit_selection.context_hash,
            "freeze_id": glossary.freeze_id,
            "glossary_rules_hash": rules_hash,
            "applicable_terms": [
                _term_payload(term, unit_selection.applicability[term.term_id]) for term in unit_selection.terms
            ],
            "translation_config": dict(translation_config),
        }
    )
    items = {
        segment.item_id: ItemRecord(
            item_id=segment.item_id,
            segment_id=segment.segment_id,
            selected_term_ids=segment.selected_term_ids,
            term_applicability=segment.term_applicability,
            terms_hash=segment.terms_hash,
            context_hash=segment.context_hash,
        )
        for segment in segments
    }
    return UnitPlanInitialization(
        logical_hash=logical_hash,
        input_hash=compute_input_hash(logical_hash, cut_plan.plan_hash),
        cut_plan=cut_plan,
        items=items,
    )


def _validate_inputs(unit: Unit, document: DocumentPlan, glossary: GlossarySnapshot) -> None:
    _validate_unit_document(unit, document)
    if document.source_hash != glossary.source_hash:
        raise ValueError("glossary and document source identities differ")


def _validate_unit_document(unit: Unit, document: DocumentPlan) -> None:
    planned_unit = next((item for item in document.units if item.unit_id == unit.unit_id), None)
    if unit.document_id != document.document_id or planned_unit != unit:
        raise ValueError("Unit does not belong to the DocumentPlan")
    missing = set(unit.source_view_ids) - document.source_views.keys()
    if missing:
        raise ValueError(f"Unit references unknown source views: {sorted(missing)}")


def _context_payload(unit: Unit, ranges: tuple[_ContextRange, ...]) -> dict[str, Any]:
    return {
        "views": [
            {
                "view_id": context_range.view_id,
                "unit_id": context_range.unit_id,
                "document_id": context_range.document_id,
                "text": context_range.text,
                "view_hash": context_range.view_hash,
                "start": context_range.start,
                "end": context_range.end,
                "relation_kind": context_range.relation_kind,
                "direction": context_range.direction,
                "role": "context",
            }
            for context_range in ranges
        ],
        "hints": {
            ref_id: _hint_payload(entry)
            for ref_id, entry in sorted(unit.registry.items())
            if entry.hints or entry.kind == "g"
        },
    }


def build_context_index(
    documents: Mapping[str, DocumentPlan] | Sequence[DocumentPlan],
    reading_edges: tuple[tuple[str, str], ...] = (),
    context_chars: int = 400,
) -> ContextIndex:
    """Precompute immutable source relationships once for all Unit planning."""
    if context_chars < 0:
        raise ValueError("context limit cannot be negative")
    if isinstance(documents, Mapping):
        inventory = dict(documents)
        if any(key != value.document_id for key, value in inventory.items()):
            raise ValueError("document inventory keys must match document_id")
    else:
        inventory = {document.document_id: document for document in documents}
        if len(inventory) != len(documents):
            raise ValueError("document inventory IDs must be unique")
    if not inventory:
        raise ValueError("context index requires at least one DocumentPlan")
    source_hashes = {document.source_hash for document in inventory.values()}
    if len(source_hashes) != 1:
        raise ValueError("document inventory mixes source identities")
    links = _reading_links(reading_edges, set(inventory))
    ranges: dict[str, list[_ContextRange]] = {
        unit.unit_id: [] for document in inventory.values() for unit in document.units
    }
    seen: dict[str, set[tuple[str, int, int, str, str]]] = {unit_id: set() for unit_id in ranges}

    for document in inventory.values():
        units = {unit.unit_id: unit for unit in document.units}
        for boundary in document.boundaries:
            raw_edges = boundary.get("relation_edges")
            if raw_edges is None:
                continue
            if not isinstance(raw_edges, list):
                raise TypeError("source relation_edges must be a list")
            for raw_edge in raw_edges:
                left, right, kind = _edge(raw_edge, units)
                if kind != "table_header":
                    _append_context_range(
                        ranges[left], seen[left], units[right], document, "next", kind, context_chars
                    )
                _append_context_range(
                    ranges[right], seen[right], units[left], document, "previous", kind, context_chars
                )

    narrative = {document_id: _narrative_units(document) for document_id, document in inventory.items()}
    for left_document, right_document in links.items():
        left_units, right_units = narrative[left_document], narrative[right_document]
        if not left_units or not right_units:
            continue
        left, right = left_units[-1], right_units[0]
        _append_context_range(
            ranges[left.unit_id],
            seen[left.unit_id],
            right,
            inventory[right_document],
            "next",
            "reading_order",
            context_chars,
        )
        _append_context_range(
            ranges[right.unit_id],
            seen[right.unit_id],
            left,
            inventory[left_document],
            "previous",
            "reading_order",
            context_chars,
        )
    return ContextIndex(
        source_hash=next(iter(source_hashes)),
        document_hashes={document_id: canonical_hash(document) for document_id, document in inventory.items()},
        document_identities={document_id: _document_identity(document) for document_id, document in inventory.items()},
        reading_edges=reading_edges,
        context_chars=context_chars,
        ranges_by_unit={unit_id: tuple(values) for unit_id, values in ranges.items()},
    )


def _edge(raw_edge: Any, units: Mapping[str, Unit]) -> tuple[str, str, str]:
    allowed = {"narrative", "table_row", "table_header", "footnote_reference"}
    if not isinstance(raw_edge, dict) or set(raw_edge) != {"from_unit_id", "to_unit_id", "kind"}:
        raise TypeError("source relation edges require exactly from_unit_id, to_unit_id, and kind")
    left, right, kind = raw_edge["from_unit_id"], raw_edge["to_unit_id"], raw_edge["kind"]
    if not all(isinstance(value, str) and value for value in (left, right, kind)):
        raise TypeError("source relation edge fields must be non-empty strings")
    if kind not in allowed:
        raise ValueError(f"unsupported source relation kind: {kind}")
    if left not in units or right not in units:
        raise ValueError("source relation edge references an unknown Unit")
    return left, right, kind


def _append_context_range(
    ranges: list[_ContextRange],
    seen: set[tuple[str, int, int, str, str]],
    related: Unit,
    document: DocumentPlan,
    direction: Literal["previous", "next"],
    kind: str,
    limit: int,
) -> None:
    view_ids = list(related.source_view_ids)
    if kind in {"narrative", "reading_order"} and view_ids:
        view_ids = [view_ids[-1] if direction == "previous" else view_ids[0]]
    for view_id in view_ids:
        view = document.source_views[view_id]
        start, end = _context_bounds(view.text, limit, tail=direction == "previous")
        key = view_id, start, end, kind, direction
        if start == end or key in seen:
            continue
        seen.add(key)
        ranges.append(
            _ContextRange(
                view_id=view_id,
                unit_id=view.unit_id,
                document_id=view.document_id,
                view_hash=view.view_hash,
                text=view.text[start:end],
                start=start,
                end=end,
                relation_kind=kind,
                direction=direction,
            )
        )


def _validate_context_index(index: ContextIndex, document: DocumentPlan) -> None:
    if (
        index.source_hash != document.source_hash
        or document.document_id not in index.document_hashes
        or index.document_identities.get(document.document_id) != _document_identity(document)
    ):
        raise ValueError("ContextIndex does not match the verified DocumentPlan inventory")


def _document_identity(document: DocumentPlan) -> tuple[str, str, str, str, int, int, int]:
    return (
        document.source_hash,
        document.resource.source_sha256,
        document.adapter_version,
        document.extractor_version,
        len(document.units),
        len(document.source_views),
        len(document.boundaries),
    )


def _context_bounds(value: str, limit: int, *, tail: bool) -> tuple[int, int]:
    if limit < 0:
        raise ValueError("context limit cannot be negative")
    if not value or limit == 0:
        return 0, 0
    matches = list(regex.finditer(r"\X", value))
    if not tail:
        end = 0
        for match in matches:
            if match.end() > limit:
                break
            end = match.end()
        return 0, end
    start = len(value)
    for match in reversed(matches):
        if len(value) - match.start() > limit:
            break
        start = match.start()
    return start, len(value)


def _document_inventory(
    document: DocumentPlan,
    documents: Mapping[str, DocumentPlan] | Sequence[DocumentPlan] | None,
) -> dict[str, DocumentPlan]:
    if documents is None:
        inventory = {document.document_id: document}
    elif isinstance(documents, Mapping):
        inventory = dict(documents)
        if any(key != value.document_id for key, value in inventory.items()):
            raise ValueError("document inventory keys must match document_id")
    else:
        inventory = {value.document_id: value for value in documents}
        if len(inventory) != len(documents):
            raise ValueError("document inventory IDs must be unique")
    if inventory.get(document.document_id) != document:
        raise ValueError("current DocumentPlan is absent or changed in the verified inventory")
    if any(value.source_hash != document.source_hash for value in inventory.values()):
        raise ValueError("document inventory mixes source identities")
    return inventory


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
        current = start
        seen: set[str] = set()
        while current in successors:
            if current in seen:
                raise ValueError("reading edges cannot contain a cycle")
            seen.add(current)
            current = successors[current]
    return successors


def _narrative_units(document: DocumentPlan) -> tuple[Unit, ...]:
    independent = {
        "attribute",
        "metadata",
        "metadata_title",
        "metadata_description",
        "opf_title",
        "opf_description",
        "head_title",
        "nav",
        "navigation",
    }
    tree = parse_xml_safely(document.source_markup)
    narrative: list[Unit] = []
    for unit in document.units:
        if unit.kind.casefold() in independent or unit.region.get("attribute_name"):
            continue
        path = document.nodes[unit.node_key].element_path
        ancestors = [find_by_element_path(tree, path[:size]) for size in range(len(path) + 1)]
        names = {qname_local_name(element.tag) for element in ancestors}
        tokens = {
            token.casefold()
            for element in ancestors
            for token in f"{element.get('{http://www.idpf.org/2007/ops}type') or element.get('epub:type') or ''} {element.get('role') or ''}".split()
        }
        if names & {"nav", "table", "tr", "td", "th"}:
            continue
        if tokens & {
            "toc",
            "index",
            "doc-toc",
            "doc-index",
            "footnote",
            "endnote",
            "rearnote",
            "doc-footnote",
            "doc-endnote",
            "footnotes",
            "endnotes",
        }:
            continue
        narrative.append(unit)
    return tuple(narrative)


def _hint_payload(entry: Any) -> dict[str, JsonValue]:
    hints: dict[str, JsonValue] = dict(entry.hints)
    if entry.kind == "g":
        excerpt = _excerpt(entry.source_text)
        hints["excerpt"] = excerpt
        hints["excerpt_truncated"] = len(excerpt) < len(entry.source_text)
    return hints


def _term_payload(term: FrozenTerm, role: TermRole) -> dict[str, JsonValue]:
    return {
        "term_id": term.term_id,
        "source": term.source,
        "target": term.target,
        "aliases": list(term.aliases),
        "scope": term.scope.model_dump(mode="json"),
        "mode": term.mode,
        "match_policy": term.match_policy,
        "note": term.note,
        "role": role,
    }


def _scope_applies(term: FrozenTerm, unit_id: str, document_id: str) -> bool:
    if term.scope.kind == "book":
        return True
    if term.scope.kind == "documents":
        return document_id in term.scope.document_ids
    return unit_id in term.scope.unit_ids


def _term_occurs(term: FrozenTerm, text: str) -> bool:
    return any(_contains(text, spelling, term.match_policy == "casefold") for spelling in (term.source, *term.aliases))


def _contains(text: str, spelling: str, casefold: bool) -> bool:
    left = r"(?<!\w)" if spelling[0].isalnum() or spelling[0] == "_" else ""
    right = r"(?!\w)" if spelling[-1].isalnum() or spelling[-1] == "_" else ""
    return re.search(left + re.escape(spelling) + right, text, re.IGNORECASE if casefold else 0) is not None


def _excerpt(value: str, limit: int = 400) -> str:
    selected: list[str] = []
    size = 0
    for match in regex.finditer(r"\X", value):
        cluster = match.group()
        if size + len(cluster) > limit:
            break
        selected.append(cluster)
        size += len(cluster)
    return "".join(selected)


def _event_count(projection: str) -> int:
    return sum(
        1 if event.kind == "marker" else len(regex.findall(r"\X", event.value))
        for event in parse_projection(projection)
    )


def _planner_config(config: Mapping[str, JsonValue]) -> PlannerConfig:
    context = _integer(config, "context_tokens", _integer(config, "max_context_tokens", 8192))
    output = _integer(config, "max_output_tokens", 2048)
    return PlannerConfig(
        context_tokens=context,
        max_source_tokens=_integer(config, "max_source_tokens", MAX_SOURCE_TOKENS),
        max_input_tokens=_optional_integer(config, "max_input_tokens"),
        max_output_tokens=output,
        review_output_tokens=_integer(config, "review_output_tokens", min(768, output)),
        safety_margin=_integer(config, "safety_margin", 256),
        translation_overhead=_integer(config, "translation_overhead", 256),
        review_overhead=_integer(config, "review_overhead", 512),
        target_ratio=_number(config, "target_ratio", 1.6),
        max_batch_items=_integer(config, "max_batch_items", 8),
    )


def _integer(config: Mapping[str, JsonValue], key: str, default: int) -> int:
    value = config.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"translation_config.{key} must be an integer")
    return value


def _optional_integer(config: Mapping[str, JsonValue], key: str) -> int | None:
    value = config.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"translation_config.{key} must be an integer or null")
    return value


def _number(config: Mapping[str, JsonValue], key: str, default: float) -> float:
    value = config.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"translation_config.{key} must be numeric")
    return float(value)


__all__ = [
    "PLANNER_VERSION",
    "ContextIndex",
    "TermSelection",
    "UnitPlanInitialization",
    "build_context",
    "build_context_index",
    "initial_derived_navigation",
    "plan_unit",
    "select_terms",
]
