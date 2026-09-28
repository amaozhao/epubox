"""v2.5 source-plan adapter over the proven v2.3 XML extractor."""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from itertools import pairwise
from typing import Any
from urllib.parse import unquote, urlsplit

from engine.core.markup import find_by_element_path, parse_xml_safely, qname_local_name
from engine.item.extractor import (
    ADAPTER_VERSION,
    EXTRACTOR_VERSION,
)
from engine.item.extractor import (
    extract_document as extract_document_v23,
)
from engine.item.source_views import SourceViewGap, derive_source_views
from engine.schemas.v23 import DocumentPlan as V23DocumentPlan
from engine.schemas.v25 import (
    DocumentPlan as V25DocumentPlan,
)
from engine.schemas.v25 import (
    NodeRecord,
    RegistryEntry,
    ResourceRecord,
    SlotRange,
    SourceSlot,
    Unit,
)

type SourceDocument = V23DocumentPlan | V25DocumentPlan


def extract_document(
    source_markup: str,
    resource_path: str,
    source_hash: str,
    media_type: str = "application/xhtml+xml",
    config: Mapping[str, Any] | None = None,
    styles: Any = None,
) -> V25DocumentPlan:
    """Extract a v2.5 immutable source plan without duplicating XML ownership logic."""

    source = extract_document_v23(source_markup, resource_path, source_hash, media_type, config, styles)
    return _to_v25(source)


def _to_v25(source: V23DocumentPlan) -> V25DocumentPlan:
    derived = derive_source_views(source)
    issues = (*source.preparation_issues, *(_gap_issue(gap) for gap in derived.coverage_gaps))
    boundaries = (*source.boundaries, *_relation_boundaries(source))
    return V25DocumentPlan(
        document_id=source.document_id,
        source_hash=source.source_hash,
        resource=ResourceRecord.model_validate(source.resource.model_dump(mode="python")),
        adapter_version=source.adapter_version,
        extractor_version=source.extractor_version,
        source_markup=source.source_markup,
        nodes={key: NodeRecord.model_validate(node.model_dump(mode="python")) for key, node in source.nodes.items()},
        source_slots={
            key: SourceSlot(
                slot_id=slot.slot_id,
                node_key=slot.node_key,
                field=slot.field,
                source_value=slot.source_value,
                ranges=tuple(SlotRange.model_validate(part.model_dump(mode="python")) for part in slot.ranges),
                attribute_name=slot.attribute_name,
            )
            for key, slot in source.source_slots.items()
        },
        source_views=derived.source_views,
        units=tuple(
            Unit(
                unit_id=unit.unit_id,
                document_id=unit.document_id,
                kind=unit.kind,
                source_projection=unit.source_projection,
                node_key=unit.node_key,
                slot_ids=unit.slot_ids,
                registry={
                    key: RegistryEntry.model_validate(entry.model_dump(mode="python"))
                    for key, entry in unit.registry.items()
                },
                source_view_ids=derived.unit_source_view_ids[unit.unit_id],
                checks=unit.checks,
                region=unit.region,
            )
            for unit in source.units
        ),
        boundaries=boundaries,
        derived_bindings=source.derived_bindings,
        preparation_issues=issues,
    )


def _gap_issue(gap: SourceViewGap) -> dict[str, Any]:
    return {
        "scope": "unit",
        "stage": "source_view",
        "code": "source_view_gap",
        "unit_id": gap.unit_id,
        "reason": gap.reason,
        "source_refs": [ref.model_dump(mode="json") for ref in gap.source_refs],
    }


def validate_source_relations(document: V25DocumentPlan) -> None:
    """Replay frozen table, note, and narrative relations from authoritative source structure."""

    kinds = {"narrative_adjacent", "table_row", "footnote_reference"}
    actual = tuple(boundary for boundary in document.boundaries if boundary.get("kind") in kinds)
    expected = _relation_boundaries(document)
    if actual != expected:
        raise ValueError("persisted source relations do not match the frozen source structure")


