"""Verified, byte-local rendering of translated atomic resources."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from engine.epub.parsing import parse_resource
from engine.epub.ranges import NodeSpan, RangeError, RawSpan, ResourceIndex, SlotSpan, index_resource
from engine.item.atoms import _span
from engine.item.inline import ProjectionError, parse_projection, validate_item_target
from engine.schemas.bridge import AtomicDocument, AtomicItem, ByteSpan
from engine.schemas.internal import Event
from engine.schemas.source import RegistryEntry


class FillError(ValueError):
    """A saved inventory or translated target cannot be written safely."""


@dataclass(frozen=True)
class _Patch:
    span: RawSpan
    value: bytes


def fill_resource(
    raw: bytes,
    inventory: AtomicDocument,
    targets: Mapping[str, str],
    *,
    identity: bool = False,
) -> bytes:
    """Render every validated target while copying all unregistered source bytes."""
    saved = AtomicDocument.model_validate_json(inventory.model_dump_json())
    expected = {item.item_id for item in saved.items}
    actual = set(targets)
    if actual != expected:
        raise FillError(f"target IDs differ (missing={sorted(expected - actual)}, extra={sorted(actual - expected)})")
    if any(not isinstance(targets[item_id], str) or not targets[item_id] for item_id in expected):
        raise FillError("every atomic item requires a non-empty string target")

    digest = hashlib.sha256(raw).hexdigest()
    if digest != saved.source_map.document_hash or digest != saved.document.resource.source_sha256:
        raise FillError("raw resource differs from the saved atomic inventory")
    try:
        parsed = parse_resource(raw, saved.document.resource.media_type)
        if parsed.diagnostics:
            raise FillError("; ".join(parsed.diagnostics))
        index = index_resource(parsed)
        if index.bind_document(saved.document) != saved.source_map:
            raise FillError("saved source map differs from the indexed raw resource")
        _verify_spans(saved, index)
        _authorize(saved, index)
        events = {item.item_id: validate_item_target(item, targets[item.item_id]) for item in saved.items}
    except (ProjectionError, RangeError, ValueError) as exc:
        if isinstance(exc, FillError):
            raise
        raise FillError(str(exc)) from exc
    if identity:
        if any(targets[item.item_id] != item.source_projection for item in saved.items):
            raise FillError("identity rendering requires every original source projection")
        return raw

    renderer = _Renderer(raw, saved, index, targets, events)
    result = renderer.render()
    _verify_result(result, saved, index)
    return result


def _verify_spans(saved: AtomicDocument, index: ResourceIndex) -> None:
    lexical = {(slot.path, slot.field, slot.attribute_name, slot.special_index): slot for slot in index.slots}
    for item, unit in zip(saved.items, saved.document.units, strict=True):
        if item.source_span != _span(unit, saved.document, index, lexical):
            raise FillError(f"item source span is not authoritative: {item.item_id}")


def _authorize(saved: AtomicDocument, index: ResourceIndex) -> None:
    paths = {key: node.element_path for key, node in saved.document.nodes.items()}
    slots = {(slot.path, slot.field, slot.attribute_name, slot.special_index): slot for slot in index.slots}
    special_slots: dict[str, int] = {}
    for boundary in saved.document.boundaries:
        slot_id, child_index = boundary.get("slot_id"), boundary.get("child_index")
        if boundary.get("kind") == "non_element_tail" and isinstance(slot_id, str) and type(child_index) is int:
            special_slots[slot_id] = child_index
    for item in saved.items:
        root = item.node_key
        groups: list[str] = []
        boundaries: list[str] = []
        seen: set[tuple[int, int]] = set()
        last_by_parent: dict[str, int] = {}
        for event in parse_projection(item.source_projection):
            if event.kind != "marker":
                continue
            edge, ref = event.value[0], event.value[1:]
            entry = item.registry[ref]
            if ref.startswith("g") and edge == "-":
                if not groups or groups.pop() != entry.source_node_key:
                    raise FillError(f"registry group nesting changed: {item.item_id}/{ref}")
                parent = groups[-1] if groups else root
                if entry.parent_ref != parent:
                    raise FillError(f"registry parent differs from marker context: {item.item_id}/{ref}")
                continue
            parent = groups[-1] if groups else root
            if entry.parent_ref != parent:
                raise FillError(f"registry parent differs from marker context: {item.item_id}/{ref}")
            if ref.startswith("b"):
                if edge == "+":
                    boundaries.append(ref)
                elif not boundaries or boundaries.pop() != ref:
                    raise FillError(f"registry boundary nesting changed: {item.item_id}/{ref}")
                continue
            span, owner = _entry_span(entry, saved, index, paths, slots, special_slots)
            item_span = item.source_span
            if span.start < item_span.byte_start or span.end > item_span.byte_end:
                raise FillError(f"registry source escapes its atomic item: {item.item_id}/{ref}")
            physical = (span.start, span.end)
            if physical in seen:
                raise FillError(f"registry source is claimed more than once: {item.item_id}/{ref}")
            seen.add(physical)
            if owner != parent:
                raise FillError(f"registry source has the wrong DOM parent: {item.item_id}/{ref}")
            if span.start < last_by_parent.get(parent, -1):
                raise FillError(f"registry source order differs from the raw document: {item.item_id}/{ref}")
            last_by_parent[parent] = span.start
            if ref.startswith("g"):
                groups.append(entry.source_node_key)
        if groups or boundaries:
            raise FillError(f"registry source projection ended with open markers: {item.item_id}")


def _entry_span(
    entry: RegistryEntry,
    saved: AtomicDocument,
    index: ResourceIndex,
    paths: Mapping[str, tuple[int, ...]],
    slots: Mapping[tuple[tuple[int, ...], str, str | None, int | None], SlotSpan],
    special_slots: Mapping[str, int],
) -> tuple[RawSpan, str]:
    hints = entry.hints
    if "slot_id" in hints:
        try:
            slot = saved.document.source_slots[hints["slot_id"]]
            start, end = int(hints["start"]), int(hints["end"])
        except (KeyError, ValueError) as exc:
            raise FillError(f"registry slot reference is invalid: {entry.ref_id}") from exc
        if (
            start < 0
            or start >= end
            or end > len(slot.source_value)
            or entry.source_node_key != slot.node_key
            or entry.source_text != slot.source_value[start:end]
        ):
            raise FillError(f"registry slot evidence differs from its source: {entry.ref_id}")
        special = special_slots.get(slot.slot_id)
        try:
            lexical = slots[(paths[slot.node_key], slot.field, slot.attribute_name, special)]
            span = _rawspan(lexical.source_span(start, end))
        except (KeyError, RangeError) as exc:
            raise FillError(f"registry slot has no verified source bytes: {entry.ref_id}") from exc
        return span, slot.node_key
    if "child_index" in hints:
        kind = hints.get("node_kind")
        try:
            child_index = int(hints["child_index"])
            span = index.specials[(paths[entry.source_node_key], child_index, str(kind))]
        except (KeyError, ValueError) as exc:
            raise FillError(f"registry special node has no verified source bytes: {entry.ref_id}") from exc
        return span, entry.source_node_key
    try:
        path = paths[entry.source_node_key]
        span = index.nodes[path].full
        parent_path = paths.get(entry.parent_ref)
        if parent_path is None or path[:-1] != parent_path:
            raise FillError(f"registry element has the wrong DOM parent: {entry.ref_id}")
        local = index.nodes[path].qname.rsplit("}", 1)[-1]
        if entry.hints.get("element") != local or entry.source_text != _node_text(path, index.slots):
            raise FillError(f"registry element evidence differs from its source: {entry.ref_id}")
        return span, entry.parent_ref
    except KeyError as exc:
        raise FillError(f"registry element has no verified source node: {entry.ref_id}") from exc


def _node_text(path: tuple[int, ...], slots: Sequence[SlotSpan]) -> str:
    parts: list[tuple[int, str]] = []
    for slot in slots:
        descendant = slot.path[: len(path)] == path
        included = descendant and (
            slot.field == "text" or slot.field == "tail" and (slot.path != path or slot.special_index is not None)
        )
        if included and slot.chars:
            parts.append((slot.chars[0].raw.start, slot.text))
    return "".join(text for _, text in sorted(parts))


class _Renderer:
    def __init__(
        self,
        raw: bytes,
        saved: AtomicDocument,
        index: ResourceIndex,
        targets: Mapping[str, str],
        events: Mapping[str, Sequence[Event]],
    ) -> None:
        self.raw = raw
        self.saved = saved
        self.index = index
        self.targets = targets
        self.events = events
        self.paths = {key: node.element_path for key, node in saved.document.nodes.items()}
        self.nodes = {key: index.nodes[path] for key, path in self.paths.items()}
        self.slots = {(slot.path, slot.field, slot.attribute_name, slot.special_index): slot for slot in index.slots}
        self.special_slots: dict[str, int] = {}
        for boundary in saved.document.boundaries:
            slot_id, child_index = boundary.get("slot_id"), boundary.get("child_index")
            if boundary.get("kind") == "non_element_tail" and isinstance(slot_id, str) and type(child_index) is int:
                self.special_slots[slot_id] = child_index
        self.attributes: list[_Patch] = []

    def render(self) -> bytes:
        attributes = [item for item in self.saved.items if item.region.get("type") == "attribute"]
        self.attributes = [
            _Patch(
                _rawspan(item.source_span),
                self.raw[item.source_span.byte_start : item.source_span.byte_end]
                if self.targets[item.item_id] == item.source_projection
                else self._events(item, attribute=True),
            )
            for item in attributes
        ]
        patches: list[_Patch] = []
        for item in self.saved.items:
            if item.region.get("type") == "attribute":
                if not item.region.get("patch_owner_id"):
                    patches.append(
                        next(patch for patch in self.attributes if patch.span == _rawspan(item.source_span))
                    )
                continue
            patches.append(_Patch(_rawspan(item.source_span), self._item(item)))
        return _splice(self.raw, RawSpan(0, len(self.raw)), patches)

    def _item(self, item: AtomicItem) -> bytes:
        span = _rawspan(item.source_span)
        nested = [patch for patch in self.attributes if span.start <= patch.span.start and patch.span.end <= span.end]
        if self.targets[item.item_id] == item.source_projection:
            return _splice(self.raw, span, nested)
        if simple := self._simple(item):
            return _splice(self.raw, span, [*nested, simple])
        node = self.nodes[item.node_key]
        wrapped = span == node.full and not node.self_closing
        self._assert_cdata_safe(item, span)
        body = self._events(item)
        if not wrapped:
            return body
        return self._copy(node.starttag) + body + self._copy(node.endtag)

    def _simple(self, item: AtomicItem) -> _Patch | None:
        if len(item.slot_ids) != 1:
            return None
        slot_id = item.slot_ids[0]
        slot = self.saved.document.source_slots[slot_id]
        if any(part.owner_unit_id not in {None, item.item_id} for part in slot.ranges):
            return None
        events = self.events[item.item_id]
        if any(
            event.kind == "marker"
            and (not event.value.startswith("=x") or item.registry[event.value[1:]].hints.get("slot_id") != slot_id)
            for event in events
        ):
            return None
        lexical = self._slot(slot_id)
        try:
            rawspan = _rawspan(lexical.source_span(0, len(slot.source_value)))
        except RangeError:
            return None
        if self._cdata(item, rawspan):
            parts = [
                event.value.replace("]]>", "]]]]><![CDATA[>").encode(self.index.encoding)
                if event.kind == "text"
                else self._opaque(item.registry[event.value[1:]])
                for event in events
            ]
            encoded = b"".join(parts)
        else:
            encoded = self._events(item)
        return _Patch(rawspan, encoded)

    def _cdata(self, item: AtomicItem, span: RawSpan) -> bool:
        node = self.nodes[item.node_key]
        prefix = self.raw[node.content.start : span.start].decode(self.index.encoding)
        return prefix.rfind("<![CDATA[") > prefix.rfind("]]>")

    def _events(self, item: AtomicItem, *, attribute: bool = False) -> bytes:
        result: list[bytes] = []
        stack: list[tuple[str, NodeSpan | None, bytes | None]] = []
        for event in self.events[item.item_id]:
            if event.kind == "text":
                cdata = not attribute and any(suffix is not None for _, _, suffix in stack)
                escaped = (
                    event.value.replace("]]>", "]]]]><![CDATA[>")
                    if cdata
                    else _attribute(event.value, self._quote(item))
                    if attribute
                    else _text(event.value)
                )
                result.append(escaped.encode(self.index.encoding))
                continue
            edge, ref = event.value[0], event.value[1:]
            entry = item.registry[ref]
            if ref.startswith("b"):
                if edge == "+":
                    stack.append((ref, None, None))
                else:
                    self._pop(stack, ref)
                continue
            if ref.startswith("g"):
                node = self.nodes[entry.source_node_key]
                if edge == "+":
                    result.append(self._copy(node.starttag))
                    wrapper = self._cdata_wrapper(entry.source_node_key)
                    if wrapper is not None:
                        result.append(wrapper[0])
                    stack.append((ref, node, None if wrapper is None else wrapper[1]))
                else:
                    opened, suffix = self._pop(stack, ref)
                    if opened is None:
                        raise FillError(f"range marker has no source node: {ref}")
                    if suffix is not None:
                        result.append(suffix)
                    result.append(self._copy(opened.endtag))
                continue
            if edge != "=":
                raise FillError(f"invalid opaque marker edge: {event.value}")
            result.append(self._opaque(entry))
        if stack:
            raise FillError("target rendering ended with open range markers")
        return b"".join(result)

    @staticmethod
    def _pop(stack: list[tuple[str, NodeSpan | None, bytes | None]], ref: str) -> tuple[NodeSpan | None, bytes | None]:
        if not stack or stack[-1][0] != ref:
            raise FillError(f"range marker nesting changed: {ref}")
        _, node, suffix = stack.pop()
        return node, suffix

    def _cdata_wrapper(self, node_key: str) -> tuple[bytes, bytes] | None:
        path = self.paths[node_key]
        lexical = self.slots.get((path, "text", None, None))
        if lexical is None or not lexical.text:
            return None
        try:
            span = _rawspan(lexical.source_span(0, len(lexical.text)))
        except RangeError:
            return None
        opener = "<![CDATA[".encode(self.index.encoding)
        closer = "]]>".encode(self.index.encoding)
        if (
            self.raw[max(0, span.start - len(opener)) : span.start] == opener
            and self.raw[span.end : span.end + len(closer)] == closer
        ):
            return opener, closer
        return None

    def _assert_cdata_safe(self, item: AtomicItem, span: RawSpan) -> None:
        opener = "<![CDATA[".encode(self.index.encoding)
        safe: list[RawSpan] = []
        for entry in item.registry.values():
            if "slot_id" in entry.hints or "child_index" in entry.hints:
                continue
            node = self.nodes[entry.source_node_key]
            if entry.kind == "x" or entry.kind == "g" and self._cdata_wrapper(entry.source_node_key) is not None:
                safe.append(node.full)
        position = self.raw.find(opener, span.start, span.end)
        while position >= 0:
            if not any(part.start <= position < part.end for part in safe):
                raise FillError("mixed CDATA cannot be rendered without changing its lexical structure")
            position = self.raw.find(opener, position + len(opener), span.end)

    def _opaque(self, entry: RegistryEntry) -> bytes:
        hints = entry.hints
        if "slot_id" in hints:
            slot = self.saved.document.source_slots[hints["slot_id"]]
            lexical = self._slot(slot.slot_id)
            span = lexical.source_span(int(hints["start"]), int(hints["end"]))
            return self.raw[span.byte_start : span.byte_end]
        if "child_index" in hints:
            kind = hints.get("node_kind")
            if kind not in {"comment", "pi"}:
                raise FillError("unknown protected non-element node")
            key = (self.paths[entry.source_node_key], int(hints["child_index"]), kind)
            try:
                span = self.index.specials[key]
            except KeyError as exc:
                raise FillError("protected non-element node has no verified byte span") from exc
            return self.raw[span.start : span.end]
        return self._copy(self.nodes[entry.source_node_key].full)

    def _slot(self, slot_id: str) -> SlotSpan:
        slot = self.saved.document.source_slots[slot_id]
        special = self.special_slots.get(slot_id)
        path = self.paths[slot.node_key]
        try:
            return self.slots[(path, slot.field, slot.attribute_name, special)]
        except KeyError as exc:
            raise FillError(f"source slot has no verified lexical range: {slot_id}") from exc

    def _copy(self, span: RawSpan) -> bytes:
        patches = [patch for patch in self.attributes if span.start <= patch.span.start and patch.span.end <= span.end]
        return _splice(self.raw, span, patches)

    def _quote(self, item: AtomicItem) -> str:
        start = item.source_span.byte_start
        for quote in ('"', "'"):
            encoded = quote.encode(self.index.encoding)
            if self.raw[max(0, start - len(encoded)) : start] == encoded:
                return quote
        raise FillError(f"attribute value has no verified quote: {item.item_id}")


def _verify_result(result: bytes, saved: AtomicDocument, source: ResourceIndex) -> None:
    try:
        parsed = parse_resource(result, saved.document.resource.media_type)
        if parsed.diagnostics:
            raise FillError("; ".join(parsed.diagnostics))
        rendered = index_resource(parsed)
    except (RangeError, ValueError) as exc:
        if isinstance(exc, FillError):
            raise
        raise FillError(f"rendered resource is not strict XML: {exc}") from exc
    if Counter(node.qname for node in rendered.nodes.values()) != Counter(
        node.qname for node in source.nodes.values()
    ):
        raise FillError("rendered resource changed the element inventory")
    if Counter(key[2] for key in rendered.specials) != Counter(key[2] for key in source.specials):
        raise FillError("rendered resource changed the comment or processing-instruction inventory")


def _splice(raw: bytes, outer: RawSpan, patches: Sequence[_Patch]) -> bytes:
    ordered = sorted(patches, key=lambda patch: patch.span.start)
    cursor = outer.start
    result: list[bytes] = []
    for patch in ordered:
        if patch.span.start < cursor or patch.span.end > outer.end:
            raise FillError("render patches overlap or escape their authoritative source range")
        result.extend((raw[cursor : patch.span.start], patch.value))
        cursor = patch.span.end
    result.append(raw[cursor : outer.end])
    return b"".join(result)


def _rawspan(span: ByteSpan) -> RawSpan:
    return RawSpan(span.byte_start, span.byte_end)


def _text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attribute(value: str, quote: str) -> str:
    escaped = _text(value).replace("\t", "&#9;").replace("\n", "&#10;").replace("\r", "&#13;")
    return escaped.replace(quote, "&quot;" if quote == '"' else "&apos;")


__all__ = ["FillError", "fill_resource"]
