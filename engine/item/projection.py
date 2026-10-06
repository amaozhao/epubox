"""Projection, marker, and protected-boundary extraction helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from lxml import etree  # type: ignore[attr-defined]

from engine.core.markup import qname_local_name
from engine.item.inline import Event, events_to_projection, literal_marker_spans
from engine.item.planner import MAX_SOURCE_TOKENS, source_token_count
from engine.item.policy import _BLOCK_TAGS, _CJK_RE, _HINT_LIMIT, _LATIN_RE, _bounded, _stable_id
from engine.schemas.internal import RegistryEntry, SlotRange, Unit, canonical_hash

if TYPE_CHECKING:
    from engine.item.structure import OwnerKind, _Extractor, _Slot


def _paragraph_group(
    self,
    parent: etree._Element,
    children: list[etree._Element],
    start: int,
    end: int,
    translated: bool,
    *,
    commit: bool,
    omit_final_tail: bool = False,
) -> bool:
    members = children[start:end]
    after = children[start - 1] if start else None
    before = children[end] if end < len(children) else None
    lead = parent.text if after is None else after.tail
    if (
        len(members) < 2
        or (after is not None and (not isinstance(after.tag, str) or qname_local_name(after.tag) not in _BLOCK_TAGS))
        or (
            before is not None and (not isinstance(before.tag, str) or qname_local_name(before.tag) not in _BLOCK_TAGS)
        )
        or (lead or "").strip()
        or any((member.tail or "").strip() for member in members)
    ):
        return False
    keys = {self.node_keys[node] for member in members for node in member.iter() if isinstance(node.tag, str)}
    # ponytail: scan local slots for previews; index by node only if thousand-paragraph XHTML files become common.
    saved = {slot_id: list(slot.ranges) for slot_id, slot in self.slots.items() if slot.node_key in keys}
    if lead_slot := self._leading_slot(parent, after):
        saved[lead_slot.slot_id] = list(lead_slot.ranges)
    made = self._make_region_unit(
        parent,
        after,
        before,
        translated,
        "paragraph_group",
        member_node_keys=tuple(self.node_keys[member] for member in members),
        omit_final_tail=omit_final_tail,
    )
    fits = made and source_token_count(self.units[-1].source_projection) <= MAX_SOURCE_TOKENS
    if not commit or not fits:
        if made:
            self.units.pop()
        for slot_id, ranges in saved.items():
            self.slots[slot_id].ranges = ranges
    return fits


def _groupable_paragraph(self: _Extractor, node: etree._Element, translated: bool) -> bool:
    return (
        isinstance(node.tag, str)
        and qname_local_name(node.tag) == "p"
        and self._translate_state(node, translated)
        and not self._is_hard(node)
        and not any(isinstance(child.tag, str) and qname_local_name(child.tag) in _BLOCK_TAGS for child in node)
        and bool(_LATIN_RE.search("".join(node.itertext())))
    )


def _make_whole_content_unit(self: _Extractor, node: etree._Element, translated: bool, kind: str) -> None:
    self._make_region_unit(node, None, None, translated, kind)


def _make_region_unit(
    self,
    parent: etree._Element,
    after_node: etree._Element | None,
    before_node: etree._Element | None,
    translated: bool,
    kind: str,
    *,
    member_node_keys: tuple[str, ...] = (),
    omit_final_tail: bool = False,
) -> bool:
    members = self._region_members(parent, after_node, before_node)
    lead_slot = self._leading_slot(parent, after_node)
    has_latin = bool(lead_slot and _LATIN_RE.search(lead_slot.source_value)) or self._members_have_latin(members)
    if (not translated and not self._has_translate_yes(parent)) or not has_latin:
        return False
    region = {
        "type": "content",
        "parent_node_key": self.node_keys[parent],
        "after_node_key": self.node_keys.get(after_node) if after_node is not None else None,
        "before_node_key": self.node_keys.get(before_node) if before_node is not None else None,
    }
    if member_node_keys:
        region["member_node_keys"] = member_node_keys
    unit_id = _stable_id("u", self.source_hash, self.resource_path, canonical_hash(region), self.extractor_version)
    registry: dict[str, RegistryEntry] = {}
    events: list[Event] = []
    slot_ids: list[str] = []
    counter = {"g": 0, "x": 0, "b": 0}

    if lead_slot is not None:
        self._emit_slot(
            lead_slot,
            unit_id,
            events,
            registry,
            counter,
            slot_ids,
            self.node_keys[parent],
            force_protected=not translated,
        )
    for member_index, member in enumerate(members):
        if member is after_node or member is before_node:
            continue
        self._emit_child(
            member,
            unit_id,
            events,
            registry,
            counter,
            slot_ids,
            self.node_keys[parent],
            translated,
            paragraph_boundary=bool(member_node_keys),
        )
        tail = None if omit_final_tail and member_index == len(members) - 1 else self._tail_slot(member)
        if tail is not None:
            self._emit_slot(
                tail,
                unit_id,
                events,
                registry,
                counter,
                slot_ids,
                self.node_keys[parent],
                force_protected=not translated,
            )
    events = list(self._constrain_hard_boundaries(tuple(events), registry, counter))
    if not any(event.kind == "text" and _LATIN_RE.search(event.value) for event in events):
        return False
    projection = events_to_projection(events)
    self.units.append(
        Unit(
            unit_id=unit_id,
            document_id=self.document_id,
            kind=kind,
            source_projection=projection,
            node_key=member_node_keys[0] if member_node_keys else self.node_keys[parent],
            slot_ids=tuple(dict.fromkeys(slot_ids)),
            registry=registry,
            checks=("projection", "source_target", "format_binding"),
            region=region,
            logical_hash="pending",
        )
    )
    return True


def _region_members(
    self, parent: etree._Element, after_node: etree._Element | None, before_node: etree._Element | None
) -> list[etree._Element]:
    children = list(parent)
    start = children.index(after_node) + 1 if after_node is not None else 0
    end = children.index(before_node) if before_node is not None else len(children)
    return children[start:end]


def _members_have_latin(self: _Extractor, members: list[etree._Element]) -> bool:
    for member in members:
        if isinstance(member.tag, str) and not self._is_hard(member) and _LATIN_RE.search("".join(member.itertext())):
            return True
        if member.tail and _LATIN_RE.search(member.tail):
            return True
    return False


def _leading_slot(self: _Extractor, parent: etree._Element, after_node: etree._Element | None) -> _Slot | None:
    return (
        self._slot_for(self.node_keys[parent], "text")
        if after_node is None
        else self._slot_for(self.node_keys[after_node], "tail")
    )


def _emit_child(
    self,
    node: etree._Element,
    unit_id: str,
    events: list[Event],
    registry: dict[str, RegistryEntry],
    counter: dict[str, int],
    slot_ids: list[str],
    parent_ref: str,
    inherited_translate: bool,
    *,
    paragraph_boundary: bool = False,
) -> None:
    if not isinstance(node.tag, str):
        self._add_atom(
            node, "comment" if isinstance(node, etree._Comment) else "pi", parent_ref, events, registry, counter
        )
        return
    translated = self._translate_state(node, inherited_translate)
    if self._is_atom(node) or not translated and not self._has_translate_yes(node):
        self._mark_subtree(node, "protected", preserve_attributes=self._is_atom(node) and not self._is_hard(node))
        self._add_atom(node, self._boundary_type(node), parent_ref, events, registry, counter)
        return
    counter["g"] += 1
    ref = f"g{counter['g']}"
    reorder = not paragraph_boundary and self._style_reorder_allowed(node) and self._inline_reorder_allowed(node)
    registry[ref] = RegistryEntry(
        ref_id=ref,
        kind="g",
        source_node_key=self.node_keys[node],
        parent_ref=parent_ref,
        movement="same_parent" if reorder else "locked",
        reorder_allowed=reorder,
        source_text="".join(node.itertext()),
        hints={"element": qname_local_name(node.tag)}
        | ({"source_view_boundary": "paragraph"} if paragraph_boundary else {}),
    )
    events.append(Event(kind="marker", value=f"+{ref}"))
    text_slot = self._slot_for(self.node_keys[node], "text")
    if text_slot is not None:
        self._emit_slot(
            text_slot,
            unit_id,
            events,
            registry,
            counter,
            slot_ids,
            self.node_keys[node],
            force_protected=not translated,
        )
    for child in node:
        self._emit_child(child, unit_id, events, registry, counter, slot_ids, self.node_keys[node], translated)
        tail = self._tail_slot(child)
        if tail is not None:
            self._emit_slot(
                tail,
                unit_id,
                events,
                registry,
                counter,
                slot_ids,
                self.node_keys[node],
                force_protected=not translated,
            )
    events.append(Event(kind="marker", value=f"-{ref}"))


def _emit_slot(
    self,
    slot: _Slot,
    unit_id: str,
    events: list[Event],
    registry: dict[str, RegistryEntry],
    counter: dict[str, int],
    slot_ids: list[str],
    parent_ref: str,
    *,
    force_protected: bool = False,
) -> None:
    if slot.ranges:
        return
    value = slot.source_value
    if not value:
        return
    if force_protected or not value.strip():
        self._emit_protected_text(
            slot,
            0,
            len(value),
            events,
            registry,
            counter,
            parent_ref,
            "protected_text" if force_protected else "whitespace",
            "protected" if force_protected else "whitespace",
        )
        return
    protected = [(start, end, "literal_marker", "protected") for start, end in literal_marker_spans(value)] + [
        (match.start(), match.end(), "existing_chinese", "out_of_scope") for match in _CJK_RE.finditer(value)
    ]
    cursor = 0
    for start, end, boundary_type, owner_kind in sorted(protected):
        if start > cursor:
            self._emit_text_range(slot, cursor, start, unit_id, events)
        self._emit_protected_text(
            slot,
            start,
            end,
            events,
            registry,
            counter,
            parent_ref,
            boundary_type,
            owner_kind,
        )
        cursor = end
    if cursor < len(value):
        self._emit_text_range(slot, cursor, len(value), unit_id, events)
    if any(item.owner_unit_id == unit_id for item in slot.ranges):
        slot_ids.append(slot.slot_id)


def _emit_text_range(self: _Extractor, slot: _Slot, start: int, end: int, unit_id: str, events: list[Event]) -> None:
    value = slot.source_value[start:end]
    owner_kind: OwnerKind = "unit" if value.strip() else "whitespace"
    owner_unit_id = unit_id if owner_kind == "unit" else None
    slot.ranges.append(SlotRange(start=start, end=end, owner_kind=owner_kind, owner_unit_id=owner_unit_id))
    events.append(Event(kind="text", value=value))


def _emit_protected_text(
    self,
    slot: _Slot,
    start: int,
    end: int,
    events: list[Event],
    registry: dict[str, RegistryEntry],
    counter: dict[str, int],
    parent_ref: str,
    boundary_type: str,
    owner_kind: OwnerKind,
) -> None:
    counter["x"] += 1
    ref = f"x{counter['x']}"
    text = slot.source_value[start:end]
    slot.ranges.append(SlotRange(start=start, end=end, owner_kind=owner_kind))
    registry[ref] = RegistryEntry(
        ref_id=ref,
        kind="x",
        source_node_key=slot.node_key,
        parent_ref=parent_ref,
        movement="locked",
        source_text=text,
        hints={"slot_id": slot.slot_id, "start": str(start), "end": str(end)},
        boundary_type=boundary_type,
    )
    events.append(Event(kind="marker", value=f"={ref}"))


def _add_atom(
    self,
    node: etree._Element,
    boundary_type: str,
    parent_ref: str,
    events: list[Event],
    registry: dict[str, RegistryEntry],
    counter: dict[str, int],
) -> None:
    counter["x"] += 1
    ref = f"x{counter['x']}"
    if isinstance(node.tag, str):
        node_key = self.node_keys[node]
        hints = {"element": qname_local_name(node.tag)}
        source_text = "".join(node.itertext())
        if boundary_type == "code" and source_text:
            hints["readonly"] = _bounded(source_text, _HINT_LIMIT)
    else:
        parent = node.getparent()
        node_key = self.node_keys[parent]
        hints = {"child_index": str(list(parent).index(node)), "node_kind": boundary_type}
        source_text = node.text or ""
    hard = boundary_type in {"code", "math", "media", "footnote", "br", "page", "anchor"}
    registry[ref] = RegistryEntry(
        ref_id=ref,
        kind="x",
        source_node_key=node_key,
        parent_ref=parent_ref,
        movement="fixed" if hard else "locked",
        reorder_allowed=False,
        source_text=source_text,
        hints=hints,
        boundary_type=boundary_type,
    )
    events.append(Event(kind="marker", value=f"={ref}"))


def _constrain_hard_boundaries(
    self,
    events: tuple[Event, ...],
    registry: dict[str, RegistryEntry],
    counter: dict[str, int],
) -> tuple[Event, ...]:
    hard_types = {"code", "math", "media", "footnote", "br", "page", "anchor"}

    def visit(index: int, closing_ref: str | None = None) -> tuple[list[Event], int, bool]:
        items: list[tuple[list[Event], str | None]] = []
        contains_hard = False
        while index < len(events):
            event = events[index]
            if event.kind == "text":
                items.append(([event], None))
                index += 1
                continue
            edge, ref = event.value[0], event.value[1:]
            if edge == "-":
                if ref != closing_ref:
                    raise ValueError(f"unexpected source close marker: {event.value}")
                break
            if edge == "+" and ref.startswith("g"):
                inner, index, child_hard = visit(index + 1, ref)
                if child_hard:
                    registry[ref] = registry[ref].model_copy(update={"movement": "fixed", "reorder_allowed": False})
                items.append(([event, *inner, events[index]], None))
                contains_hard |= child_hard
                index += 1
                continue
            entry = registry[ref]
            direct_hard = ref if edge == "=" and entry.boundary_type in hard_types else None
            items.append(([event], direct_hard))
            contains_hard |= direct_hard is not None
            index += 1

        direct = [ref for _, ref in items if ref is not None]
        if not direct:
            return [event for item, _ in items for event in item], index, contains_hard

        parent_ref = registry[direct[0]].parent_ref
        source_node_key = registry[direct[0]].source_node_key
        constrained: list[Event] = []
        fixed_order: list[str] = []
        run: list[Event] = []

        def flush_run() -> None:
            counter["b"] += 1
            boundary = f"b{counter['b']}"
            fixed_order.append(boundary)
            registry[boundary] = RegistryEntry(
                ref_id=boundary,
                kind="b",
                source_node_key=source_node_key,
                parent_ref=parent_ref,
                movement="fixed",
                reorder_allowed=False,
                boundary_type="hard_interval",
            )
            constrained.extend((Event(kind="marker", value=f"+{boundary}"), *run))
            constrained.append(Event(kind="marker", value=f"-{boundary}"))
            run.clear()

        for item, hard_ref in items:
            if hard_ref is None:
                run.extend(item)
                continue
            flush_run()
            constrained.extend(item)
            fixed_order.append(hard_ref)
        flush_run()
        order = tuple(fixed_order)
        for ref in order:
            registry[ref] = registry[ref].model_copy(update={"fixed_order": order})
        return constrained, index, True

    constrained, index, _ = visit(0)
    if index != len(events):
        raise ValueError("unexpected trailing source marker")
    return tuple(constrained)