def _relation_boundaries(source: SourceDocument) -> tuple[dict[str, Any], ...]:
    recovered = _note_relations(source)
    table_rows, table_units = _table_relations(source)
    note_units = set(recovered["note_units"])
    navigation_paths = tuple(recovered["navigation_paths"])
    excluded = table_units | note_units
    narrative_lanes: dict[tuple[int, ...], list[Any]] = {}
    for unit in source.units:
        path = source.nodes[unit.node_key].element_path
        if (
            unit.unit_id in excluded
            or _independent_unit(unit)
            or any(path[: len(nav_path)] == nav_path for nav_path in navigation_paths)
        ):
            continue
        lane = _nearest_node(path, source, {"section", "article", "aside", "main", "body"})
        narrative_lanes.setdefault(lane or path[:-1], []).append(unit)
    relations: list[dict[str, Any]] = [
        {
            "kind": "narrative_adjacent",
            "unit_ids": [left.unit_id, right.unit_id],
            "relation_edges": [{"from_unit_id": left.unit_id, "to_unit_id": right.unit_id, "kind": "narrative"}],
        }
        for units in narrative_lanes.values()
        for left, right in pairwise(units)
    ]
    relations.extend(
        {"kind": "table_row", "unit_ids": list(unit_ids), "relation_edges": list(edges)}
        for unit_ids, edges in table_rows
    )
    notes_by_body: dict[str, list[str]] = {}
    for body_id, note_ids in recovered["note_references"]:
        for note_id in note_ids:
            if note_id not in notes_by_body.setdefault(body_id, []):
                notes_by_body[body_id].append(note_id)
    relations.extend(
        {
            "kind": "footnote_reference",
            "unit_ids": [body_id, *note_ids],
            "relation_edges": [
                {"from_unit_id": body_id, "to_unit_id": note_id, "kind": "footnote_reference"} for note_id in note_ids
            ],
        }
        for body_id, note_ids in notes_by_body.items()
    )
    unique: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for relation in relations:
        key = str(relation["kind"]), tuple(str(unit_id) for unit_id in relation["unit_ids"])
        if len(key[1]) > 1:
            unique[key] = relation
    return tuple(unique.values())


def _table_relations(
    source: SourceDocument,
) -> tuple[tuple[tuple[tuple[str, ...], tuple[dict[str, str], ...]], ...], set[str]]:
    tree = parse_xml_safely(source.source_markup)
    elements = {node.element_path: find_by_element_path(tree, node.element_path) for node in source.nodes.values()}
    cell_units: dict[tuple[int, ...], list[str]] = {}
    row_cells: dict[tuple[int, ...], list[tuple[int, ...]]] = {}
    table_rows: dict[tuple[int, ...], list[tuple[int, ...]]] = {}
    table_for_row: dict[tuple[int, ...], tuple[int, ...]] = {}
    for unit in source.units:
        if _independent_unit(unit):
            continue
        path = source.nodes[unit.node_key].element_path
        cell = _nearest(path, elements, {"td", "th"})
        row = _nearest(path, elements, {"tr"})
        table = _nearest(path, elements, {"table"})
        if cell is None or row is None or table is None:
            continue
        cell_units.setdefault(cell, []).append(unit.unit_id)
        if cell not in row_cells.setdefault(row, []):
            row_cells[row].append(cell)
        if row not in table_rows.setdefault(table, []):
            table_rows[table].append(row)
        table_for_row[row] = table

    cell_by_id = {
        element.get("id"): path
        for path, element in elements.items()
        if qname_local_name(element.tag) in {"td", "th"} and element.get("id")
    }
    table_headers: dict[tuple[int, ...], list[tuple[int, ...]]] = {}
    for table, rows in table_rows.items():
        for row in rows:
            for cell in row_cells[row]:
                element = elements[cell]
                scope = (element.get("scope") or "").casefold()
                if qname_local_name(element.tag) == "th" and (
                    scope in {"col", "colgroup"} or _nearest(cell, elements, {"thead"}) is not None
                ):
                    table_headers.setdefault(table, []).append(cell)

    relations: list[tuple[tuple[str, ...], tuple[dict[str, str], ...]]] = []
    table_unit_ids: set[str] = set()
    for row, cells in row_cells.items():
        row_units = [unit_id for cell in cells for unit_id in cell_units[cell]]
        table_unit_ids.update(row_units)
        edges = [
            {"from_unit_id": left, "to_unit_id": right, "kind": "table_row"} for left, right in pairwise(row_units)
        ]
        fallback_headers = table_headers.get(table_for_row[row], [])
        for index, cell in enumerate(cells):
            explicit = [
                cell_by_id[header_id]
                for header_id in (elements[cell].get("headers") or "").split()
                if header_id in cell_by_id
            ]
            headers = explicit or ([fallback_headers[index]] if index < len(fallback_headers) else [])
            edges.extend(
                {"from_unit_id": header_unit, "to_unit_id": data_unit, "kind": "table_header"}
                for header in headers
                if header != cell
                for header_unit in cell_units.get(header, [])
                for data_unit in cell_units[cell]
            )
        unique_edges = {(edge["from_unit_id"], edge["to_unit_id"], edge["kind"]): edge for edge in edges}
        relations.append((tuple(row_units), tuple(unique_edges[key] for key in unique_edges)))
    return tuple(relations), table_unit_ids


