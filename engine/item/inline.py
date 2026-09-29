"""Strict text projection codec for the v2.3 translation protocol."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from engine.schemas.source_internal import Event

LEFT = "⟦"
RIGHT = "⟧"
_MARKER = re.compile(r"(?:(?P<edge>[+-])(?P<range>[gb][A-Za-z0-9_.:-]+)|=(?P<atom>x[A-Za-z0-9_.:-]+))\Z")


class ProjectionError(ValueError):
    """A projection cannot be decoded or violates its frozen registry."""


def escape_text(text: str) -> str:
    """Escape projection syntax in literal text before JSON serialization."""
    return text.replace("\\", "\\\\").replace(LEFT, f"\\{LEFT}").replace(RIGHT, f"\\{RIGHT}")


def events_to_projection(events: Iterable[Event | Sequence[Any] | Mapping[str, Any]]) -> str:
    """Serialize events without accepting raw model markup."""
    parts: list[str] = []
    for raw in events:
        event = _event(raw)
        if event.kind == "text":
            parts.append(escape_text(event.value))
        elif event.kind == "marker" and _MARKER.fullmatch(event.value):
            parts.append(f"{LEFT}{event.value}{RIGHT}")
        else:
            raise ProjectionError(f"invalid event: {event.kind} {event.value!r}")
    return "".join(parts)


def parse_projection(projection: str) -> tuple[Event, ...]:
    """Decode a projection with a character scanner; malformed syntax is fatal."""
    if not isinstance(projection, str):
        raise ProjectionError("projection must be a string")

    events: list[Event] = []
    text: list[str] = []

    def flush() -> None:
        if text:
            events.append(Event(kind="text", value="".join(text)))
            text.clear()

    index = 0
    while index < len(projection):
        char = projection[index]
        if char == "\\":
            index += 1
            if index == len(projection):
                raise ProjectionError("dangling projection escape")
            escaped = projection[index]
            if escaped not in {"\\", LEFT, RIGHT}:
                raise ProjectionError(f"unsupported projection escape: \\{escaped}")
            text.append(escaped)
            index += 1
            continue
        if char == RIGHT:
            raise ProjectionError("literal closing delimiter must be escaped")
        if char != LEFT:
            text.append(char)
            index += 1
            continue

        flush()
        end = projection.find(RIGHT, index + 1)
        if end < 0:
            raise ProjectionError("unclosed projection marker")
        marker = projection[index + 1 : end]
        match = _MARKER.fullmatch(marker)
        if match is None:
            raise ProjectionError(f"unknown projection marker: {marker}")
        events.append(Event(kind="marker", value=marker))
        index = end + 1

    flush()
    return tuple(events)


def plain_text(projection: str | Iterable[Event]) -> str:
    """Return only human text; local g/x/b references contribute no text."""
    events = parse_projection(projection) if isinstance(projection, str) else tuple(projection)
    return "".join(event.value for event in events if event.kind == "text")


def projection_identities(projection: str | Iterable[Event]) -> tuple[str, ...]:
    """Return each real g/x/b identity once, in source encounter order."""
    events = parse_projection(projection) if isinstance(projection, str) else tuple(projection)
    return tuple(_shape(events, "projection")["ordered"])


def validate_projection(
    unit: Any,
    target: str | None = None,
    registry: Mapping[str, Any] | Sequence[Any] | None = None,
) -> tuple[Event, ...]:
    """Validate target structure against a Unit (or a source projection string).

    The function intentionally accepts plain mappings and objects so persisted JSON
    can be checked before it is promoted to a richer in-memory model.
    """
    source = unit if isinstance(unit, str) else _field(unit, "source_projection", "projection", "source")
    if not isinstance(source, str):
        raise ProjectionError("unit has no source projection")
    if target is None:
        target = source
    if not isinstance(target, str):
        raise ProjectionError("target projection must be a string")

    source_events = parse_projection(source)
    target_events = parse_projection(target)
    _validate_xml_text(target_events)
    source_shape = _shape(source_events, "source")
    target_shape = _shape(target_events, "target")

    refs = _registry(registry if registry is not None else _field(unit, "registry", default={}))
    if not _unit_allows_markers(unit) and target_shape["inventory"]:
        raise ProjectionError("plain text and metadata units cannot contain control markers")
    if not isinstance(unit, str) and set(refs) != source_shape["inventory"]:
        raise ProjectionError("unit registry does not exactly match its source projection")

    if source_shape["inventory"] != target_shape["inventory"]:
        missing = source_shape["inventory"] - target_shape["inventory"]
        extra = target_shape["inventory"] - source_shape["inventory"]
        raise ProjectionError(f"marker inventory mismatch (missing={sorted(missing)}, extra={sorted(extra)})")
    for ref in source_shape["inventory"]:
        expected_kind = ref[0]
        entry = refs.get(ref)
        if refs and entry is None:
            raise ProjectionError(f"unknown registry reference: {ref}")
        if entry is not None and _ref_kind(entry, ref) != expected_kind:
            raise ProjectionError(f"registry kind mismatch for {ref}")
        if source_shape["parents"][ref] != target_shape["parents"][ref]:
            raise ProjectionError(f"reference moved across parent or boundary: {ref}")

    if any(ref.startswith("b") for ref in source_shape["inventory"]):
        if _boundary_domain_sequence(source_shape, refs) != _boundary_domain_sequence(target_shape, refs):
            raise ProjectionError("text moved across a protected range or boundary")
    elif set(source_shape["text_domains"]) != set(target_shape["text_domains"]):
        raise ProjectionError("text moved across a protected range")
    _validate_locked_order(source_shape, target_shape, refs)
    return target_events


def _event(raw: Event | Sequence[Any] | Mapping[str, Any]) -> Event:
    if isinstance(raw, Event):
        return raw
    if isinstance(raw, Mapping):
        value = raw.get("value", raw.get("ref"))
        kind = raw.get("kind")
        virtual = raw.get("virtual", False)
    else:
        if isinstance(raw, (str, bytes)) or len(raw) not in {2, 3}:
            raise ProjectionError("event tuples need kind, value, and optional virtual")
        kind, value = raw[:2]
        virtual = raw[2] if len(raw) == 3 else False
    if kind not in {"text", "marker"} or not isinstance(value, str) or not isinstance(virtual, bool):
        raise ProjectionError("event has an invalid kind, value, or virtual flag")
    return Event(kind=kind, value=value, virtual=virtual)


def _shape(events: Sequence[Event], label: str) -> dict[str, Any]:
    stack: list[str] = []
    seen: set[str] = set()
    inventory: set[str] = set()
    parents: dict[str, tuple[str, ...]] = {}
    ordered: list[str] = []
    text_domains: list[tuple[str, ...]] = []
    for event in events:
        if event.kind == "text":
            if event.value.strip():
                text_domains.append(tuple(stack))
            continue
        if event.kind != "marker":
            raise ProjectionError(f"unknown event kind: {event.kind}")
        marker = event.value
        match = _MARKER.fullmatch(marker)
        if match is None:
            raise ProjectionError(f"unknown {label} marker: {marker}")
        ref = match.group("atom") or match.group("range")
        edge = "atom" if match.group("atom") else ("open" if match.group("edge") == "+" else "close")
        if edge == "open":
            if ref in seen:
                raise ProjectionError(f"duplicate {label} reference: {ref}")
            seen.add(ref)
            inventory.add(ref)
            parents[ref] = tuple(stack)
            ordered.append(ref)
            stack.append(ref)
        elif edge == "close":
            if not stack or stack[-1] != ref:
                raise ProjectionError(f"crossed or unmatched {label} close marker: {ref}")
            stack.pop()
        else:
            if ref in seen:
                raise ProjectionError(f"duplicate {label} reference: {ref}")
            seen.add(ref)
            inventory.add(ref)
            parents[ref] = tuple(stack)
            ordered.append(ref)
    if stack:
        raise ProjectionError(f"unclosed {label} reference: {stack[-1]}")
    return {"inventory": inventory, "parents": parents, "ordered": ordered, "text_domains": text_domains}


def _boundary_domain_sequence(shape: Mapping[str, Any], refs: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    boundaries = {ref for ref in shape["inventory"] if ref.startswith("b")}
    fixed_ancestors = {
        ancestor
        for boundary in boundaries
        for ancestor in shape["parents"][boundary]
        if ancestor.startswith("g")
        and (
            str(_field(refs.get(ancestor), "movement", default="same_parent")) in {"fixed", "locked"}
            or not bool(_field(refs.get(ancestor), "reorder_allowed", default=True))
        )
    }
    relevant = boundaries | fixed_ancestors
    sequence: list[tuple[str, ...]] = []
    for stack in shape["text_domains"]:
        domain = tuple(ref for ref in stack if ref in relevant)
        if not sequence or sequence[-1] != domain:
            sequence.append(domain)
    return tuple(sequence)


def _validate_xml_text(events: Sequence[Event]) -> None:
    for event in events:
        if event.kind != "text":
            continue
        for char in event.value:
            code = ord(char)
            if not (
                code in {0x9, 0xA, 0xD}
                or 0x20 <= code <= 0xD7FF
                or 0xE000 <= code <= 0xFFFD
                or 0x10000 <= code <= 0x10FFFF
            ):
                raise ProjectionError(f"target contains an XML-invalid character: U+{code:04X}")


def _registry(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    result: dict[str, Any] = {}
    for entry in raw:
        ref = _field(entry, "ref_id", "id", "name")
        if not isinstance(ref, str) or ref in result:
            raise ProjectionError("registry references must have unique string IDs")
        result[ref] = entry
    return result


def _ref_kind(entry: Any, ref: str) -> str:
    kind = _field(entry, "kind", "ref_kind", default=ref[0])
    return str(getattr(kind, "value", kind)).lower()[0]


def _validate_locked_order(source: Mapping[str, Any], target: Mapping[str, Any], refs: Mapping[str, Any]) -> None:
    source_order: list[str] = source["ordered"]
    target_order: list[str] = target["ordered"]
    source_index = {ref: index for index, ref in enumerate(source_order)}
    target_index = {ref: index for index, ref in enumerate(target_order)}
    groups: dict[tuple[Any, ...], list[str]] = defaultdict(list)
    parent_members: dict[str, list[str]] = defaultdict(list)
    locked_refs: list[tuple[str, str]] = []

    for ref in source_order:
        entry = refs.get(ref)
        kind = ref[0]
        parent_scope = str(_field(entry, "parent_ref", default=source["parents"][ref]))
        parent_members[parent_scope].append(ref)
        boundary = str(_field(entry, "boundary_type", default="")).lower()
        movement = str(_field(entry, "movement", default="same_parent" if kind == "g" else "locked"))
        reorder = bool(_field(entry, "reorder_allowed", default=kind == "g"))
        fixed_order = tuple(_field(entry, "fixed_order", default=()))

        if kind == "b":
            groups[("boundary", parent_scope)].append(ref)
        if fixed_order:
            if any(item not in refs for item in fixed_order):
                raise ProjectionError(f"fixed order for {ref} names an unknown reference")
            present_fixed = tuple(item for item in fixed_order if item in source_index)
            source_fixed = tuple(sorted(present_fixed, key=source_index.__getitem__))
            target_fixed = tuple(sorted(present_fixed, key=target_index.__getitem__))
            if source_fixed != present_fixed or target_fixed != present_fixed:
                raise ProjectionError(f"fixed reference order changed: {fixed_order}")
        if kind == "x" and boundary in {"code", "formula", "math", "footnote", "br", "page", "anchor"}:
            groups[("hard", parent_scope)].append(ref)
        if movement in {"locked", "fixed"} or not reorder:
            locked_refs.append((ref, parent_scope))

    for anchor, parent_scope in locked_refs:
        for other in parent_members[parent_scope]:
            if other == anchor:
                continue
            source_relation = source_index[anchor] < source_index[other]
            target_relation = target_index[anchor] < target_index[other]
            if source_relation != target_relation:
                raise ProjectionError(f"locked reference order changed around: {anchor}")

    for members in groups.values():
        members = list(dict.fromkeys(members))
        if len(members) < 2:
            continue
        expected = sorted(members, key=source_index.__getitem__)
        actual = sorted(members, key=target_index.__getitem__)
        if actual != expected:
            raise ProjectionError(f"locked reference order changed: {expected}")


def _unit_allows_markers(unit: Any) -> bool:
    if isinstance(unit, str):
        return True
    kind = str(_field(unit, "unit_type", "kind", default="")).lower()
    return not any(word in kind for word in ("attribute", "metadata", "navigation", "plain", "opf"))


def _field(value: Any, *names: str, default: Any = None) -> Any:
    if value is None:
        return default
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default
