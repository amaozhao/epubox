"""Core source-slot ownership and structural traversal helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Literal

from lxml import etree  # type: ignore[attr-defined]

from engine.core.markup import element_path, parse_xml_safely, qname_local_name
from engine.item import metadata, policy, projection
from engine.item.policy import _BLOCK_TAGS, _CONTAINER_TAGS, _SEMANTIC_TAGS, _TRANSLATABLE_ATTRS
from engine.schemas.internal import DocumentPlan, NodeRecord, ResourceRecord, SlotRange, SourceSlot, Unit

EXTRACTOR_VERSION = "epubox-extractor-7"
ADAPTER_VERSION = "epubox-xml-1"

type SlotField = Literal["text", "tail", "attribute"]
type OwnerKind = Literal["unit", "protected", "whitespace", "out_of_scope"]


@dataclass
class _Slot:
    slot_id: str
    node_key: str
    field: SlotField
    source_value: str
    attribute_name: str | None = None
    ranges: list[SlotRange] = dataclass_field(default_factory=list)


def extract(self: _Extractor) -> DocumentPlan:
    root = self.tree.getroot()
    root_name = qname_local_name(root.tag)
    if root_name == "package":
        self._extract_opf(root)
    elif root_name == "ncx":
        for node in self.elements:
            parent = node.getparent()
            if (
                qname_local_name(node.tag) == "text"
                and parent is not None
                and isinstance(parent.tag, str)
                and qname_local_name(parent.tag) in {"navlabel", "doctitle"}
            ):
                self._make_whole_content_unit(node, True, "navigation")
    else:
        title = next((node for node in self.elements if qname_local_name(node.tag) == "title"), None)
        if title is not None:
            self._make_whole_content_unit(title, True, "head_title")
        body = next((node for node in self.elements if qname_local_name(node.tag) == "body"), root)
        self._walk(body, inherited_translate=True)
    self._extract_attributes()
    self._finalize_unowned_slots()
    self._finalize_units()
    self._collect_derived_bindings()
    source_slots = {slot_id: self._slot_model(slot) for slot_id, slot in self.slots.items()}
    return DocumentPlan(
        document_id=self.document_id,
        source_hash=self.source_hash,
        resource=ResourceRecord(
            path=self.resource_path,
            media_type=self.media_type,
            source_sha256=hashlib.sha256(self.source_markup.encode("utf-8")).hexdigest(),
        ),
        adapter_version=self.adapter_version,
        extractor_version=self.extractor_version,
        source_markup=self.source_markup,
        nodes=self.nodes,
        source_slots=source_slots,
        units=tuple(self.units),
        boundaries=tuple(self.boundaries),
        derived_bindings=tuple(self.derived_bindings),
        preparation_issues=tuple(self.issues),
    )


def _node_key(self: _Extractor, node: etree._Element) -> str:
    path = element_path(node)
    return "n-root" if not path else "n-" + "-".join(map(str, path))


def _collect_slots(self: _Extractor) -> None:
    for node in self.elements:
        key = self.node_keys[node]
        if node.text is not None:
            self._add_slot(key, "text", node.text)
        if node.getparent() is not None and node.tail is not None:
            self._add_slot(key, "tail", node.tail)
        for name, value in node.attrib.items():
            if qname_local_name(name) in _TRANSLATABLE_ATTRS:
                self._add_slot(key, "attribute", value, name)
        for index, child in enumerate(node):
            if not isinstance(child.tag, str) and child.tail is not None:
                slot = self._add_slot(key, "tail", child.tail, token=f"special-{index}")
                self.non_element_tail_slots[child] = slot
                self.boundaries.append(
                    {
                        "kind": "non_element_tail",
                        "slot_id": slot.slot_id,
                        "parent_node_key": key,
                        "child_index": index,
                    }
                )


def _add_slot(
    self,
    node_key: str,
    field_name: SlotField,
    value: str,
    attribute_name: str | None = None,
    *,
    token: str | None = None,
) -> _Slot:
    suffix = token or attribute_name or field_name
    slot_id = f"s-{node_key[2:]}-{hashlib.sha256(suffix.encode()).hexdigest()[:8]}"
    slot = _Slot(slot_id, node_key, field_name, value, attribute_name)
    self.slots[slot_id] = slot
    return slot


def _walk(self: _Extractor, node: etree._Element, inherited_translate: bool, metadata_mode: bool = False) -> None:
    if not isinstance(node.tag, str):
        return
    name = qname_local_name(node.tag)
    translated = self._translate_state(node, inherited_translate)
    if self._is_hard(node) or not translated and not self._has_translate_yes(node):
        self._mark_subtree(node, "protected")
        return
    if metadata_mode and name == "text" and qname_local_name(node.getparent().tag) in {"navlabel", "doctitle"}:
        self._make_whole_content_unit(node, translated, "navigation")
        return
    if name in _SEMANTIC_TAGS:
        direct_blocks = [
            child for child in node if isinstance(child.tag, str) and qname_local_name(child.tag) in _BLOCK_TAGS
        ]
        if not direct_blocks:
            self._make_whole_content_unit(node, translated, self._kind(node))
            return
    grouped = (
        self._extract_direct_regions(node, translated) if name in _CONTAINER_TAGS or name in _SEMANTIC_TAGS else set()
    )
    for child in node:
        if child not in grouped and isinstance(child.tag, str) and qname_local_name(child.tag) in _BLOCK_TAGS:
            self._walk(child, translated, metadata_mode)


def _extract_direct_regions(self: _Extractor, parent: etree._Element, translated: bool) -> set[etree._Element]:
    grouped: set[etree._Element] = set()
    children = list(parent)
    index = 0
    while index < len(children):
        first = index
        while index < len(children) and self._groupable_paragraph(children[index], translated):
            index += 1
        cursor = first
        planned: list[tuple[int, int]] = []
        while cursor + 1 < index:
            longest = 0
            low, high = cursor + 2, index
            while low <= high:
                end = (low + high) // 2
                if self._paragraph_group(parent, children, cursor, end, translated, commit=False):
                    longest = end
                    low = end + 1
                else:
                    high = end - 1
            if longest:
                planned.append((cursor, longest))
                cursor = longest
            else:
                cursor += 1
        for group_index, (start, end) in enumerate(planned):
            handoff_tail = group_index + 1 < len(planned) and planned[group_index + 1][0] == end
            if not self._paragraph_group(
                parent, children, start, end, translated, commit=True, omit_final_tail=handoff_tail
            ):
                raise ValueError("paragraph group changed between preview and commit")
            grouped.update(children[start:end])
        if index == first:
            index += 1
    blocks = [child for child in parent if isinstance(child.tag, str) and qname_local_name(child.tag) in _BLOCK_TAGS]
    before: etree._Element | None = None
    for after in [*blocks, None]:
        self._make_region_unit(parent, before, after, translated, self._kind(parent, virtual=True))
        before = after
    return grouped


def _finalize_unowned_slots(self: _Extractor) -> None:
    for slot in self.slots.values():
        if slot.ranges or not slot.source_value:
            continue
        node = self._element_for_key(slot.node_key)
        if self._inside_hard(node) or not self._translate_state_chain(node):
            kind = "protected"
        elif not slot.source_value.strip():
            kind = "whitespace"
        else:
            kind = "out_of_scope"
        slot.ranges.append(SlotRange(start=0, end=len(slot.source_value), owner_kind=kind))


def _slot_model(self: _Extractor, slot: _Slot) -> SourceSlot:
    owner_kind = slot.ranges[0].owner_kind if len(slot.ranges) == 1 else None
    owner_unit_id = slot.ranges[0].owner_unit_id if len(slot.ranges) == 1 else None
    return SourceSlot(
        slot_id=slot.slot_id,
        node_key=slot.node_key,
        field=slot.field,
        source_value=slot.source_value,
        ranges=tuple(slot.ranges),
        owner_kind=owner_kind,
        owner_unit_id=owner_unit_id,
        attribute_name=slot.attribute_name,
    )


def _slot_for(self: _Extractor, node_key: str, field_name: SlotField) -> _Slot | None:
    special_ids = {slot.slot_id for slot in self.non_element_tail_slots.values()}
    return next(
        (
            slot
            for slot in self.slots.values()
            if slot.node_key == node_key and slot.field == field_name and slot.slot_id not in special_ids
        ),
        None,
    )


def _tail_slot(self: _Extractor, node: etree._Element) -> _Slot | None:
    if isinstance(node.tag, str):
        return self._slot_for(self.node_keys[node], "tail")
    return self.non_element_tail_slots.get(node)


def _element_for_key(self: _Extractor, node_key: str) -> etree._Element:
    return next(node for node, key in self.node_keys.items() if key == node_key)


def _mark_subtree(
    self: _Extractor, node: etree._Element, owner_kind: OwnerKind, *, preserve_attributes: bool = False
) -> None:
    keys = {self.node_keys[item] for item in node.iter() if isinstance(item.tag, str)}
    for slot in self.slots.values():
        is_root_tail = slot.node_key == self.node_keys[node] and slot.field == "tail"
        is_separate_attribute = preserve_attributes and slot.field == "attribute"
        if slot.node_key in keys and not slot.ranges and not is_root_tail and not is_separate_attribute:
            slot.ranges.append(SlotRange(start=0, end=len(slot.source_value), owner_kind=owner_kind))


class _Extractor:
    extractor_version = EXTRACTOR_VERSION
    adapter_version = ADAPTER_VERSION

    _compile_translate_exceptions = policy._compile_translate_exceptions
    _translate_state = policy._translate_state
    _translate_state_chain = policy._translate_state_chain
    _has_translate_yes = policy._has_translate_yes
    _is_hard = policy._is_hard
    _is_short_inline_code = policy._is_short_inline_code
    _inside_hard = policy._inside_hard
    _is_atom = policy._is_atom
    _is_footnote_ref = policy._is_footnote_ref
    _is_pagebreak = policy._is_pagebreak
    _is_empty_anchor = policy._is_empty_anchor
    _boundary_type = policy._boundary_type
    _inline_reorder_allowed = policy._inline_reorder_allowed
    _style_reorder_allowed = policy._style_reorder_allowed
    _kind = policy._kind

    _style_policy = metadata._style_policy
    _style_locks = metadata._style_locks
    _selector_candidates = metadata._selector_candidates
    _selector_parts = metadata._selector_parts
    _matches_stable_compound = metadata._matches_stable_compound
    _extract_attributes = metadata._extract_attributes
    _extract_opf = metadata._extract_opf
    _finalize_units = metadata._finalize_units
    _semantic_config = metadata._semantic_config
    _configured_terms = metadata._configured_terms
    _term_applies = metadata._term_applies
    _unit_source_text = metadata._unit_source_text
    _section_for = metadata._section_for
    _table_context = metadata._table_context
    _footnote_context = metadata._footnote_context
    _collect_derived_bindings = metadata._collect_derived_bindings
    _unit_for_node = metadata._unit_for_node

    _paragraph_group = projection._paragraph_group
    _groupable_paragraph = projection._groupable_paragraph
    _make_whole_content_unit = projection._make_whole_content_unit
    _make_region_unit = projection._make_region_unit
    _region_members = projection._region_members
    _members_have_latin = projection._members_have_latin
    _leading_slot = projection._leading_slot
    _emit_child = projection._emit_child
    _emit_slot = projection._emit_slot
    _emit_text_range = projection._emit_text_range
    _emit_protected_text = projection._emit_protected_text
    _add_atom = projection._add_atom
    _constrain_hard_boundaries = projection._constrain_hard_boundaries

    extract = extract
    _node_key = _node_key
    _collect_slots = _collect_slots
    _add_slot = _add_slot
    _walk = _walk
    _extract_direct_regions = _extract_direct_regions
    _finalize_unowned_slots = _finalize_unowned_slots
    _slot_model = _slot_model
    _slot_for = _slot_for
    _tail_slot = _tail_slot
    _element_for_key = _element_for_key
    _mark_subtree = _mark_subtree

    def __init__(
        self,
        source_markup: str,
        resource_path: str,
        source_hash: str,
        media_type: str,
        config: Mapping[str, Any],
        styles: Any,
        *,
        tree: etree._ElementTree | None = None,
    ) -> None:
        self.source_markup = source_markup
        self.resource_path = resource_path
        self.source_hash = source_hash
        self.media_type = media_type
        self.config = config
        self.tree = tree if tree is not None else parse_xml_safely(source_markup)
        self.elements = [node for node in self.tree.getroot().iter() if isinstance(node.tag, str)]
        self.node_keys = {node: self._node_key(node) for node in self.elements}
        self.nodes = {
            key: NodeRecord(node_key=key, element_path=element_path(node), qname=str(node.tag))
            for node, key in self.node_keys.items()
        }
        self.document_id = policy._stable_id("d", source_hash, resource_path, self.extractor_version)
        self.slots: dict[str, _Slot] = {}
        self.units: list[Unit] = []
        self.issues: list[dict[str, Any]] = []
        self.boundaries: list[dict[str, Any]] = []
        self.derived_bindings: list[dict[str, Any]] = []
        self.non_element_tail_slots: dict[etree._Element, _Slot] = {}
        self.translate_overrides = self._compile_translate_exceptions()
        self._collect_slots()
        self.style_scan = self._style_policy(styles)
        self.style_document_fallback = self.style_scan.policy == metadata.ReorderPolicy.UNKNOWN
        self.style_locked_elements, self.style_locked_parents = self._style_locks(self.style_scan)


def extract_document(
    source_markup: str,
    resource_path: str,
    source_hash: str,
    media_type: str = "application/xhtml+xml",
    config: Mapping[str, Any] | None = None,
    styles: Any = None,
) -> DocumentPlan:
    """Extract one UTF-8 EPUB text resource without normalizing its source string."""

    if not isinstance(source_markup, str):
        raise TypeError("source_markup must be decoded UTF-8 text")
    return _Extractor(source_markup, resource_path, source_hash, media_type, config or {}, styles).extract()


select_primary_title = policy.select_primary_title

__all__ = [
    "ADAPTER_VERSION",
    "EXTRACTOR_VERSION",
    "OwnerKind",
    "SlotField",
    "_Extractor",
    "_Slot",
    "extract_document",
    "select_primary_title",
]
