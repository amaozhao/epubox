from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass

from lxml import etree  # type: ignore[attr-defined]

from engine.core.markup import element_path, find_by_element_path, parse_xml_safely, serialize_xml
from engine.item.inline import events_to_projection, parse_projection, plain_text, validate_projection
from engine.schemas.contracts import DOCUMENT_FORMAT, DocumentPlan, RegistryEntry, Unit


@dataclass(frozen=True, slots=True)
class AssembledDocument:
    markup: str
    source_to_target: dict[str, str]


def _node_map(document: DocumentPlan, tree: etree._ElementTree) -> dict[str, etree._Element]:
    return {key: find_by_element_path(tree, record.element_path) for key, record in document.nodes.items()}


def _append_text(parent: etree._Element, text: str) -> None:
    if not text:
        return
    if len(parent):
        parent[-1].tail = (parent[-1].tail or "") + text
    else:
        parent.text = (parent.text or "") + text


def _source_atom(
    entry: RegistryEntry,
    source_nodes: Mapping[str, etree._Element],
    document: DocumentPlan,
) -> etree._Element | str:
    slot_id = entry.hints.get("slot_id")
    if slot_id:
        slot = document.source_slots[slot_id]
        start = int(entry.hints.get("start", "0"))
        end = int(entry.hints.get("end", str(len(slot.source_value))))
        return slot.source_value[start:end]

    source = source_nodes[entry.source_node_key]
    child_index = entry.hints.get("child_index")
    if child_index is not None:
        child = list(source)[int(child_index)]
        clone = deepcopy(child)
    else:
        clone = deepcopy(source)
    clone.tail = None
    return clone


def _render_events(
    events: tuple[object, ...],
    unit: Unit,
    source_nodes: Mapping[str, etree._Element],
    source_keys: Mapping[etree._Element, str],
    target_nodes: dict[str, etree._Element],
    document: DocumentPlan,
) -> etree._Element:
    fragment = etree.Element("epubox-fragment")
    stack = [fragment]
    for event in events:
        kind = getattr(event, "kind", None)
        value = getattr(event, "value", None)
        if not isinstance(kind, str) or not isinstance(value, str):
            raise TypeError("projection event is invalid")
        if kind == "text":
            _append_text(stack[-1], value)
            continue

        marker = value
        edge, ref = marker[0], marker[1:]
        entry = unit.registry[ref]
        if entry.kind == "b":
            continue
        if edge == "+":
            source = source_nodes[entry.source_node_key]
            clone = deepcopy(source)
            for child in list(clone):
                clone.remove(child)
            clone.text = None
            clone.tail = None
            stack[-1].append(clone)
            target_nodes[entry.source_node_key] = clone
            stack.append(clone)
        elif edge == "-":
            if len(stack) == 1:
                raise ValueError(f"unmatched close marker in {unit.unit_id}: {marker}")
            stack.pop()
        elif edge == "=":
            atom = _source_atom(entry, source_nodes, document)
            if isinstance(atom, str):
                _append_text(stack[-1], atom)
            else:
                stack[-1].append(atom)
                if entry.hints.get("child_index") is None:
                    _map_cloned_subtree(source_nodes[entry.source_node_key], atom, source_keys, target_nodes)
        else:
            raise ValueError(f"unknown marker in {unit.unit_id}: {marker}")
    if len(stack) != 1:
        raise ValueError(f"unclosed marker in {unit.unit_id}")
    return fragment


def _map_cloned_subtree(
    source: etree._Element,
    target: etree._Element,
    source_keys: Mapping[etree._Element, str],
    target_nodes: dict[str, etree._Element],
) -> None:
    if source_key := source_keys.get(source):
        target_nodes[source_key] = target
    source_children = [child for child in source if isinstance(child.tag, str)]
    target_children = [child for child in target if isinstance(child.tag, str)]
    for source_child, target_child in zip(source_children, target_children, strict=True):
        _map_cloned_subtree(source_child, target_child, source_keys, target_nodes)


