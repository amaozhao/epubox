"""Budget-aware projection cutting and batch sizing."""

from __future__ import annotations

import json
import math
import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

import regex
import tiktoken

from engine.agents.runtime import request_messages
from engine.core.markup import UnsafeMarkupError, find_by_element_path, parse_xml_safely, qname_local_name
from engine.item.inline import Event, events_to_projection, parse_projection, validate_projection
from engine.schemas.internal import CutPlan, DocumentPlan, Segment, Unit, UnitRecord, canonical_hash


class PlanningError(ValueError):
    """A Unit cannot be represented within the configured request limits."""


MAX_SOURCE_TOKENS = 1200


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    context_tokens: int
    max_source_tokens: int = MAX_SOURCE_TOKENS
    max_input_tokens: int | None = None
    max_output_tokens: int = 2048
    review_output_tokens: int = 768
    safety_margin: int = 256
    translation_overhead: int = 256
    review_overhead: int = 512
    target_ratio: float = 1.6
    max_batch_items: int = 8

    def __post_init__(self) -> None:
        integer_fields = (
            self.context_tokens,
            self.max_source_tokens,
            self.max_output_tokens,
            self.review_output_tokens,
            self.safety_margin,
            self.translation_overhead,
            self.review_overhead,
            self.max_batch_items,
        )
        if any(value < 1 for value in integer_fields) or (
            self.max_input_tokens is not None and self.max_input_tokens < 1
        ):
            raise ValueError("planner token limits must be positive")
        if self.target_ratio <= 0:
            raise ValueError("target_ratio must be positive")
        if self.max_source_tokens > MAX_SOURCE_TOKENS:
            raise ValueError(f"source fragment limit cannot exceed {MAX_SOURCE_TOKENS} tokens")
        if self.review_output_tokens > self.max_output_tokens:
            raise ValueError("review output reserve cannot exceed the provider output limit")


def plan_unit(unit: Unit, config: PlannerConfig, epoch: int = 0) -> CutPlan:
    """Create a single Segment when possible, otherwise split one immutable Unit."""
    if epoch < 0:
        raise ValueError("plan epoch must be non-negative")
    validate_projection(unit)
    parsed = parse_projection(unit.source_projection)
    atoms = _atomize(parsed)
    if not atoms or not any(event.kind == "text" and event.value for event in atoms):
        raise PlanningError(f"Unit {unit.unit_id} has no translatable text")
    stacks = _range_stacks(atoms)

    full_events = _compact(atoms)
    full_projection = events_to_projection(full_events)
    if _fits_projection(unit, full_projection, config, f"{unit.unit_id}:e{epoch}:s0"):
        segments = (_make_segment(unit, epoch, 0, 0, len(atoms), full_events),)
        return _make_plan(epoch, segments)

    spans: list[tuple[int, int]] = []
    start = 0
    while start < len(atoms):
        fitting: list[tuple[int, int]] = []
        # ponytail: candidate scoring is quadratic only within one oversized Unit;
        # replace with a prefix-token index if profiling finds book-scale outliers.
        for end in range(start + 1, len(atoms) + 1):
            candidate = _segment_events(atoms, stacks, start, end)
            if _fits_projection(
                unit, events_to_projection(candidate), config, f"{unit.unit_id}:e{epoch}:s{len(spans)}"
            ):
                fitting.append((end, _boundary_score(atoms, end)))
            elif fitting:
                break
        if not fitting:
            raise PlanningError(f"Unit {unit.unit_id} contains an atom that cannot fit the request budget")
        end = _choose_cut(start, fitting)
        spans.append((start, end))
        start = end

    segments = tuple(
        _make_segment(unit, epoch, index, start, end, _segment_events(atoms, stacks, start, end))
        for index, (start, end) in enumerate(spans)
    )
    return _make_plan(epoch, segments)


