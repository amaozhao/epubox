"""Prove request seams inside a single frozen XML attribute value."""

from __future__ import annotations

import codecs
import hashlib

import regex

from engine.epub.parsing import parse_resource
from engine.epub.ranges import RangeError, index_resource
from engine.item.inline import events_to_projection, parse_projection
from engine.schemas.bridge import AtomicDocument, AtomicItem


def attribute_boundaries(
    inventory: AtomicDocument, item: AtomicItem, raw: bytes | None = None
) -> tuple[tuple[int, int], ...]:
    """Return projection/byte offsets at whitespace, outside entities and markers.

    Derive from frozen source evidence so old inventories need no extraction
    changes. Member validation repeats the same proof before accepting seams.
    """
    if item.kind != "attribute" or len(item.slot_ids) != 1:
        raise ValueError("attribute splitting requires one complete source slot")
    document = inventory.document
    if raw is None:
        encoding = inventory.source_map.encoding
        raw = document.source_markup.encode(encoding)
        missing = inventory.source_map.source_size - len(raw)
        if missing:
            bom = {"utf-8": codecs.BOM_UTF8, "utf-16-le": codecs.BOM_UTF16_LE, "utf-16-be": codecs.BOM_UTF16_BE}.get(
                encoding, b""
            )
            if missing != len(bom) or not bom:
                raise ValueError("attribute source encoding differs from its frozen byte map")
            raw = bom + raw
    if hashlib.sha256(raw).hexdigest() != document.resource.source_sha256:
        raise ValueError("attribute source differs from its frozen resource")
    slot = document.source_slots[item.slot_ids[0]]
    if slot.field != "attribute" or slot.attribute_name != item.region.get("attribute_name"):
        raise ValueError("attribute source slot differs from its parent")
    path = document.nodes[slot.node_key].element_path
    index = index_resource(parse_resource(raw, document.resource.media_type))
    mapped = next(
        part
        for part in index.slots
        if (part.path, part.field, part.attribute_name) == (path, "attribute", slot.attribute_name)
    )
    if mapped.text != slot.source_value or mapped.source_span(0, len(mapped.text)) != item.source_span:
        raise ValueError("attribute source span differs from its frozen slot")
    result: list[tuple[int, int]] = []
    source_cursor = projection_cursor = 0
    events = parse_projection(item.source_projection)
    for event in events:
        projection = events_to_projection((event,))
        if event.kind == "text":
            if slot.source_value[source_cursor : source_cursor + len(event.value)] != event.value:
                raise ValueError("attribute projection differs from its source slot")
            grapheme_ends = {match.end() for match in regex.finditer(r"\X", event.value)}
            for match in regex.finditer(r"\s+", event.value):
                offset = source_cursor + match.end()
                cut = projection_cursor + match.end()
                if match.end() not in grapheme_ends or offset >= len(slot.source_value):
                    continue
                try:
                    byte_offset = mapped.source_span(0, offset).byte_end
                except RangeError:
                    continue
                result.append((cut, byte_offset))
            source_cursor += len(event.value)
        else:
            entry = item.registry.get(event.value[1:])
            if (
                not event.value.startswith("=x")
                or entry is None
                or entry.boundary_type != "literal_marker"
                or entry.hints.get("slot_id") != slot.slot_id
                or entry.hints.get("start") != str(source_cursor)
                or slot.source_value[source_cursor : source_cursor + len(entry.source_text)] != entry.source_text
            ):
                raise ValueError("attribute marker differs from its frozen literal")
            source_cursor += len(entry.source_text)
            if entry.hints.get("end") != str(source_cursor):
                raise ValueError("attribute marker end differs from its frozen literal")
        projection_cursor += len(projection)
    if source_cursor != len(slot.source_value) or projection_cursor != len(item.source_projection):
        raise ValueError("attribute projection does not cover its complete source slot")
    return tuple(result)
