"""Source document contracts."""

from __future__ import annotations

from itertools import pairwise
from typing import Literal

from pydantic import Field, model_validator

from engine.schemas.base import DOCUMENT_FORMAT, FrozenModel, JsonValue, canonical_hash


class ResourceRecord(FrozenModel):
    path: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    source_sha256: str = Field(min_length=1)
    properties: tuple[str, ...] = ()
    linear: bool | None = None


class NodeRecord(FrozenModel):
    node_key: str = Field(min_length=1)
    element_path: tuple[int, ...]
    kind: str = "element"
    qname: str = Field(min_length=1)


class SlotRange(FrozenModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    owner_kind: Literal["unit", "protected", "whitespace", "out_of_scope"]
    owner_unit_id: str | None = None

    @model_validator(mode="after")
    def validate_range(self) -> SlotRange:
        if self.end < self.start:
            raise ValueError("slot range end must not precede start")
        if (self.owner_kind == "unit") != (self.owner_unit_id is not None):
            raise ValueError("only unit-owned ranges must name owner_unit_id")
        return self


class SourceSlot(FrozenModel):
    slot_id: str = Field(min_length=1)
    node_key: str = Field(min_length=1)
    field: Literal["text", "tail", "attribute"]
    source_value: str
    ranges: tuple[SlotRange, ...] = ()
    attribute_name: str | None = None

    @model_validator(mode="after")
    def validate_ownership(self) -> SourceSlot:
        if (self.field == "attribute") != (self.attribute_name is not None):
            raise ValueError("attribute slots require attribute_name, other slots forbid it")
        if not self.source_value:
            if any(interval.start != 0 or interval.end != 0 for interval in self.ranges):
                raise ValueError("empty source slots can only contain empty ownership ranges")
            return self
        position = 0
        for interval in self.ranges:
            if interval.start != position or interval.end <= interval.start:
                raise ValueError("source slot ranges must be continuous and non-empty")
            if interval.end > len(self.source_value):
                raise ValueError("source slot range exceeds source_value")
            position = interval.end
        if position != len(self.source_value):
            raise ValueError("source slot ranges must cover source_value")
        return self


class RegistryEntry(FrozenModel):
    ref_id: str = Field(min_length=1)
    kind: Literal["g", "x", "b"]
    source_node_key: str = Field(min_length=1)
    parent_ref: str = Field(min_length=1)
    movement: Literal["same_parent", "locked", "fixed"] = "locked"
    reorder_allowed: bool = False
    fixed_order: tuple[str, ...] = ()
    source_text: str = ""
    hints: dict[str, str] = Field(default_factory=dict)
    boundary_type: str | None = None


class SourceRef(FrozenModel):
    slot_id: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> SourceRef:
        if self.end <= self.start:
            raise ValueError("source ref end must follow start")
        return self


def source_view_hash_payload(
    *, unit_id: str, document_id: str, text: str, source_refs: tuple[SourceRef, ...], view_kind: str
) -> dict[str, JsonValue]:
    return {
        "unit_id": unit_id,
        "document_id": document_id,
        "text": text,
        "source_refs": [ref.model_dump(mode="json") for ref in source_refs],
        "view_kind": view_kind,
    }


class SourceTextView(FrozenModel):
    view_id: str = Field(min_length=1)
    unit_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    text: str
    view_hash: str = Field(min_length=1)
    source_refs: tuple[SourceRef, ...]
    view_kind: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_hash(self) -> SourceTextView:
        if self.text and not self.source_refs:
            raise ValueError("non-empty SourceTextView requires source_refs")
        expected = canonical_hash(
            source_view_hash_payload(
                unit_id=self.unit_id,
                document_id=self.document_id,
                text=self.text,
                source_refs=self.source_refs,
                view_kind=self.view_kind,
            )
        )
        if self.view_hash != expected:
            raise ValueError("SourceTextView view_hash does not match its canonical source payload")
        return self


class Unit(FrozenModel):
    unit_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    source_projection: str
    node_key: str = Field(min_length=1)
    slot_ids: tuple[str, ...]
    registry: dict[str, RegistryEntry] = Field(default_factory=dict)
    source_view_ids: tuple[str, ...] = ()
    context_view_ids: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    region: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_ids(self) -> Unit:
        if len(set(self.slot_ids)) != len(self.slot_ids):
            raise ValueError("Unit slot_ids must be unique")
        if len(set(self.source_view_ids)) != len(self.source_view_ids):
            raise ValueError("Unit source_view_ids must be unique")
        if len(set(self.context_view_ids)) != len(self.context_view_ids):
            raise ValueError("Unit context_view_ids must be unique")
        if any(key != entry.ref_id for key, entry in self.registry.items()):
            raise ValueError("registry keys must match ref_id")
        return self


class DocumentPlan(FrozenModel):
    format: Literal["epubox-document-3"] = DOCUMENT_FORMAT
    document_id: str = Field(min_length=1)
    source_hash: str = Field(min_length=1)
    resource: ResourceRecord
    adapter_version: str = Field(min_length=1)
    extractor_version: str = Field(min_length=1)
    source_markup: str
    nodes: dict[str, NodeRecord] = Field(default_factory=dict)
    source_slots: dict[str, SourceSlot] = Field(default_factory=dict)
    source_views: dict[str, SourceTextView] = Field(default_factory=dict)
    units: tuple[Unit, ...] = ()
    boundaries: tuple[dict[str, JsonValue], ...] = ()
    derived_bindings: tuple[dict[str, JsonValue], ...] = ()
    preparation_issues: tuple[dict[str, JsonValue], ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> DocumentPlan:
        if any(key != node.node_key for key, node in self.nodes.items()):
            raise ValueError("node map keys must match node_key")
        if any(key != slot.slot_id for key, slot in self.source_slots.items()):
            raise ValueError("source slot map keys must match slot_id")
        special_slots: set[str] = set()
        special_positions: set[tuple[str, int]] = set()
        for boundary in self.boundaries:
            if boundary.get("kind") != "non_element_tail":
                continue
            slot_id = boundary.get("slot_id")
            parent = boundary.get("parent_node_key")
            child_index = boundary.get("child_index")
            slot = self.source_slots.get(slot_id) if isinstance(slot_id, str) else None
            if (
                not isinstance(slot_id, str)
                or slot is None
                or slot.field != "tail"
                or not isinstance(parent, str)
                or slot.node_key != parent
                or type(child_index) is not int
                or child_index < 0
                or slot_id in special_slots
                or (parent, child_index) in special_positions
            ):
                raise ValueError("invalid or duplicate non-element tail boundary")
            special_slots.add(slot_id)
            special_positions.add((parent, child_index))
        ordinary_slots = [slot for slot in self.source_slots.values() if slot.slot_id not in special_slots]
        physical_slots = {(slot.node_key, slot.field, slot.attribute_name) for slot in ordinary_slots}
        if len(physical_slots) != len(ordinary_slots):
            raise ValueError("duplicate physical source slot")
        if any(key != view.view_id for key, view in self.source_views.items()):
            raise ValueError("source view map keys must match view_id")
        if len({node.element_path for node in self.nodes.values()}) != len(self.nodes):
            raise ValueError("node element paths must be unique")
        last_child_by_parent: dict[tuple[int, ...], int] = {}
        for node in self.nodes.values():
            if node.element_path:
                parent_path = node.element_path[:-1]
                last_child_by_parent[parent_path] = max(
                    last_child_by_parent.get(parent_path, -1), node.element_path[-1]
                )
        if len({unit.unit_id for unit in self.units}) != len(self.units):
            raise ValueError("unit_id must be unique within a document")
        units_by_id = {unit.unit_id: unit for unit in self.units}
        unit_ids = set(units_by_id)
        primary_view_owners: dict[str, list[str]] = {view_id: [] for view_id in self.source_views}
        slot_owners: dict[str, set[str]] = {}
        for slot_id, slot in self.source_slots.items():
            if slot.node_key not in self.nodes:
                raise ValueError("source slot references an unknown node")
            owners = {part.owner_unit_id for part in slot.ranges if part.owner_unit_id is not None}
            if not owners.issubset(unit_ids):
                raise ValueError("source slot references an unknown Unit")
            slot_owners[slot_id] = owners
        for view in self.source_views.values():
            if view.document_id != self.document_id or view.unit_id not in unit_ids:
                raise ValueError("source view references an unknown document or unit")
            slot_order = {slot_id: index for index, slot_id in enumerate(units_by_id[view.unit_id].slot_ids)}
            previous_slot = -1
            previous_end = -1
            for ref in view.source_refs:
                slot = self.source_slots.get(ref.slot_id)
                if slot is None or ref.end > len(slot.source_value):
                    raise ValueError("source view references an unknown or out-of-bounds source slot")
                position = slot_order.get(ref.slot_id)
                if (
                    position is None
                    or position < previous_slot
                    or (position == previous_slot and ref.start < previous_end)
                ):
                    raise ValueError("source view refs must follow owned source order without overlap")
                if not any(
                    interval.owner_kind == "unit"
                    and interval.owner_unit_id == view.unit_id
                    and interval.start <= ref.start
                    and ref.end <= interval.end
                    for interval in slot.ranges
                ):
                    raise ValueError("source view ref is not wholly owned by its Unit")
                previous_slot, previous_end = position, ref.end
        view_ids = set(self.source_views)
        for unit in self.units:
            if unit.document_id != self.document_id or unit.node_key not in self.nodes:
                raise ValueError("Unit references an unknown document or node")
            if not set(unit.slot_ids).issubset(self.source_slots):
                raise ValueError("Unit references an unknown source slot")
            members = unit.region.get("member_node_keys")
            if unit.kind == "paragraph_group" or members is not None:
                parent_key = unit.region.get("parent_node_key")
                parent = self.nodes.get(parent_key) if isinstance(parent_key, str) else None
                if (
                    unit.kind != "paragraph_group"
                    or not isinstance(members, (list, tuple))
                    or len(members) < 2
                    or any(not isinstance(key, str) for key in members)
                    or len(set(members)) != len(members)
                    or parent is None
                    or unit.node_key != members[0]
                ):
                    raise ValueError("invalid paragraph group identity")
                paths = [self.nodes[key].element_path for key in members if isinstance(key, str) and key in self.nodes]
                if (
                    len(paths) != len(members)
                    or any(path[:-1] != parent.element_path for path in paths)
                    or any(right[-1] != left[-1] + 1 for left, right in pairwise(paths))
                    or any(
                        self.nodes[key].qname.rsplit("}", 1)[-1] != "p"
                        for key in members
                        if isinstance(key, str) and key in self.nodes
                    )
                ):
                    raise ValueError("paragraph group members must be adjacent sibling paragraphs")
                after_key, before_key = unit.region.get("after_node_key"), unit.region.get("before_node_key")
                after = self.nodes.get(after_key) if isinstance(after_key, str) else None
                before = self.nodes.get(before_key) if isinstance(before_key, str) else None
                last_child = last_child_by_parent.get(parent.element_path, -1)
                if (
                    (after is None and paths[0][-1] != 0)
                    or (after is not None and after.element_path != (*parent.element_path, paths[0][-1] - 1))
                    or (after is None and after_key is not None)
                    or (before is not None and before.element_path != (*parent.element_path, paths[-1][-1] + 1))
                    or (before is None and before_key is not None)
                    or (before is None and paths[-1][-1] != last_child)
                ):
                    raise ValueError("paragraph group boundaries do not match its members")
                wrappers = [
                    entry for entry in unit.registry.values() if entry.kind == "g" and entry.parent_ref == parent_key
                ]
                if (
                    any(not entry.ref_id[1:].isdigit() for entry in wrappers)
                    or [entry.source_node_key for entry in sorted(wrappers, key=lambda entry: int(entry.ref_id[1:]))]
                    != list(members)
                    or any(
                        entry.movement not in {"locked", "fixed"}
                        or entry.reorder_allowed
                        or entry.hints.get("source_view_boundary") != "paragraph"
                        for entry in wrappers
                    )
                ):
                    raise ValueError("paragraph group wrappers must preserve paragraph order")
                for slot_id in unit.slot_ids:
                    slot_path = self.nodes[self.source_slots[slot_id].node_key].element_path
                    if not any(slot_path[: len(path)] == path for path in paths):
                        raise ValueError("paragraph group owns a source slot outside its member paragraphs")
            owned_slots = {slot_id for slot_id, owners in slot_owners.items() if unit.unit_id in owners}
            if set(unit.slot_ids) != owned_slots:
                raise ValueError("Unit/source slot ownership must be bidirectional")
            if not set(unit.source_view_ids + unit.context_view_ids).issubset(view_ids):
                raise ValueError("Unit references an unknown source view")
            if any(self.source_views[view_id].unit_id != unit.unit_id for view_id in unit.source_view_ids):
                raise ValueError("Unit primary source views must belong to that Unit")
            for view_id in unit.source_view_ids:
                primary_view_owners[view_id].append(unit.unit_id)
            for entry in unit.registry.values():
                if entry.source_node_key not in self.nodes:
                    raise ValueError("registry entry references an unknown source node")
                if entry.parent_ref not in self.nodes and entry.parent_ref not in unit.registry:
                    raise ValueError("registry entry references an unknown parent")
        if any(owners != [self.source_views[view_id].unit_id] for view_id, owners in primary_view_owners.items()):
            raise ValueError("every source view must be registered exactly once by its owner Unit")
        return self