def merge_segments(unit: Unit, plan: CutPlan, targets: Mapping[str, str] | Sequence[str]) -> str:
    """Merge a complete target set, removing only CutPlan-declared virtual markers."""
    target_map = _target_map(plan, targets)
    merged: list[Event] = []
    for segment in plan.segments:
        try:
            target = target_map[segment.item_id]
        except KeyError as exc:
            raise PlanningError(f"missing target for {segment.item_id}") from exc
        target_events = list(validate_projection(segment.source_projection, target, unit.registry))
        prefix, suffix = _virtual_edges(segment.events)
        if [event.value for event in target_events[: len(prefix)]] != prefix:
            raise PlanningError(f"virtual opening boundary moved in {segment.item_id}")
        if suffix and [event.value for event in target_events[-len(suffix) :]] != suffix:
            raise PlanningError(f"virtual closing boundary moved in {segment.item_id}")
        body_end = len(target_events) - len(suffix) if suffix else len(target_events)
        merged.extend(target_events[len(prefix) : body_end])

    projection = events_to_projection(_compact(merged))
    validate_projection(unit, projection)
    return projection


def batch_request(
    items: Sequence[Any], config: PlannerConfig, *, stage: Literal["translation", "review"] = "translation"
) -> tuple[tuple[Any, ...], ...]:
    """Greedily shrink transport batches while preserving the stable item order."""
    batches: list[tuple[Any, ...]] = []
    current: list[Any] = []
    for item in items:
        if not _fits_batch((*current, item), config, stage):
            if not current:
                raise PlanningError(f"item {_item_id(item)} does not fit a {stage} request")
            batches.append(tuple(current))
            current = []
        if not _fits_batch((item,), config, stage):
            raise PlanningError(f"item {_item_id(item)} does not fit a {stage} request")
        current.append(item)
        if len(current) >= config.max_batch_items:
            batches.append(tuple(current))
            current = []
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def input_hash(unit: Unit, plan: CutPlan) -> str:
    """The only v2.3 input hash composition: logical identity plus current plan."""
    return canonical_hash({"logical_hash": unit.logical_hash, "plan_hash": plan.plan_hash})


def validate_cut_plan(unit: Unit, plan: CutPlan) -> None:
    """Recompute every persisted CutPlan invariant before ready/resume use."""
    validate_projection(unit)
    atoms = _atomize(parse_projection(unit.source_projection))
    stacks = _range_stacks(atoms)
    cursor = 0
    for index, segment in enumerate(plan.segments):
        if segment.source_start != cursor or segment.source_end <= segment.source_start:
            raise PlanningError("CutPlan ranges must be ordered, non-empty, and contiguous")
        if segment.source_end > len(atoms):
            raise PlanningError("CutPlan range exceeds the source event stream")
        expected_events = _segment_events(atoms, stacks, segment.source_start, segment.source_end)
        if segment.events != expected_events:
            raise PlanningError(f"segment events do not match source range: {segment.segment_id}")
        if segment.source_projection != events_to_projection(segment.events):
            raise PlanningError(f"segment projection does not match events: {segment.segment_id}")
        if source_token_count(segment.source_projection) > MAX_SOURCE_TOKENS:
            raise PlanningError(f"segment exceeds source token limit: {segment.segment_id}")
        if segment.virtual_boundaries != tuple(event.value for event in segment.events if event.virtual):
            raise PlanningError(f"segment virtual boundaries do not match events: {segment.segment_id}")
        expected = _make_segment(
            unit,
            plan.plan_epoch,
            index,
            segment.source_start,
            segment.source_end,
            expected_events,
        )
        if segment != expected:
            raise PlanningError(f"segment identity or hash is invalid: {segment.segment_id}")
        cursor = segment.source_end
    if cursor != len(atoms):
        raise PlanningError("CutPlan does not cover the complete source event stream")

    expected_plan_hash = canonical_hash(
        {"plan_epoch": plan.plan_epoch, "segments": [segment.model_dump(mode="json") for segment in plan.segments]}
    )
    if plan.plan_hash != expected_plan_hash:
        raise PlanningError("CutPlan plan hash is invalid")
    rebuilt = merge_segments(unit, plan, {segment.item_id: segment.source_projection for segment in plan.segments})
    if rebuilt != unit.source_projection:
        raise PlanningError("CutPlan segments do not rebuild the exact source projection")


