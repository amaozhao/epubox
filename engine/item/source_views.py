"""Deterministic natural-language views over structural source extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from engine.item.inline import ProjectionError, validate_projection
from engine.schemas.contracts import (
    DocumentPlan,
    SourceRef,
    SourceTextView,
    canonical_hash,
    source_view_hash_payload,
)
from engine.schemas.source_internal import DocumentPlan as ExtractedDocument
from engine.schemas.source_internal import SourceSlot, Unit

SOURCE_VIEW_RULE_VERSION = "epubox-source-view-1"


class SourceViewError(ValueError):
    """A persisted projection cannot be reconciled with its owned source ranges."""


@dataclass(frozen=True)
class SourceViewGap:
    unit_id: str
    reason: str
    source_refs: tuple[SourceRef, ...] = ()


@dataclass(frozen=True)
class SourceViewDerivation:
    source_views: dict[str, SourceTextView]
    unit_source_view_ids: dict[str, tuple[str, ...]]
    coverage_gaps: tuple[SourceViewGap, ...]


@dataclass(frozen=True)
class _OwnedFragment:
    slot: SourceSlot
    start: int
    end: int
    domain_node_key: str


def derive_source_views(document: ExtractedDocument) -> SourceViewDerivation:
    """Build source views without reparsing XML or changing source ownership."""

    views: dict[str, SourceTextView] = {}
    unit_views: dict[str, tuple[str, ...]] = {}
    gaps: list[SourceViewGap] = []

    for unit in document.units:
        unit_result, unit_gaps = _derive_unit_views(document, unit)
        for view in unit_result:
            if view.view_id in views:
                raise SourceViewError(f"duplicate source view identity: {view.view_id}")
            views[view.view_id] = view
        unit_views[unit.unit_id] = tuple(view.view_id for view in unit_result)
        gaps.extend(unit_gaps)

    return SourceViewDerivation(views, unit_views, tuple(gaps))


def validate_source_views(document: DocumentPlan) -> None:
    """Replay source views from frozen slots/projections; self-consistent hashes are insufficient."""

    expected = derive_source_views(cast(ExtractedDocument, document))
    if document.source_views != expected.source_views:
        raise SourceViewError("persisted source_views do not match frozen source slots and projections")
    actual_unit_views = {unit.unit_id: unit.source_view_ids for unit in document.units}
    if actual_unit_views != expected.unit_source_view_ids:
        raise SourceViewError("persisted Unit source_view_ids do not match replayed source views")


def _derive_unit_views(
    document: ExtractedDocument, unit: Unit
) -> tuple[tuple[SourceTextView, ...], tuple[SourceViewGap, ...]]:
    fragments = _owned_fragments(document, unit)
    fragment_index = 0
    fragment_offset = 0
    text_parts: list[str] = []
    refs: list[SourceRef] = []
    views: list[SourceTextView] = []
    gaps: list[SourceViewGap] = []
    range_stack: list[str] = []

    def flush() -> None:
        if not text_parts:
            return
        text = "".join(text_parts)
        source_refs = tuple(refs)
        text_parts.clear()
        refs.clear()
        if not text.strip():
            gaps.append(SourceViewGap(unit.unit_id, "whitespace_only", source_refs))
            return
        data = {
            "unit_id": unit.unit_id,
            "document_id": document.document_id,
            "text": text,
            "source_refs": source_refs,
            "view_kind": "primary",
        }
        view_hash = canonical_hash(source_view_hash_payload(**data))
        view_id = "sv-" + canonical_hash({"rule_version": SOURCE_VIEW_RULE_VERSION, "view_hash": view_hash})
        views.append(SourceTextView(view_id=view_id, view_hash=view_hash, **data))

    try:
        events = validate_projection(unit)
    except ProjectionError as exc:
        raise SourceViewError(f"{unit.unit_id}: invalid source projection: {exc}") from exc

    for event in events:
        if event.kind == "marker":
            ref_id = event.value[1:]
            entry = unit.registry.get(ref_id)
            if entry is None or entry.kind != ref_id[:1]:
                raise SourceViewError(f"{unit.unit_id}: projection marker {ref_id!r} has no matching registry entry")
            if entry.kind in {"x", "b"}:
                flush()
            if event.value[0] == "+":
                range_stack.append(ref_id)
            elif event.value[0] == "-" and (not range_stack or range_stack.pop() != ref_id):
                raise SourceViewError(f"{unit.unit_id}: projection has crossed or unmatched marker {ref_id!r}")
            continue

        remaining = event.value
        while remaining:
            if fragment_index >= len(fragments):
                raise SourceViewError(f"{unit.unit_id}: projection contains text outside owned source ranges")
            fragment = fragments[fragment_index]
            domain_node_key = next(
                (unit.registry[ref_id].source_node_key for ref_id in reversed(range_stack) if ref_id.startswith("g")),
                unit.node_key,
            )
            if fragment.domain_node_key != domain_node_key:
                raise SourceViewError(
                    f"{unit.unit_id}: source slot {fragment.slot.slot_id} is not owned by the active projection domain"
                )
            start = fragment.start + fragment_offset
            available = fragment.end - start
            take = min(len(remaining), available)
            expected = fragment.slot.source_value[start : start + take]
            actual = remaining[:take]
            if actual != expected:
                raise SourceViewError(
                    f"{unit.unit_id}: projection/source mismatch at {fragment.slot.slot_id}[{start}:{start + take}]"
                )
            text_parts.append(actual)
            _append_ref(refs, fragment.slot.slot_id, start, start + take)
            remaining = remaining[take:]
            fragment_offset += take
            if fragment_offset == fragment.end - fragment.start:
                fragment_index += 1
                fragment_offset = 0

    flush()
    if range_stack:
        raise SourceViewError(f"{unit.unit_id}: projection has unclosed marker {range_stack[-1]!r}")
    if fragment_index != len(fragments) or fragment_offset:
        fragment = fragments[fragment_index]
        raise SourceViewError(
            f"{unit.unit_id}: owned source range was not represented in the projection: "
            f"{fragment.slot.slot_id}[{fragment.start + fragment_offset}:{fragment.end}]"
        )
    if not views and not gaps:
        gaps.append(SourceViewGap(unit.unit_id, "no_natural_language_text"))
    return tuple(views), tuple(gaps)


def _owned_fragments(document: ExtractedDocument, unit: Unit) -> tuple[_OwnedFragment, ...]:
    fragments: list[_OwnedFragment] = []
    listed = set(unit.slot_ids)
    node_by_path = {node.element_path: node.node_key for node in document.nodes.values()}
    special_tail_parents = {
        str(boundary["slot_id"]): str(boundary["parent_node_key"])
        for boundary in document.boundaries
        if boundary.get("kind") == "non_element_tail"
        and isinstance(boundary.get("slot_id"), str)
        and isinstance(boundary.get("parent_node_key"), str)
    }
    for slot_id, slot in document.source_slots.items():
        owns_unit = any(part.owner_kind == "unit" and part.owner_unit_id == unit.unit_id for part in slot.ranges)
        if owns_unit != (slot_id in listed):
            raise SourceViewError(f"{unit.unit_id}: slot ownership disagrees with slot_ids for {slot_id}")

    for slot_id in unit.slot_ids:
        slot = document.source_slots.get(slot_id)
        if slot is None:
            raise SourceViewError(f"{unit.unit_id}: unknown source slot {slot_id}")
        owned = [part for part in slot.ranges if part.owner_kind == "unit" and part.owner_unit_id == unit.unit_id]
        if not owned:
            raise SourceViewError(f"{unit.unit_id}: source slot {slot_id} has no range owned by the Unit")
        if slot.field == "tail":
            node = document.nodes.get(slot.node_key)
            domain_node_key = special_tail_parents.get(slot_id)
            if domain_node_key is None:
                domain_node_key = node_by_path.get(node.element_path[:-1]) if node is not None else None
            if domain_node_key is None:
                raise SourceViewError(f"{unit.unit_id}: tail slot {slot_id} has no known parent node")
        else:
            domain_node_key = slot.node_key
        fragments.extend(_OwnedFragment(slot, part.start, part.end, domain_node_key) for part in owned)
    return tuple(fragments)


def _append_ref(refs: list[SourceRef], slot_id: str, start: int, end: int) -> None:
    if start == end:
        return
    if refs and refs[-1].slot_id == slot_id and refs[-1].end == start:
        refs[-1] = SourceRef(slot_id=slot_id, start=refs[-1].start, end=end)
    else:
        refs.append(SourceRef(slot_id=slot_id, start=start, end=end))


__all__ = [
    "SOURCE_VIEW_RULE_VERSION",
    "SourceViewDerivation",
    "SourceViewError",
    "SourceViewGap",
    "derive_source_views",
    "validate_source_views",
]