def _apply_content_unit(
    document: DocumentPlan,
    unit: Unit,
    events: tuple[object, ...],
    source_nodes: Mapping[str, etree._Element],
    source_keys: Mapping[etree._Element, str],
    target_nodes: dict[str, etree._Element],
) -> None:
    region = unit.region
    parent_key = str(region["parent_node_key"])
    parent = target_nodes[parent_key]
    after_key = region.get("after_node_key")
    before_key = region.get("before_node_key")
    after = target_nodes[str(after_key)] if after_key else None
    before = target_nodes[str(before_key)] if before_key else None

    children = list(parent)
    start = children.index(after) + 1 if after is not None else 0
    end = children.index(before) if before is not None else len(children)
    for child in children[start:end]:
        parent.remove(child)

    fragment = _render_events(events, unit, source_nodes, source_keys, target_nodes, document)
    if after is None:
        parent.text = fragment.text
    else:
        after.tail = fragment.text
    insert_at = list(parent).index(before) if before is not None else len(parent)
    for child in list(fragment):
        fragment.remove(child)
        parent.insert(insert_at, child)
        insert_at += 1


def assemble_document(
    document: DocumentPlan,
    targets: Mapping[str, str],
    *,
    identity: bool = False,
) -> AssembledDocument:
    """Build XML from the immutable source template and accepted text projections."""

    if document.format != DOCUMENT_FORMAT:
        raise TypeError(f"assembly requires {DOCUMENT_FORMAT}")

    tree = parse_xml_safely(document.source_markup)
    source_tree = parse_xml_safely(document.source_markup)
    target_nodes = _node_map(document, tree)
    source_nodes = _node_map(document, source_tree)
    source_keys = {node: key for key, node in source_nodes.items()}
    attribute_units: list[tuple[Unit, str]] = []

    for unit in document.units:
        target = unit.source_projection if identity else targets[unit.unit_id]
        events = validate_projection(unit, target, unit.registry)
        if unit.region.get("type") == "attribute":
            attribute_units.append((unit, target))
        else:
            _apply_content_unit(document, unit, events, source_nodes, source_keys, target_nodes)

    # Attribute ownership is separate from subtree ownership and must be applied last.
    for unit, target in attribute_units:
        node_key = str(unit.region["node_key"])
        attribute_name = str(unit.region["attribute_name"])
        target_nodes[node_key].set(attribute_name, plain_text(target))

    sidecar: dict[str, str] = {}
    for source_key, node in target_nodes.items():
        if node.getroottree().getroot() is tree.getroot():
            path = element_path(node)
            sidecar[source_key] = "/" + "/".join(map(str, path))
    return AssembledDocument(serialize_xml(tree, source_markup=document.source_markup), sidecar)


def derive_navigation_projection(unit: Unit, accepted_title: str) -> str:
    """Replace one simple navigation label with the current accepted title text."""

    title = plain_text(accepted_title).strip()
    if not title or any(entry.kind == "x" for entry in unit.registry.values()):
        raise ValueError("derived navigation requires one non-empty plain title")
    events = parse_projection(unit.source_projection)
    text_indexes = [index for index, event in enumerate(events) if event.kind == "text" and event.value.strip()]
    if len(text_indexes) != 1:
        raise ValueError("derived navigation requires exactly one text range")
    index = text_indexes[0]
    value = events[index].value
    leading = value[: len(value) - len(value.lstrip())]
    trailing = value[len(value.rstrip()) :]
    replaced = tuple(
        {"kind": event.kind, "value": leading + title + trailing if offset == index else event.value}
        for offset, event in enumerate(events)
    )
    projection = events_to_projection(replaced)
    validate_projection(unit, projection)
    return projection


__all__ = ["AssembledDocument", "assemble_document", "derive_navigation_projection"]