def initial_coherence_windows(document: DocumentPlan, records: Mapping[str, UnitRecord]) -> tuple[dict[str, Any], ...]:
    """Build the frozen source relationships that receive the initial check budget."""
    relations = _document_relations(document)
    nav_paths = relations["navigation_paths"]
    derived_navigation = {
        str(binding.get("unit_id", ""))
        for binding in document.derived_bindings
        if binding.get("kind") == "derived_navigation"
    }
    seam_units = [unit for unit in document.units if unit.unit_id not in derived_navigation]
    adjacency_units = [
        unit
        for unit in seam_units
        if not _independent_coherence_unit(unit)
        if not any(document.nodes[unit.node_key].element_path[: len(nav_path)] == nav_path for nav_path in nav_paths)
        if unit.unit_id not in relations["table_units"]
        if unit.unit_id not in relations["note_units"]
    ]
    windows: list[dict[str, Any]] = []
    for left, right in pairwise(adjacency_units):
        left_lane, right_lane = _coherence_lane(left), _coherence_lane(right)
        if left.context.get("section") != right.context.get("section") or left_lane != right_lane:
            continue
        target = _candidate_pair(records.get(left.unit_id), records.get(right.unit_id))
        windows.append(
            {
                "item_id": "w" + canonical_hash([document.document_id, "adjacent", left.unit_id, right.unit_id])[:24],
                "unit_ids": [left.unit_id, right.unit_id],
                "source": [_snippet(left.source_projection, tail=True), _snippet(right.source_projection)],
                "target": target,
                "scope": "chapter",
                "relation": "narrative_adjacent",
            }
        )

    by_id = {unit.unit_id: unit for unit in document.units}
    for row_units in relations["table_rows"]:
        selected = [by_id[unit_id] for unit_id in row_units if unit_id in by_id and unit_id not in derived_navigation]
        if len(selected) < 2:
            continue
        windows.append(_relation_window(document, records, selected, relation="table_row", identity=row_units))

    for body_id, note_units in relations["note_references"]:
        selected_ids = (body_id, *note_units)
        selected = [
            by_id[unit_id] for unit_id in selected_ids if unit_id in by_id and unit_id not in derived_navigation
        ]
        if len(selected) < 2:
            continue
        windows.append(
            _relation_window(document, records, selected, relation="footnote_reference", identity=selected_ids)
        )

    for unit in seam_units:
        record = records.get(unit.unit_id)
        if record is None or record.cut_plan is None:
            continue
        for index, (left, right) in enumerate(zip(record.cut_plan.segments, record.cut_plan.segments[1:])):
            left_item, right_item = record.items.get(left.item_id), record.items.get(right.item_id)
            target = (
                [_snippet(left_item.target_projection, tail=True), _snippet(right_item.target_projection)]
                if left_item is not None
                and right_item is not None
                and left_item.target_projection is not None
                and right_item.target_projection is not None
                else []
            )
            windows.append(
                {
                    "item_id": "w" + canonical_hash([document.document_id, "seam", unit.unit_id, index])[:24],
                    "unit_ids": [unit.unit_id],
                    "source": [_snippet(left.source_projection, tail=True), _snippet(right.source_projection)],
                    "target": target,
                    "scope": "unit"
                    if _independent_coherence_unit(unit) or _inside_nav(document, unit, nav_paths)
                    else "chapter",
                    "relation": "seam",
                }
            )
    return tuple(windows)


def _relation_window(
    document: DocumentPlan,
    records: Mapping[str, UnitRecord],
    units: Sequence[Unit],
    *,
    relation: Literal["table_row", "footnote_reference"],
    identity: Sequence[str],
) -> dict[str, Any]:
    return {
        "item_id": "w" + canonical_hash([document.document_id, relation, *identity])[:24],
        "unit_ids": [unit.unit_id for unit in units],
        "source": [_snippet(unit.source_projection, tail=index == 0) for index, unit in enumerate(units)],
        "target": _candidate_group(records, units),
        "scope": "chapter",
        "relation": relation,
    }


