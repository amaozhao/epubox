"""Normalize chapter labels while retaining the source inline structure."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence

from engine.item.inline import parse_projection
from engine.schemas.contracts import DocumentPlan, Unit
from engine.schemas.internal import Event

HEADING_TAGS = frozenset(f"h{level}" for level in range(1, 7))
CHAPTER_LABEL = re.compile(r"^\s*第[0-9]+章(?:(?P<gap>\s+)(?=\S))?")
_SOURCE = re.compile(r"^\s*Chapter[ \t]+(?P<number>[0-9]+)(?![.．][0-9])(?=$|[\s.:：–—-])", re.IGNORECASE)
_TARGET = re.compile(
    r"^\s*(?P<label>第[ \t\u00a0\u3000]*(?P<number>[0-9０-９零〇一二三四五六七八九十百千]+)"
    r"[ \t\u00a0\u3000]*章)"
)
_SEPARATOR_MARKS = (".．。", ":：", ",，、", ";；", "!?！？", "-–—")
_SEPARATOR = re.compile(r"\s*(?P<marks>" + "|".join(f"[{marks}]+" for marks in _SEPARATOR_MARKS) + r")?\s*")


def chapter_render_events(document: DocumentPlan, unit: Unit, target: tuple[Event, ...]) -> tuple[Event, ...]:
    node = document.nodes[unit.node_key]
    tag = node.qname.rsplit("}", 1)[-1]
    heading = tag in HEADING_TAGS and (
        unit.kind == "heading" or unit.kind == f"{tag}_text_region" and unit.region.get("after_node_key") is None
    )
    navigation = (
        unit.region.get("navigation_anchor")
        or unit.kind == "list_item"
        and any(
            binding.get("kind") == "derived_navigation" and binding.get("unit_id") == unit.unit_id
            for binding in document.derived_bindings
        )
        or unit.kind == "navigation"
        and document.resource.media_type == "application/x-dtbncx+xml"
        and any(
            binding.get("kind") == "href_candidate" and binding.get("source_unit_id") == unit.unit_id
            for binding in document.derived_bindings
        )
    )
    return _normalize_chapter(unit.source_projection, target) if heading or navigation else target


def _normalize_chapter(source_projection: str, target: tuple[Event, ...]) -> tuple[Event, ...]:
    """Use the source ordinal and change only the translated chapter prefix."""
    source = parse_projection(source_projection)
    source_text, source_offsets = _first_line(source)
    original = _SOURCE.match(source_text)
    if original is None:
        return target
    target_text, target_offsets = _first_line(target)
    translated = _TARGET.match(target_text)
    if translated is None:
        return target
    separator = _SEPARATOR.match(target_text, translated.end())
    source_separator = _SEPARATOR.match(source_text, original.end("number"))
    assert separator is not None and source_separator is not None  # Either separator may be empty.
    title_start = separator.end()
    source_title = source_text[source_separator.end() :]
    if (
        source_title
        and separator["marks"]
        and any(source_title[0] in marks and separator["marks"][0] in marks for marks in _SEPARATOR_MARKS)
    ):
        # The translation omitted the chapter delimiter and starts with the title's punctuation.
        title_start = separator.start("marks")

    source_start, _ = _location(source_offsets, source, len(source_text) - len(source_text.lstrip()))
    source_number, _ = _location(source_offsets, source, original.end("number") - 1)
    first_index, _ = _location(target_offsets, target, translated.start("label"))
    number_index, _ = _location(target_offsets, target, translated.start("number"))
    last_digit_index, _ = _location(target_offsets, target, translated.end("number") - 1)
    if number_index != last_digit_index:
        # A number split among styled elements has no unambiguous replacement slot.
        return target

    # Close any ordinal-only wrapper before adding 章. Wrappers that already
    # enclose the start of the source label keep the suffix inside them.
    number_wrappers = {
        event.value[1:]
        for event in source[source_start + 1 : source_number]
        if event.kind == "marker" and event.value.startswith("+g")
    }
    suffix_index = number_index
    if target_offsets[number_index] + len(target[number_index].value) <= title_start:
        for index in range(number_index + 1, len(target)):
            event = target[index]
            if event.kind != "marker" or not event.value.startswith("-g") or event.value[1:] not in number_wrappers:
                break
            suffix_index = index + 1

    insertions: dict[int, list[str]] = defaultdict(list)
    insertions[first_index].append("第")
    insertions[number_index].append(original["number"])
    insertions[suffix_index].append("章")
    if title_start < len(target_text):
        title_index, title_offset = _location(target_offsets, target, title_start)
        if title_offset == 0:
            # A title's emphasis or link should start at its first word.
            while (
                title_index > suffix_index
                and target[title_index - 1].kind == "marker"
                and target[title_index - 1].value.startswith("+g")
            ):
                title_index -= 1
        line_break = re.search(r"[\r\n][\r\n \t]*", target_text[translated.end() : title_start])
        insertions[title_index].append(line_break[0] if line_break else " ")

    result: list[Event] = []
    for index, event in enumerate(target):
        replacement = "".join(insertions.get(index, ()))
        if index in target_offsets:
            offset = target_offsets[index]
            start = max(0, translated.start("label") - offset)
            stop = max(0, title_start - offset)
            value = event.value[:start] + replacement + event.value[stop:]
            if value:
                result.append(event.model_copy(update={"value": value}))
        else:
            if replacement:
                result.append(Event(kind="text", value=replacement))
            result.append(event)
    if replacement := "".join(insertions.get(len(target), ())):
        result.append(Event(kind="text", value=replacement))
    return tuple(result)


def _first_line(events: Sequence[Event]) -> tuple[str, dict[int, int]]:
    """Read text through inline groups, stopping at protected or line boundaries."""
    parts: list[str] = []
    offsets: dict[int, int] = {}
    length = 0
    for index, event in enumerate(events):
        if event.kind == "text":
            offsets[index] = length
            parts.append(event.value)
            length += len(event.value)
        elif not (event.value[1:2] == "g" or not parts and event.value.startswith("+b")):
            break
    return "".join(parts), offsets


def _location(offsets: dict[int, int], events: Sequence[Event], position: int) -> tuple[int, int]:
    return next(
        (index, position - start)
        for index, start in offsets.items()
        if start <= position < start + len(events[index].value)
    )
