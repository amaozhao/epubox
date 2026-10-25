"""Deterministic repairs for package and NCX metadata."""

from __future__ import annotations

import html
from collections.abc import Mapping

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from engine.core.markup import element_path, qname_local_name
from engine.epub.parsing import parse_resource
from engine.epub.ranges import ResourceIndex, index_resource

_DC = "http://purl.org/dc/elements/1.1/"


def required_dc(metadata: etree._Element, name: str, fallback: str) -> None:
    elements = [item for item in metadata if qname_local_name(item.tag) == name]
    item = next((value for value in elements if (value.text or "").strip()), None)
    if item is None:
        item = elements[0] if elements else etree.SubElement(metadata, f"{{{_DC}}}{name}")
        item.text = fallback


def recover_empty_agents(metadata: etree._Element) -> None:
    """Recover an empty agent only from its unique, explicit file-as refinement."""

    refinements: dict[str, set[str]] = {}
    for item in metadata:
        if qname_local_name(item.tag) != "meta" or item.get("property") != "file-as":
            continue
        refines = item.get("refines", "")
        content = (item.text or "").strip()
        if refines.startswith("#") and len(refines) > 1 and content:
            refinements.setdefault(refines[1:], set()).add(content)
    for agent in metadata:
        if qname_local_name(agent.tag) not in {"creator", "contributor"} or (agent.text or "").strip():
            continue
        values = refinements.get(agent.get("id", ""), set())
        if len(values) == 1:
            agent.text = next(iter(values))


def unique_identifier(package: etree._Element, metadata: etree._Element) -> str | None:
    identifier_id = package.get("unique-identifier", "").strip()
    return next(
        (
            content
            for item in metadata
            if qname_local_name(item.tag) == "identifier"
            and item.get("id") == identifier_id
            and (content := (item.text or "").strip())
        ),
        None,
    )


def sync_ncx_uid(data: bytes, identifier: str | None) -> bytes:
    """Patch existing NCX uid attributes without serializing its navigation content."""

    if not identifier:
        return data
    parsed = parse_resource(data, "application/x-dtbncx+xml")
    if parsed.tree is None or parsed.diagnostics:
        return data
    changes = {
        (element_path(item), "content"): identifier
        for item in parsed.tree.getroot().iter()
        if isinstance(item.tag, str)
        and qname_local_name(item.tag) == "meta"
        and item.get("name") == "dtb:uid"
        and item.get("content") != identifier
    }
    return _patch_attributes(data, index_resource(parsed), changes) if changes else data


def _patch_attributes(
    data: bytes, indexed: ResourceIndex, changes: Mapping[tuple[tuple[int, ...], str], str]
) -> bytes:
    patches = [
        (slot.source_span(0, len(slot.text)), changes[slot.path, slot.attribute_name])
        for slot in indexed.slots
        if slot.field == "attribute"
        and slot.attribute_name is not None
        and (slot.path, slot.attribute_name) in changes
        and slot.text
    ]
    for span, value in sorted(patches, key=lambda patch: patch[0].byte_start, reverse=True):
        replacement = html.escape(value, quote=True).encode(indexed.encoding)
        data = data[: span.byte_start] + replacement + data[span.byte_end :]
    return data


__all__ = ["recover_empty_agents", "required_dc", "sync_ncx_uid", "unique_identifier"]