def _document_relations(document: DocumentPlan) -> dict[str, Any]:
    """Recover structural relationships from one immutable source parse."""
    table_groups: dict[tuple[int, ...], list[str]] = {}
    note_groups: dict[tuple[int, ...], list[str]] = {}
    note_fragments: dict[str, tuple[int, ...]] = {}
    unit_note_groups: dict[str, tuple[int, ...]] = {}
    elements: dict[tuple[int, ...], Any] = {}
    try:
        tree = parse_xml_safely(document.source_markup)
        elements = {
            node.element_path: find_by_element_path(tree, node.element_path) for node in document.nodes.values()
        }
    except (KeyError, UnsafeMarkupError):
        tree = None

    singular_notes: set[tuple[int, ...]] = set()
    plural_notes: set[tuple[int, ...]] = set()
    navigation_paths = {
        node.element_path for node in document.nodes.values() if qname_local_name(node.qname).lower() == "nav"
    }
    for path, element in elements.items():
        tokens = _semantic_tokens(element)
        if tokens & {"toc", "index", "doc-toc", "doc-index"}:
            navigation_paths.add(path)
        if tokens & {"footnote", "endnote", "rearnote", "doc-footnote", "doc-endnote"}:
            singular_notes.add(path)
        if tokens & {"footnotes", "endnotes", "rearnotes", "doc-footnotes", "doc-endnotes"}:
            plural_notes.add(path)

    for unit in document.units:
        if _independent_coherence_unit(unit):
            continue
        path = document.nodes[unit.node_key].element_path
        row_path = _nearest_element_path(path, elements, {"tr"})
        table_path = _nearest_element_path(path, elements, {"table"})
        cell_path = _nearest_element_path(path, elements, {"td", "th"})
        if table_path is not None and row_path is not None and cell_path is not None:
            table_groups.setdefault(row_path, []).append(unit.unit_id)

        note_path = _note_container(path, elements, singular_notes, plural_notes)
        if note_path is not None:
            note_groups.setdefault(note_path, []).append(unit.unit_id)
            unit_note_groups[unit.unit_id] = note_path

    if tree is not None:
        for path in note_groups:
            element = find_by_element_path(tree, path)
            for item in element.iter():
                fragment = item.get("id")
                if fragment:
                    note_fragments[fragment] = path

    note_references: list[tuple[str, tuple[str, ...]]] = []
    seen_references: set[tuple[str, tuple[str, ...]]] = set()
    for binding in document.derived_bindings:
        if binding.get("kind") != "href_candidate":
            continue
        body_id = str(binding.get("source_unit_id", ""))
        if not body_id or body_id in unit_note_groups:
            continue
        fragment = _same_document_fragment(document.resource.path, str(binding.get("href", "")))
        note_path = note_fragments.get(fragment) if fragment else None
        note_ids = tuple(note_groups.get(note_path, ())) if note_path is not None else ()
        key = (body_id, note_ids)
        if note_ids and key not in seen_references:
            seen_references.add(key)
            note_references.append(key)

    # Old frozen plans with explicit table identity still get the same relationship.
    if not table_groups:
        legacy: dict[tuple[str, ...], list[str]] = {}
        for unit in document.units:
            lane = _coherence_lane(unit)
            if lane and lane[0] == "table":
                legacy.setdefault(lane, []).append(unit.unit_id)
        table_groups.update({(index,): units for index, units in enumerate(legacy.values())})

    return {
        "navigation_paths": tuple(sorted(navigation_paths)),
        "table_rows": tuple(tuple(units) for units in table_groups.values() if len(units) > 1),
        "table_units": {unit_id for units in table_groups.values() for unit_id in units},
        "note_units": set(unit_note_groups),
        "note_references": tuple(note_references),
    }


def _semantic_tokens(element: Any) -> set[str]:
    epub_type = element.get("{http://www.idpf.org/2007/ops}type") or element.get("epub:type") or ""
    return {token.casefold() for token in f"{epub_type} {element.get('role') or ''}".split()}


def _nearest_element_path(
    path: tuple[int, ...], elements: Mapping[tuple[int, ...], Any], names: set[str]
) -> tuple[int, ...] | None:
    return next(
        (
            prefix
            for size in range(len(path), -1, -1)
            if (prefix := path[:size]) in elements and qname_local_name(elements[prefix].tag) in names
        ),
        None,
    )


