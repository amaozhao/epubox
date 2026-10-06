"""Whole-element extraction over the existing source-slot/projection codec."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, cast

import regex
from lxml import etree  # type: ignore[attr-defined]

from engine.epub.parsing import NCX_NAMESPACE, OPF_NAMESPACE, XHTML_NAMESPACE, ParsedResource, parse_resource
from engine.epub.ranges import RangeError, ResourceIndex, index_resource
from engine.item.extractor import _to_document_plan
from engine.item.inline import Event, events_to_projection, literal_marker_spans
from engine.item.planner import _false_sentence_boundary
from engine.item.policy import _BLOCK_TAGS, _SEMANTIC_TAGS, _effective_language
from engine.item.structure import OwnerKind, SlotField, _Extractor, _Slot
from engine.schemas.bridge import (
    ATOMIC_TAGS,
    TRANSLATABLE_ATTRIBUTES,
    AtomicDocument,
    AtomicItem,
    AtomicTag,
    ByteSpan,
    source_channel,
)
from engine.schemas.contracts import Unit
from engine.schemas.internal import DocumentPlan as StructuralDocument
from engine.schemas.internal import RegistryEntry, ResourceRecord, SlotRange

EXTRACTOR_VERSION = "epubox-atomic-1"
ADAPTER_VERSION = "epubox-bytes-1"
DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"


def _name(node: etree._Element) -> str:
    return etree.QName(node).localname


def _namespace(node: etree._Element) -> str:
    return etree.QName(node).namespace or ""


class _AtomicExtractor(_Extractor):
    """Specialize source ownership; saved legacy plans keep their old extractor."""

    extractor_version = EXTRACTOR_VERSION
    adapter_version = ADAPTER_VERSION

    def __init__(
        self, parsed: ParsedResource, path: str, source_hash: str, media_type: str, config: Mapping[str, Any]
    ):
        self.parsed = parsed
        if parsed.tree is None:
            raise RangeError("genuine HTML has no proven source mapping")
        super().__init__(parsed.text, path, source_hash, media_type, config, None, tree=parsed.tree)
        self._elements_by_key = {key: node for node, key in self.node_keys.items()}
        special = {slot.slot_id for slot in self.non_element_tail_slots.values()}
        self._slots_by_node: dict[str, list[_Slot]] = {}
        self._ordinary_slots: dict[tuple[str, SlotField], _Slot] = {}
        for slot in self.slots.values():
            self._slots_by_node.setdefault(slot.node_key, []).append(slot)
            if slot.slot_id not in special and slot.field != "attribute":
                self._ordinary_slots[(slot.node_key, slot.field)] = slot
        self._structural: dict[etree._Element, bool] = {}
        for node in reversed(self.elements):
            self._structural[node] = not self._is_atom(node) and (
                _name(node) in ATOMIC_TAGS
                or any(self._structural.get(child, False) for child in node if isinstance(child.tag, str))
            )

    def _collect_slots(self) -> None:
        super()._collect_slots()
        if self.parsed.kind == "xhtml":
            for node in self.elements:
                if self._description(node) and node.get("content") is not None:
                    self._add_slot(self.node_keys[node], "attribute", node.get("content"), "content")

    def extract(self) -> StructuralDocument:
        root = self.tree.getroot()
        if self.parsed.kind == "xhtml":
            for head in root.findall(f"{{{XHTML_NAMESPACE}}}head"):
                for title in head.findall(f"{{{XHTML_NAMESPACE}}}title"):
                    self._make_whole_content_unit(title, self._translate_state_chain(title), "head_title")
            bodies = root.findall(f"{{{XHTML_NAMESPACE}}}body")
            if len(bodies) != 1:
                raise RangeError("XHTML requires one unambiguous body")
            self._walk(bodies[0], self._translate_state_chain(bodies[0]))
        elif self.parsed.kind == "ncx":
            for node in self.elements:
                parent = node.getparent()
                if (
                    node.tag == f"{{{NCX_NAMESPACE}}}text"
                    and parent is not None
                    and parent.tag in {f"{{{NCX_NAMESPACE}}}navLabel", f"{{{NCX_NAMESPACE}}}docTitle"}
                ):
                    self._make_whole_content_unit(node, self._translate_state_chain(node), "navigation")
        elif self.parsed.kind == "opf":
            self._metadata(root)
        self._extract_attributes()
        self._finalize_unowned_slots()
        self._collect_derived_bindings()
        return StructuralDocument(
            document_id=self.document_id,
            source_hash=self.source_hash,
            resource=ResourceRecord(
                path=self.resource_path,
                media_type=self.media_type,
                source_sha256=hashlib.sha256(self.parsed.raw).hexdigest(),
            ),
            adapter_version=self.adapter_version,
            extractor_version=self.extractor_version,
            source_markup=self.source_markup,
            nodes=self.nodes,
            source_slots={key: self._slot_model(slot) for key, slot in self.slots.items()},
            units=tuple(self.units),
            boundaries=tuple(self.boundaries),
            derived_bindings=tuple(self.derived_bindings),
            preparation_issues=tuple(self.issues),
        )

    def _metadata(self, root: etree._Element) -> None:
        metadata = root.find(f"{{{OPF_NAMESPACE}}}metadata")
        if metadata is None:
            return
        titles = list(metadata.findall(f"{{{DC_NAMESPACE}}}title"))
        by_id = {node.get("id"): node for node in titles if node.get("id")}
        main_refs = {
            (node.get("refines") or "").removeprefix("#")
            for node in metadata.findall(f"{{{OPF_NAMESPACE}}}meta")
            if node.get("property") == "title-type" and (node.text or "").strip() == "main"
        }
        main = {by_id[ref] for ref in main_refs if ref in by_id}
        if main_refs:
            primary = next(iter(main)) if len(main) == 1 else None
        else:
            primary = titles[0] if len(titles) == 1 else self._language_match(metadata, titles)
        if primary is not None:
            self._make_whole_content_unit(primary, self._translate_state_chain(primary), "metadata_title")
        elif titles:
            self.issues.append({"code": "ambiguous_primary_title", "stage": "extract", "severity": "warning"})
        descriptions = [node for node in metadata.findall(f"{{{DC_NAMESPACE}}}description") if len(node) == 0]
        description = descriptions[0] if len(descriptions) == 1 else self._language_match(metadata, descriptions)
        if description is not None:
            self._make_whole_content_unit(
                description, self._translate_state_chain(description), "metadata_description"
            )

    @staticmethod
    def _language_match(metadata, candidates):
        languages = {(node.text or "").strip().casefold() for node in metadata.findall(f"{{{DC_NAMESPACE}}}language")}
        if len(languages) != 1:
            return None
        matches = [node for node in candidates if _effective_language(node) == next(iter(languages))]
        return matches[0] if len(matches) == 1 else None

    def _walk(self, node: etree._Element, inherited_translate: bool, metadata_mode: bool = False) -> None:
        translated = self._translate_state(node, inherited_translate)
        if self._is_hard(node) or not translated and not self._has_translate_yes(node):
            self._mark_subtree(node, "protected")
            return
        if self._is_atom(node):
            self._mark_subtree(node, "protected", preserve_attributes=_name(node) == "img")
            return
        name = _name(node)
        if name in ATOMIC_TAGS:
            self._make_whole_content_unit(node, translated, name)
            return
        cuts = [
            child
            for child in node
            if isinstance(child.tag, str)
            and not self._is_hard(child)
            and (_name(child) in _BLOCK_TAGS or self._structural.get(child, False))
        ]
        if not cuts and name in _SEMANTIC_TAGS:
            self._make_whole_content_unit(node, translated, self._kind(node))
            return
        after = None
        for before in [*cuts, None]:
            self._make_region_unit(node, after, before, translated, self._kind(node, virtual=True))
            if before is not None:
                self._walk(before, translated)
            after = before

    def _make_region_unit(self, parent, after_node, before_node, translated, kind, **kwargs) -> bool:
        members = self._region_members(parent, after_node, before_node)
        relevant = [parent, *[node for member in members for node in member.iter() if isinstance(node.tag, str)]]
        saved = {
            slot.slot_id: list(slot.ranges)
            for node in relevant
            for slot in self._slots_by_node.get(self.node_keys[node], ())
        }
        lead = self._leading_slot(parent, after_node)
        if lead is not None:
            saved[lead.slot_id] = list(lead.ranges)
        for member in members:
            tail = self._tail_slot(member)
            if tail is not None:
                saved[tail.slot_id] = list(tail.ranges)
        made = super()._make_region_unit(parent, after_node, before_node, translated, kind, **kwargs)
        if not made:
            for slot_id, ranges in saved.items():
                self.slots[slot_id].ranges = ranges
        return made

    def _emit_child(self, node, *args, paragraph_boundary=False, **kwargs) -> None:
        boundary = isinstance(node.tag, str) and _name(node) in _BLOCK_TAGS
        super()._emit_child(node, *args, paragraph_boundary=paragraph_boundary or boundary, **kwargs)

    def _extract_attributes(self) -> None:
        for slot in self.slots.values():
            if slot.field == "attribute" and not slot.ranges and not self._attribute_allowed(slot):
                slot.ranges.append(SlotRange(start=0, end=len(slot.source_value), owner_kind="out_of_scope"))
        super()._extract_attributes()
        self._protect_attribute_markers()

    def _protect_attribute_markers(self) -> None:
        rewritten: list[Any] = []
        for unit in self.units:
            if unit.kind != "attribute":
                rewritten.append(unit)
                continue
            slot = self.slots[unit.slot_ids[0]]
            spans = literal_marker_spans(slot.source_value)
            if not spans:
                rewritten.append(unit)
                continue
            events: list[Event] = []
            registry: dict[str, RegistryEntry] = {}
            cursor = 0
            for index, (start, end) in enumerate(spans, 1):
                if start > cursor:
                    events.append(Event(kind="text", value=slot.source_value[cursor:start]))
                ref = f"x{index}"
                registry[ref] = RegistryEntry(
                    ref_id=ref,
                    kind="x",
                    source_node_key=slot.node_key,
                    parent_ref=slot.node_key,
                    movement="locked",
                    source_text=slot.source_value[start:end],
                    hints={"slot_id": slot.slot_id, "start": str(start), "end": str(end)},
                    boundary_type="literal_marker",
                )
                events.append(Event(kind="marker", value=f"={ref}"))
                cursor = end
            if cursor < len(slot.source_value):
                events.append(Event(kind="text", value=slot.source_value[cursor:]))
            if not any(event.kind == "text" and event.value.strip() for event in events):
                slot.ranges[:] = [SlotRange(start=0, end=len(slot.source_value), owner_kind="protected")]
                continue
            rewritten.append(
                unit.model_copy(
                    update={
                        "source_projection": events_to_projection(events),
                        "registry": registry,
                    }
                )
            )
        self.units[:] = rewritten

    def _attribute_allowed(self, slot: _Slot) -> bool:
        node = self._element_for_key(slot.node_key)
        if self.parsed.kind != "xhtml" or _namespace(node) != XHTML_NAMESPACE:
            return False
        if (self._is_atom(node) and _name(node) != "img") or any(
            self._is_atom(ancestor) for ancestor in node.iterancestors()
        ):
            return False
        if slot.attribute_name == "content":
            return self._description(node)
        return slot.attribute_name in TRANSLATABLE_ATTRIBUTES and any(
            ancestor.tag == f"{{{XHTML_NAMESPACE}}}body" for ancestor in [node, *node.iterancestors()]
        )

    @staticmethod
    def _description(node: etree._Element) -> bool:
        return (
            node.tag == f"{{{XHTML_NAMESPACE}}}meta"
            and node.get("name") == "description"
            and node.getparent() is not None
            and node.getparent().tag == f"{{{XHTML_NAMESPACE}}}head"
        )

    def _is_hard(self, node: etree._Element) -> bool:
        if not isinstance(node.tag, str):
            return False
        if self.parsed.kind == "xhtml" and (_namespace(node) != XHTML_NAMESPACE or _name(node) != _name(node).lower()):
            return True
        return super()._is_hard(node)

    def _slot_for(self, node_key: str, field_name: SlotField) -> _Slot | None:
        return self._ordinary_slots.get((node_key, field_name))

    def _element_for_key(self, node_key: str) -> etree._Element:
        return self._elements_by_key[node_key]

    def _mark_subtree(self, node: etree._Element, owner_kind: OwnerKind, *, preserve_attributes: bool = False) -> None:
        for member in node.iter():
            if not isinstance(member.tag, str):
                continue
            for slot in self._slots_by_node.get(self.node_keys[member], ()):
                if (
                    slot.ranges
                    or member is node
                    and slot.field == "tail"
                    or preserve_attributes
                    and slot.field == "attribute"
                ):
                    continue
                slot.ranges.append(SlotRange(start=0, end=len(slot.source_value), owner_kind=owner_kind))


def _span(unit: Unit, document, index: ResourceIndex, lexical) -> ByteSpan:
    path = document.nodes[unit.node_key].element_path
    if unit.region.get("type") == "attribute":
        slot = document.source_slots[unit.slot_ids[0]]
        return lexical[(path, "attribute", slot.attribute_name, None)].source_span(0, len(slot.source_value))
    node = index.nodes[path]
    if unit.kind in ATOMIC_TAGS or unit.kind in {"heading", "table_cell", "list_item", "figure_caption"}:
        raw = node.full
    else:
        start, end = node.content.start, node.content.end
        if after := unit.region.get("after_node_key"):
            start = index.nodes[document.nodes[after].element_path].full.end
        if before := unit.region.get("before_node_key"):
            end = index.nodes[document.nodes[before].element_path].full.start
        return ByteSpan(byte_start=start, byte_end=end)
    return ByteSpan(byte_start=raw.start, byte_end=raw.end)


def _safe_boundaries(unit: Unit, document, lexical) -> list[dict[str, Any]]:
    """Expose source-only virtual cuts; T08/T14 decide which fit the budget."""
    if not unit.kind.endswith("_text_region"):
        return []
    parent = unit.region["parent_node_key"]
    parent_path = document.nodes[parent].element_path
    special = {
        boundary["slot_id"]: boundary["child_index"]
        for boundary in document.boundaries
        if boundary.get("kind") == "non_element_tail"
    }
    result: list[dict[str, Any]] = []
    for slot_id in unit.slot_ids:
        slot = document.source_slots[slot_id]
        path = document.nodes[slot.node_key].element_path
        domain = path if slot.field != "tail" or slot_id in special else path[:-1]
        if domain != parent_path:
            continue  # A paired inline range must not be cut through its markers.
        mapped = lexical[(path, slot.field, slot.attribute_name, special.get(slot_id))]
        grapheme_ends = {match.end() for match in regex.finditer(r"\X", slot.source_value)}
        for match in regex.finditer(r"(?<=[.!?。！？])\s+|\n[\t ]*\n", slot.source_value):
            offset = match.end()
            if offset not in grapheme_ends or offset >= len(slot.source_value):
                continue
            if "\n\n" not in match.group() and _false_sentence_boundary(
                slot.source_value[max(0, match.start() - 32) : match.start()], slot.source_value[offset : offset + 1]
            ):
                continue
            owned = next(
                (
                    part
                    for part in slot.ranges
                    if part.owner_unit_id == unit.unit_id and part.start < offset < part.end
                ),
                None,
            )
            if owned is None:
                continue
            try:
                position = mapped.source_span(owned.start, offset).byte_end
            except RangeError:
                continue
            result.append({"slot_id": slot_id, "char_offset": offset, "byte_offset": position})
    return result


def extract_resource(
    data: bytes,
    resource_path: str,
    source_hash: str,
    media_type: str = "application/xhtml+xml",
    config: Mapping[str, Any] | None = None,
) -> AtomicDocument:
    """Prepare raw-byte atom inventory; T15 owns production pipeline integration."""
    parsed = parse_resource(data, media_type)
    if parsed.diagnostics:
        prefix = "genuine HTML: " if parsed.kind == "html" else ""
        raise RangeError(prefix + "; ".join(parsed.diagnostics))
    index = index_resource(parsed)
    draft = _AtomicExtractor(parsed, resource_path, source_hash, media_type, config or {})
    document = _to_document_plan(draft.extract())
    lexical = {(slot.path, slot.field, slot.attribute_name, slot.special_index): slot for slot in index.slots}
    spans = {unit.unit_id: _span(unit, document, index, lexical) for unit in document.units}
    ordered = sorted(document.units, key=lambda unit: (spans[unit.unit_id].byte_start, unit.unit_id))
    units: list[Unit] = []
    owner: Unit | None = None
    for unit in ordered:
        span = spans[unit.unit_id]
        boundaries = _safe_boundaries(unit, document, lexical)
        if boundaries:
            unit = unit.model_copy(update={"region": unit.region | {"safe_boundaries": boundaries}})
        if unit.kind == "attribute":
            if owner is not None and span.byte_end <= spans[owner.unit_id].byte_end:
                unit = unit.model_copy(update={"region": unit.region | {"patch_owner_id": owner.unit_id}})
        else:
            owner = unit
        units.append(unit)
    document = document.model_copy(update={"units": tuple(units)})
    items = tuple(
        AtomicItem(
            **unit.model_dump(),
            item_id=unit.unit_id,
            ordinal=ordinal,
            channel=source_channel(unit),
            atomic_tag=cast(AtomicTag, unit.kind) if unit.kind in ATOMIC_TAGS else None,
            source_span=spans[unit.unit_id],
        )
        for ordinal, unit in enumerate(document.units)
    )
    return AtomicDocument(document=document, source_map=index.bind_document(document), items=items)
