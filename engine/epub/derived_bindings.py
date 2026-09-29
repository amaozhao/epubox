"""Deterministic navigation-to-title bindings over frozen v2.5 source plans."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from engine.item.inline import parse_projection, plain_text
from engine.schemas.contracts import DOCUMENT_FORMAT, DocumentPlan, Unit


def resolve_derived_navigation(documents: Sequence[DocumentPlan]) -> tuple[DocumentPlan, ...]:
    """Bind simple local navigation labels to one exact same-source title Unit."""

    unit_owner = {unit.unit_id: document.document_id for document in documents for unit in document.units}
    if len(unit_owner) != sum(len(document.units) for document in documents):
        raise ValueError("Unit IDs must be unique across source documents")
    if any(document.format != DOCUMENT_FORMAT for document in documents):
        raise TypeError(f"derived navigation requires {DOCUMENT_FORMAT}")

    titles: dict[tuple[str, str, str], list[str]] = {}
    for document in documents:
        units = {unit.unit_id: unit for unit in document.units}
        for candidate in document.derived_bindings:
            if candidate.get("kind") != "title_candidate" or not _trusted_candidate(document, candidate, units):
                continue
            unit = units[str(candidate["source_unit_id"])]
            source_text = str(candidate.get("source_text", ""))
            if not _simple_text(unit, source_text):
                continue
            key = document.resource.path, str(candidate.get("fragment", "")), source_text
            titles.setdefault(key, []).append(unit.unit_id)

    resolved: list[DocumentPlan] = []
    for document in documents:
        units = {unit.unit_id: unit for unit in document.units}
        bindings = [
            dict(candidate) for candidate in document.derived_bindings if candidate.get("kind") != "derived_navigation"
        ]
        for candidate in document.derived_bindings:
            if candidate.get("kind") != "href_candidate" or not _trusted_candidate(document, candidate, units):
                continue
            unit = units[str(candidate["source_unit_id"])]
            source_text = str(candidate.get("source_text", ""))
            if not _simple_text(unit, source_text):
                continue
            target = _local_target(document.resource.path, str(candidate.get("href", "")))
            if target is None:
                continue
            resource, fragment = target
            matches = titles.get((resource, fragment, source_text), ())
            if len(matches) != 1 or unit_owner.get(matches[0]) is None:
                continue
            bindings.append(
                {
                    "kind": "derived_navigation",
                    "unit_id": unit.unit_id,
                    "source_unit_id": matches[0],
                    "source_resource": document.resource.path,
                    "target_resource": resource,
                    "fragment": fragment,
                    "source_text": source_text,
                }
            )
        resolved.append(document.model_copy(update={"derived_bindings": tuple(bindings)}))
    return tuple(resolved)


def _trusted_candidate(document: DocumentPlan, candidate: Mapping[str, object], units: dict[str, Unit]) -> bool:
    unit_id = candidate.get("source_unit_id")
    node_key = candidate.get("source_node_key")
    if (
        not isinstance(unit_id, str)
        or unit_id not in units
        or candidate.get("source_resource") != document.resource.path
        or not isinstance(node_key, str)
        or node_key not in document.nodes
    ):
        return False
    unit = units[unit_id]
    return node_key == unit.node_key or any(entry.source_node_key == node_key for entry in unit.registry.values())


def _simple_text(unit: Unit, expected: str) -> bool:
    if not expected or any(entry.kind == "x" for entry in unit.registry.values()):
        return False
    text_events = [
        event.value
        for event in parse_projection(unit.source_projection)
        if event.kind == "text" and event.value.strip()
    ]
    return len(text_events) == 1 and plain_text(unit.source_projection).strip() == expected


def _local_target(base_resource: str, href: str) -> tuple[str, str] | None:
    parsed = urlsplit(href)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or (not parsed.path and not parsed.fragment)
        or parsed.path.startswith("/")
    ):
        return None
    fragment = unquote(parsed.fragment)
    if not parsed.path:
        return base_resource, fragment
    path = PurePosixPath(base_resource).parent / unquote(parsed.path)
    parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts), fragment


__all__ = ["resolve_derived_navigation"]