def _note_container(
    path: tuple[int, ...],
    elements: Mapping[tuple[int, ...], Any],
    singular: set[tuple[int, ...]],
    plural: set[tuple[int, ...]],
) -> tuple[int, ...] | None:
    direct = next((path[:size] for size in range(len(path), -1, -1) if path[:size] in singular), None)
    if direct is not None:
        return direct
    parent = next((path[:size] for size in range(len(path), -1, -1) if path[:size] in plural), None)
    if parent is None:
        return None
    descendants = [path[:size] for size in range(len(parent) + 1, len(path) + 1)]
    for candidate in descendants:
        element = elements.get(candidate)
        if element is not None and qname_local_name(element.tag) in {"li", "aside"}:
            return candidate
    for candidate in reversed(descendants):
        element = elements.get(candidate)
        if element is not None and element.get("id"):
            return candidate
    return path


def _same_document_fragment(resource_path: str, href: str) -> str:
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc or not parsed.fragment:
        return ""
    target_path = unquote(parsed.path)
    if target_path:
        target_path = posixpath.normpath(posixpath.join(posixpath.dirname(resource_path), target_path)).lstrip("/")
        if target_path != posixpath.normpath(resource_path).lstrip("/"):
            return ""
    return unquote(parsed.fragment)


def _candidate_group(records: Mapping[str, UnitRecord], units: Sequence[Unit]) -> list[str]:
    candidates = [records.get(unit.unit_id) for unit in units]
    if any(record is None or record.candidate is None for record in candidates):
        return []
    return [
        _snippet(record.candidate or "", tail=index == 0)
        for index, record in enumerate(candidates)
        if record is not None
    ]


def _independent_coherence_unit(unit: Unit) -> bool:
    kind = unit.kind.lower()
    return kind in {
        "attribute",
        "metadata",
        "metadata_title",
        "metadata_description",
        "opf_title",
        "opf_description",
        "head_title",
        "nav",
        "navigation",
    } or bool(unit.region.get("attribute_name"))


def _inside_nav(document: DocumentPlan, unit: Unit, nav_paths: Sequence[tuple[int, ...]]) -> bool:
    path = document.nodes[unit.node_key].element_path
    return any(path[: len(nav_path)] == nav_path for nav_path in nav_paths)


