"""Literal text replacements applied immediately before EPUB packaging."""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from engine.core.markup import find_by_element_path
from engine.epub.parsing import parse_resource
from engine.epub.ranges import CharSpan, RawSpan, index_resource

# Edit this mapping to add or remove publication-time literal replacements.
TEXT_REPLACEMENTS: dict[str, str] = {"您": "你"}

_MARKUP_TYPES = {
    ".htm": "application/xhtml+xml",
    ".html": "application/xhtml+xml",
    ".ncx": "application/x-dtbncx+xml",
    ".opf": "application/oebps-package+xml",
    ".xht": "application/xhtml+xml",
    ".xhtml": "application/xhtml+xml",
    ".xml": "application/xml",
}
_PROTECTED = {"code", "kbd", "math", "pre", "samp", "script", "style", "svg", "tt", "var"}
_ATTRIBUTES = {"alt", "aria-description", "aria-label", "title"}
_READABLE_TYPES = {
    "application/oebps-package+xml",
    "application/x-dtbncx+xml",
    "application/xhtml+xml",
    "application/xml",
    "text/html",
    "text/xml",
}


def replace_resources(
    resources: Mapping[str, bytes],
    replacements: Mapping[str, str] | None = None,
    *,
    media_types: Mapping[str, str] | None = None,
    source: Path | None = None,
) -> dict[str, bytes]:
    """Replace readable XML text while leaving markup and protected subtrees byte-identical."""
    configured = _configured(replacements)
    if not configured:
        return dict(resources)
    result = dict(resources)
    candidates = dict(resources)
    if source is not None:
        with zipfile.ZipFile(source) as archive:
            names = set(archive.namelist())
            for path in media_types or {}:
                if path not in candidates and path in names and _media_type(path, media_types) is not None:
                    candidates[path] = archive.read(path)
    for path, raw in candidates.items():
        media_type = _media_type(path, media_types)
        if media_type is None:
            continue
        changed = _replace_resource(raw, media_type, configured)
        if path in result or changed != raw:
            result[path] = changed
    return result


def _media_type(path: str, media_types: Mapping[str, str] | None) -> str | None:
    media_type = (media_types or {}).get(path) or _MARKUP_TYPES.get(PurePosixPath(path).suffix.lower())
    if media_type is None or media_type.partition(";")[0].strip().lower() not in _READABLE_TYPES:
        return None
    return media_type


def replacement_fingerprint(replacements: Mapping[str, str] | None = None) -> str:
    """Fingerprint the ordered mapping because replacement order can change output."""
    configured = _configured(replacements)
    payload = json.dumps(list(configured.items()), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _configured(replacements: Mapping[str, str] | None) -> Mapping[str, str]:
    configured = TEXT_REPLACEMENTS if replacements is None else replacements
    if any(not isinstance(source, str) or not source for source in configured):
        raise ValueError("publication replacement sources must be non-empty strings")
    if any(not isinstance(target, str) for target in configured.values()):
        raise ValueError("publication replacement targets must be strings")
    return configured


def _replace_resource(raw: bytes, media_type: str, replacements: Mapping[str, str]) -> bytes:
    parsed = parse_resource(raw, media_type)
    if parsed.tree is None or parsed.diagnostics:
        return raw
    index = index_resource(parsed)
    patches: list[tuple[RawSpan, bytes]] = []
    for slot in index.slots:
        if not slot.text or _protected(index, _owner(slot.path, slot.field, slot.special_index)):
            continue
        if slot.field == "attribute" and not _readable_attribute(parsed.tree, slot.path, slot.attribute_name):
            continue
        for text, raw_span in _groups(slot.chars):
            changed = text
            for source, target in replacements.items():
                changed = changed.replace(source, target)
            if changed == text:
                continue
            if slot.field == "attribute":
                changed = _attribute(changed)
            elif _cdata(raw, index, slot.path, raw_span):
                changed = changed.replace("]]>", "]]]]><![CDATA[>")
            else:
                changed = _text(changed)
            patches.append((raw_span, changed.encode(index.encoding)))
    result = raw
    for span, value in sorted(patches, key=lambda patch: patch[0].start, reverse=True):
        result = result[: span.start] + value + result[span.end :]
    return result


def _owner(path: tuple[int, ...], field: str, special_index: int | None) -> tuple[int, ...]:
    return path[:-1] if field == "tail" and special_index is None else path


def _groups(chars: Sequence[CharSpan]) -> list[tuple[str, RawSpan]]:
    groups: list[list[CharSpan]] = []
    for char in chars:
        if not groups or (groups[-1][-1].raw.end != char.raw.start and groups[-1][-1].raw != char.raw):
            groups.append([])
        groups[-1].append(char)
    return [("".join(char.text for char in group), RawSpan(group[0].raw.start, group[-1].raw.end)) for group in groups]


def _readable_attribute(tree: etree._ElementTree, path: tuple[int, ...], attribute_name: str | None) -> bool:
    name = _local(attribute_name or "")
    if name in _ATTRIBUTES:
        return True
    node = find_by_element_path(tree, path)
    description = (node.get("name") or node.get("property") or "").lower()
    return _local(node.tag) == "meta" and name == "content" and description.rsplit(":", 1)[-1] == "description"


def _protected(index, path: tuple[int, ...]) -> bool:
    return any(_local(index.nodes[path[:depth]].qname) in _PROTECTED for depth in range(len(path) + 1))


def _cdata(raw: bytes, index, path: tuple[int, ...], span: RawSpan) -> bool:
    node = index.nodes[path]
    prefix = raw[node.content.start : span.start].decode(index.encoding)
    return prefix.rfind("<![CDATA[") > prefix.rfind("]]>")


def _local(value: str) -> str:
    return value.rsplit("}", 1)[-1].lower()


def _text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attribute(value: str) -> str:
    return (
        _text(value)
        .replace('"', "&quot;")
        .replace("'", "&apos;")
        .replace("\t", "&#9;")
        .replace("\n", "&#10;")
        .replace("\r", "&#13;")
    )


__all__ = ["TEXT_REPLACEMENTS", "replace_resources", "replacement_fingerprint"]
