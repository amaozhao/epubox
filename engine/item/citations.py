"""Source-grounded citation-title handling for translation script checks."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from engine.item.inline import parse_projection
from engine.schemas.contracts import RegistryEntry
from engine.schemas.internal import Event

_TITLE_ELEMENTS = frozenset({"cite", "em", "i"})
_CITATION_CUE = re.compile(
    r"\b(?:paper|article|study|book|report)\s+(?:(?:called|entitled|named|titled)\s+)?$",
    re.IGNORECASE,
)


def script_paragraphs_without_retained_titles(
    source_projection: str,
    target_projection: str,
    registry: Mapping[str, RegistryEntry],
) -> tuple[str, ...]:
    """Return semantic target paragraphs with exact cited English titles omitted."""
    source_events = parse_projection(source_projection)
    target_events = parse_projection(target_projection)
    source_ranges = _range_texts(source_events)
    target_ranges = _range_texts(target_events)
    retained: set[str] = set()

    for index, event in enumerate(source_events):
        if event.kind != "marker" or not event.value.startswith("+g"):
            continue
        ref = event.value[1:]
        entry = registry.get(ref)
        previous = source_events[index - 1] if index else None
        if (
            entry is not None
            and entry.hints.get("element", "").lower() in _TITLE_ELEMENTS
            and bool(entry.source_text)
            and source_ranges.get(ref) == entry.source_text == target_ranges.get(ref)
            and previous is not None
            and previous.kind == "text"
            and _CITATION_CUE.search(previous.value)
        ):
            retained.add(ref)

    paragraphs: list[str] = []
    parts: list[str] = []
    stack: list[str] = []

    def flush() -> None:
        if text := "".join(parts):
            paragraphs.append(text)
        parts.clear()

    for event in target_events:
        if event.kind == "text":
            if not retained.intersection(stack):
                parts.append(event.value)
        elif event.value.startswith("+"):
            ref = event.value[1:]
            if registry[ref].hints.get("source_view_boundary") == "paragraph":
                flush()
            stack.append(ref)
        elif event.value.startswith("-"):
            ref = event.value[1:]
            if registry[ref].hints.get("source_view_boundary") == "paragraph":
                flush()
            stack.pop()
    flush()
    return tuple(paragraphs)


def _range_texts(events: Sequence[Event]) -> dict[str, str]:
    parts: dict[str, list[str]] = {}
    stack: list[str] = []
    for event in events:
        if event.kind == "text":
            for ref in stack:
                parts[ref].append(event.value)
        elif event.value.startswith("+"):
            ref = event.value[1:]
            stack.append(ref)
            parts[ref] = []
        elif event.value.startswith("-"):
            stack.pop()
    return {ref: "".join(values) for ref, values in parts.items()}