def _note_relations(source: SourceDocument) -> dict[str, Any]:
    tree = parse_xml_safely(source.source_markup)
    elements = {node.element_path: find_by_element_path(tree, node.element_path) for node in source.nodes.values()}
    singular: set[tuple[int, ...]] = set()
    plural: set[tuple[int, ...]] = set()
    navigation: set[tuple[int, ...]] = set()
    for path, element in elements.items():
        tokens = _semantic_tokens(element)
        if qname_local_name(element.tag) == "nav" or tokens & {"toc", "index", "doc-toc", "doc-index"}:
            navigation.add(path)
        if tokens & {"footnote", "endnote", "rearnote", "doc-footnote", "doc-endnote"}:
            singular.add(path)
        if tokens & {"footnotes", "endnotes", "rearnotes", "doc-footnotes", "doc-endnotes"}:
            plural.add(path)

    note_groups: dict[tuple[int, ...], list[str]] = {}
    unit_notes: dict[str, tuple[int, ...]] = {}
    for unit in source.units:
        if _independent_unit(unit):
            continue
        path = source.nodes[unit.node_key].element_path
        note = _note_container(path, elements, singular, plural)
        if note is not None:
            note_groups.setdefault(note, []).append(unit.unit_id)
            unit_notes[unit.unit_id] = note

    fragments: dict[str, tuple[int, ...]] = {}
    for note_path in note_groups:
        for element in elements[note_path].iter():
            if fragment := element.get("id"):
                fragments[fragment] = note_path

    references: list[tuple[str, tuple[str, ...]]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for binding in source.derived_bindings:
        if binding.get("kind") != "href_candidate":
            continue
        body_id = str(binding.get("source_unit_id", ""))
        if not body_id or body_id in unit_notes:
            continue
        fragment = _same_document_fragment(source.resource.path, str(binding.get("href", "")))
        note_path = fragments.get(fragment)
        note_ids = tuple(note_groups.get(note_path, ())) if note_path is not None else ()
        key = body_id, note_ids
        if note_ids and key not in seen:
            seen.add(key)
            references.append(key)
    return {
        "navigation_paths": tuple(sorted(navigation)),
        "note_units": set(unit_notes),
        "note_references": tuple(references),
    }


def _semantic_tokens(element: Any) -> set[str]:
    epub_type = element.get("{http://www.idpf.org/2007/ops}type") or element.get("epub:type") or ""
    return {token.casefold() for token in f"{epub_type} {element.get('role') or ''}".split()}


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
    return next(
        (
            candidate
            for candidate in reversed(descendants)
            if (element := elements.get(candidate)) is not None and element.get("id")
        ),
        path,
    )


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


def _nearest(
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


def _nearest_node(path: tuple[int, ...], source: SourceDocument, names: set[str]) -> tuple[int, ...] | None:
    nodes = {node.element_path: node for node in source.nodes.values()}
    return next(
        (
            prefix
            for size in range(len(path), -1, -1)
            if (prefix := path[:size]) in nodes and qname_local_name(nodes[prefix].qname) in names
        ),
        None,
    )


def _independent_unit(unit: Any) -> bool:
    return unit.kind.lower() in {
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


__all__ = ["ADAPTER_VERSION", "EXTRACTOR_VERSION", "extract_document", "validate_source_relations"]