def estimate_request_tokens(
    items: Sequence[Any], config: PlannerConfig, *, stage: Literal["translation", "review"] = "translation"
) -> tuple[int, int]:
    """Return conservative input and output reservations for an actual request shape."""
    payload = _request_payload(items, stage)
    runtime_stage = "translate" if stage == "translation" else "review"
    messages = request_messages(runtime_stage, payload)
    source_tokens = _count_tokens(json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if stage == "translation":
        input_tokens = config.translation_overhead + source_tokens
    else:
        missing_target_reserve = 0
        for item, wire_item in zip(items, payload["items"], strict=True):
            if _target(item):
                continue
            missing_target_reserve += math.ceil(_count_tokens(str(wire_item["source"])) * config.target_ratio)
            binding_tokens = sum(
                _count_tokens(str(binding.get("source", "")))
                for binding in wire_item.get("bindings", ())
                if isinstance(binding, Mapping)
            )
            missing_target_reserve += math.ceil(binding_tokens * max(0.0, config.target_ratio - 1))
        input_tokens = config.review_overhead + source_tokens + missing_target_reserve
    return input_tokens, recommended_output_tokens(items, config, stage=stage)


def recommended_output_tokens(
    items: Sequence[Any], config: PlannerConfig, *, stage: Literal["translation", "review"] = "translation"
) -> int:
    """Return the per-request provider output cap used by planning and runtime."""
    request_id = "r00000000000000000000000000000000"
    if stage == "translation":
        response = {
            "protocol": "epubox-text-1",
            "request_id": request_id,
            "items": [{"item_id": _item_id(item), "target": ""} for item in items],
        }
        envelope = _count_tokens(json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        target = math.ceil(sum(_count_tokens(_projection(item)) for item in items) * config.target_ratio)
        return max(config.max_output_tokens, envelope + target)
    response = {
        "protocol": "epubox-review-2",
        "request_id": request_id,
        "items": [
            {
                "item_id": _item_id(item),
                "base_revision": 0,
                "decision": "replace",
                "checks": {
                    "accuracy": "pass",
                    "fluency": "pass",
                    "terminology": "not_applicable",
                    "bindings": "not_applicable",
                    "script": "pass",
                },
                "issues": [],
                "target": "",
            }
            for item in items
        ],
    }
    envelope = _count_tokens(json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    complete_target = sum(
        _count_tokens(_target(item))
        if _target(item)
        else math.ceil(_count_tokens(_projection(item)) * config.target_ratio)
        for item in items
    )
    return max(config.max_output_tokens, config.review_output_tokens, envelope + complete_target)


def _fits_projection(unit: Unit, projection: str, config: PlannerConfig, item_id: str) -> bool:
    if source_token_count(projection) > config.max_source_tokens:
        return False
    item = {
        "item_id": item_id,
        "source_projection": projection,
        "context": unit.context,
        "terms": unit.terms,
        "hints": {ref: _request_hint(entry) for ref, entry in unit.registry.items()},
        "constraints": {
            ref: {
                "parent_ref": entry.parent_ref,
                "movement": entry.movement,
                "fixed_order": list(entry.fixed_order),
            }
            for ref, entry in unit.registry.items()
        },
        "applicability": {"terminology": bool(unit.terms), "bindings": bool(unit.registry)},
        "bindings": _segment_bindings(projection, unit.registry),
    }
    return _fits_batch((item,), config, "translation") and _fits_batch((item,), config, "review")


@lru_cache(maxsize=1)
def _planner_tokenizer() -> Any | None:
    try:
        return tiktoken.encoding_for_model("gpt-3.5-turbo")
    except (KeyError, OSError, RuntimeError, ValueError):
        try:
            return tiktoken.get_encoding("cl100k_base")
        except (KeyError, OSError, RuntimeError, ValueError):
            return None


def _count_tokens(text: str) -> int:
    tokenizer = _planner_tokenizer()
    if tokenizer is not None:
        return len(tokenizer.encode(text))
    return max(1, len(text.encode("utf-8")))


def source_token_count(projection: str) -> int:
    """Count the exact projected source with a conservative offline fallback."""
    return _count_tokens(projection)


def _coherence_lane(unit: Unit) -> tuple[str, ...] | None:
    kind = unit.kind.lower()
    excluded = {
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
    if kind in excluded or unit.region.get("attribute_name"):
        return None
    if kind == "table_cell" or "table" in kind:
        identity = tuple(
            f"{key}={unit.context[key]}"
            for key in ("table", "table_id", "table_group", "group", "row", "row_id")
            if unit.context.get(key)
        )
        return ("table", *identity) if identity else ("table", unit.node_key)
    if "footnote" in kind or kind in {"note", "endnote"}:
        identity = tuple(
            f"{key}={unit.context[key]}"
            for key in ("footnote_group", "note_group", "group", "footnote_id", "note_id")
            if unit.context.get(key)
        )
        return ("footnote", *identity) if identity else ("footnote", unit.node_key)
    return ("narrative", unit.context.get("section", ""))


def _request_hint(entry: Any) -> dict[str, str]:
    hints = dict(entry.hints)
    if entry.kind == "g":
        excerpt = _excerpt(entry.source_text)
        hints.update(
            {
                "excerpt": excerpt,
                "excerpt_truncated": "true" if len(excerpt) < len(entry.source_text) else "false",
            }
        )
    return hints


def _segment_bindings(projection: str, registry: Mapping[str, Any]) -> list[dict[str, str]]:
    ranges: dict[str, list[str]] = {}
    stack: list[str] = []
    footnotes: list[str] = []
    for event in parse_projection(projection):
        if event.kind == "text":
            for ref in stack:
                ranges[ref].append(event.value)
            continue
        edge, ref = event.value[0], event.value[1:]
        if edge == "+" and ref.startswith("g"):
            stack.append(ref)
            ranges.setdefault(ref, [])
        elif edge == "-" and ref.startswith("g"):
            if not stack or stack[-1] != ref:
                raise PlanningError(f"invalid segment binding range: {event.value}")
            stack.pop()
        elif edge == "=" and _field(registry.get(ref), "boundary_type") == "footnote":
            footnotes.append(ref)
    bindings = [{"ref": ref, "source": "".join(text), "target": "".join(text)} for ref, text in ranges.items()]
    bindings.extend(
        {"ref": ref, "source": _excerpt(str(_field(registry.get(ref), "source_text", default=""))), "target": ""}
        for ref in footnotes
    )
    return bindings


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


def _field(value: Any, name: str, *, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _candidate_pair(left: UnitRecord | None, right: UnitRecord | None) -> list[str]:
    if left is None or right is None or left.candidate is None or right.candidate is None:
        return []
    return [_snippet(left.candidate, tail=True), _snippet(right.candidate)]


def _snippet(value: str, *, tail: bool = False, limit: int = 800) -> str:
    return value[-limit:] if tail else value[:limit]


def _fits_batch(items: Sequence[Any], config: PlannerConfig, stage: Literal["translation", "review"]) -> bool:
    input_tokens, output_tokens = estimate_request_tokens(items, config, stage=stage)
    if config.max_input_tokens is not None and input_tokens > config.max_input_tokens:
        return False
    if output_tokens > config.max_output_tokens:
        return False
    return input_tokens + output_tokens + config.safety_margin <= config.context_tokens


def _request_payload(items: Sequence[Any], stage: Literal["translation", "review"]) -> dict[str, Any]:
    protocol = "epubox-text-1" if stage == "translation" else "epubox-review-2"
    payload: dict[str, Any] = {
        "protocol": protocol,
        "request_id": "r00000000000000000000000000000000",
        "items": [_payload(item, stage) for item in items],
    }
    if stage == "translation":
        payload["target_language"] = "zh-Hans"
    return payload


def _payload(item: Any, stage: Literal["translation", "review"]) -> dict[str, Any]:
    if isinstance(item, Mapping):
        data: dict[str, Any] = {
            key: value
            for key, value in item.items()
            if key not in {"source_projection", "target_projection", "unit_id"}
        }
        data.setdefault("item_id", _item_id(item))
        data.setdefault("source", _projection(item))
    else:
        data = {"item_id": _item_id(item), "source": _projection(item)}
        for name in ("context", "terms", "hints", "constraints"):
            value = getattr(item, name, None)
            if value:
                data[name] = value
    if stage == "review":
        data.setdefault("target", _target(item))
        data.setdefault("base_revision", 0)
        data.setdefault(
            "applicability",
            {"terminology": bool(data.get("terms")), "bindings": "⟦" in data["source"]},
        )
        data.setdefault("bindings", ())
    else:
        data.pop("applicability", None)
        data.pop("bindings", None)
    return data


def _projection(item: Any) -> str:
    if isinstance(item, Mapping):
        value = item.get("source_projection", item.get("source", ""))
    else:
        value = getattr(item, "source_projection", getattr(item, "source", ""))
    return str(value)


def _target(item: Any) -> str:
    if isinstance(item, Mapping):
        return str(item.get("target_projection", item.get("target", "")))
    return str(getattr(item, "target_projection", getattr(item, "target", "")))


def _item_id(item: Any) -> str:
    if isinstance(item, Mapping):
        return str(item.get("item_id", item.get("unit_id", "unknown")))
    return str(getattr(item, "item_id", getattr(item, "unit_id", "unknown")))


def _atomize(events: Sequence[Event]) -> tuple[Event, ...]:
    atoms: list[Event] = []
    for event in events:
        if event.kind == "marker":
            atoms.append(event)
        else:
            atoms.extend(Event(kind="text", value=cluster) for cluster in regex.findall(r"\X", event.value))
    return tuple(atoms)


def _range_stacks(events: Sequence[Event]) -> tuple[tuple[str, ...], ...]:
    stacks: list[tuple[str, ...]] = []
    stack: list[str] = []
    for event in events:
        stacks.append(tuple(stack))
        if event.kind != "marker":
            continue
        marker = event.value
        if marker.startswith(("+g", "+b")):
            stack.append(marker[1:])
        elif marker.startswith(("-g", "-b")):
            if not stack or stack[-1] != marker[1:]:
                raise PlanningError(f"invalid source range stack at {marker}")
            stack.pop()
    stacks.append(tuple(stack))
    if stack:
        raise PlanningError(f"unclosed source range {stack[-1]}")
    return tuple(stacks)


def _segment_events(
    atoms: Sequence[Event], stacks: Sequence[tuple[str, ...]], start: int, end: int
) -> tuple[Event, ...]:
    prefix = [Event(kind="marker", value=f"+{ref}", virtual=True) for ref in stacks[start]]
    suffix = [Event(kind="marker", value=f"-{ref}", virtual=True) for ref in reversed(stacks[end])]
    return _compact((*prefix, *atoms[start:end], *suffix))


def _compact(events: Sequence[Event]) -> tuple[Event, ...]:
    compact: list[Event] = []
    for event in events:
        if event.kind == "text" and compact and compact[-1].kind == "text" and compact[-1].virtual == event.virtual:
            compact[-1] = Event(kind="text", value=compact[-1].value + event.value, virtual=event.virtual)
        elif event.kind != "text" or event.value:
            compact.append(event)
    return tuple(compact)


def _boundary_score(atoms: Sequence[Event], end: int) -> int:
    if end >= len(atoms):
        return 4
    previous = atoms[end - 1]
    if previous.kind != "text":
        return 0
    char = previous.value
    before = "".join(event.value for event in atoms[max(0, end - 16) : end] if event.kind == "text")
    after = atoms[end].value if atoms[end].kind == "text" else ""
    if char in ".!?。！？" and not _false_sentence_boundary(before, after):
        return 3
    if char in ",;:，；：":
        return 2
    if char.isspace():
        return 1
    return 0


def _false_sentence_boundary(before: str, after: str) -> bool:
    if before.endswith(".") and before[:-1].endswith(("Mr", "Mrs", "Ms", "Dr", "Prof", "e.g", "i.e")):
        return True
    if len(before) >= 2 and before[-2].isdigit() and after[:1].isdigit():
        return True
    return bool(regex.search(r"(?:\b[A-Za-z]\.){2,}$", before))


def _choose_cut(start: int, fitting: Sequence[tuple[int, int]]) -> int:
    farthest = fitting[-1][0]
    threshold = start + max(1, (farthest - start) * 3 // 5)
    preferred = [candidate for candidate in fitting if candidate[0] >= threshold and candidate[1] > 0]
    if not preferred:
        return farthest
    best_score = max(score for _, score in preferred)
    return max(end for end, score in preferred if score == best_score)


def _make_segment(unit: Unit, epoch: int, index: int, start: int, end: int, events: Sequence[Event]) -> Segment:
    segment_id = f"{unit.unit_id}:e{epoch}:s{index}"
    item_id = segment_id
    projection = events_to_projection(events)
    virtual = tuple(event.value for event in events if event.virtual)
    data = {
        "segment_id": segment_id,
        "item_id": item_id,
        "source_start": start,
        "source_end": end,
        "source_projection": projection,
        "events": [event.model_dump(mode="json") for event in events],
        "virtual_boundaries": virtual,
    }
    return Segment(**data, segment_hash=canonical_hash(data))


def _make_plan(epoch: int, segments: tuple[Segment, ...]) -> CutPlan:
    plan_hash = canonical_hash(
        {"plan_epoch": epoch, "segments": [segment.model_dump(mode="json") for segment in segments]}
    )
    return CutPlan(plan_epoch=epoch, plan_hash=plan_hash, segments=segments)


def _target_map(plan: CutPlan, targets: Mapping[str, str] | Sequence[str]) -> dict[str, str]:
    if isinstance(targets, Mapping):
        result: dict[str, str] = {}
        for segment in plan.segments:
            value = targets.get(segment.item_id, targets.get(segment.segment_id))
            if value is not None:
                result[segment.item_id] = value
        return result
    if len(targets) != len(plan.segments):
        raise PlanningError("target sequence length does not match CutPlan")
    return {segment.item_id: target for segment, target in zip(plan.segments, targets, strict=True)}


def _virtual_edges(events: Sequence[Event]) -> tuple[list[str], list[str]]:
    prefix: list[str] = []
    for event in events:
        if not event.virtual:
            break
        prefix.append(event.value)
    suffix: list[str] = []
    for event in reversed(events):
        if not event.virtual:
            break
        suffix.append(event.value)
    suffix.reverse()
    return prefix, suffix
