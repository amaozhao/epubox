from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from typing import Literal
from xml.parsers import expat

from lxml import etree  # type: ignore[attr-defined]

from engine.core.markup import UnsafeMarkupError, parse_xml_bytes

ResourceKind = Literal["xhtml", "ncx", "opf", "xml", "html"]

XHTML_NAMESPACE = "http://www.w3.org/1999/xhtml"
NCX_NAMESPACE = "http://www.daisy.org/z3986/2005/ncx/"
OPF_NAMESPACE = "http://www.idpf.org/2007/opf"

_DECLARATION = re.compile(r"^\s*<\?xml\s+[^>]*\bencoding\s*=\s*['\"]([^'\"]+)", re.IGNORECASE)
_XML_MEDIA_TYPES = {
    "application/xml",
    "text/xml",
    "application/xhtml+xml",
    "application/x-dtbncx+xml",
    "application/oebps-package+xml",
}


@dataclass(frozen=True)
class ParsedResource:
    raw: bytes
    text: str
    encoding: str
    kind: ResourceKind
    tree: etree._ElementTree | None
    diagnostics: tuple[str, ...]


def parse_resource(data: bytes, media_type: str) -> ParsedResource:
    """Classify one EPUB text resource without normalizing its source bytes."""

    text, encoding = _decode(data)
    normalized_media = media_type.partition(";")[0].strip().lower()
    if normalized_media == "text/html":
        try:
            tree = parse_xml_bytes(data)
        except UnsafeMarkupError:
            if _root_name(text) == f"{XHTML_NAMESPACE}}}html":
                raise
            return _html(data, text, encoding)
        kind, diagnostics = _classify_xml(tree, normalized_media)
        if kind == "xhtml":
            return ParsedResource(data, text, encoding, kind, tree, diagnostics)
        return _html(data, text, encoding)

    xml_required = normalized_media in _XML_MEDIA_TYPES or normalized_media.endswith("+xml")
    if not xml_required and not _looks_like_xml(text):
        raise UnsafeMarkupError(f"unsupported markup media type: {media_type or '(empty)'}")

    tree = parse_xml_bytes(data)
    kind, diagnostics = _classify_xml(tree, normalized_media)
    return ParsedResource(data, text, encoding, kind, None if kind == "html" else tree, diagnostics)


def _html(data: bytes, text: str, encoding: str) -> ParsedResource:
    return ParsedResource(
        raw=data,
        text=text,
        encoding=encoding,
        kind="html",
        tree=None,
        diagnostics=("genuine HTML requires verified source mapping before translation",),
    )


def _root_name(text: str) -> str | None:
    class RootSeen(Exception):
        pass

    parser = expat.ParserCreate(namespace_separator="}")
    root: str | None = None

    def capture(name: str, _attributes: dict[str, str]) -> None:
        nonlocal root
        root = name
        raise RootSeen

    parser.StartElementHandler = capture
    try:
        parser.Parse(text, True)
    except RootSeen:
        pass
    except expat.ExpatError:
        return None
    return root


def _decode(data: bytes) -> tuple[str, str]:
    encoding, bom_size = _encoding(data)
    try:
        text = data[bom_size:].decode(encoding)
    except UnicodeDecodeError as exc:
        raise UnsafeMarkupError(f"resource is not valid {encoding}: {exc}") from exc

    declaration = _DECLARATION.match(text)
    if declaration:
        declared = _canonical_encoding(declaration.group(1))
        if not _compatible_encoding(encoding, declared, bom_size > 0):
            raise UnsafeMarkupError(f"XML declaration encoding {declared} does not match source bytes {encoding}")
    return text, encoding


def _encoding(data: bytes) -> tuple[str, int]:
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8", len(codecs.BOM_UTF8)
    if data.startswith(codecs.BOM_UTF16_LE):
        return "utf-16-le", len(codecs.BOM_UTF16_LE)
    if data.startswith(codecs.BOM_UTF16_BE):
        return "utf-16-be", len(codecs.BOM_UTF16_BE)
    if data.startswith((b"<\x00?\x00x\x00m\x00l\x00", b"<\x00")):
        return "utf-16-le", 0
    if data.startswith((b"\x00<\x00?\x00x\x00m\x00l", b"\x00<")):
        return "utf-16-be", 0
    return "utf-8", 0


def _canonical_encoding(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    aliases = {
        "utf8": "utf-8",
        "utf16": "utf-16",
        "utf16le": "utf-16-le",
        "utf16be": "utf-16-be",
        "utf-16le": "utf-16-le",
        "utf-16be": "utf-16-be",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"utf-8", "utf-16", "utf-16-le", "utf-16-be"}:
        raise UnsafeMarkupError(f"unsupported XML encoding: {value}")
    return normalized


def _compatible_encoding(actual: str, declared: str, has_bom: bool) -> bool:
    if actual == declared:
        return True
    return has_bom and declared == "utf-16" and actual in {"utf-16-le", "utf-16-be"}


def _looks_like_xml(text: str) -> bool:
    source = text.lstrip()
    return source.startswith("<?xml") or (source.startswith("<") and not source.lower().startswith("<!doctype html"))


def _classify_xml(tree: etree._ElementTree, media_type: str) -> tuple[ResourceKind, tuple[str, ...]]:
    root = tree.getroot()
    name = etree.QName(root)
    namespace = name.namespace or ""
    local = name.localname
    if namespace == XHTML_NAMESPACE and local == "html":
        return "xhtml", ()
    if namespace == NCX_NAMESPACE and local == "ncx":
        return "ncx", ()
    if namespace == OPF_NAMESPACE and local == "package":
        return "opf", ()
    if local.lower() == "html" and media_type == "text/html":
        return "html", ("genuine HTML requires verified source mapping before translation",)
    diagnostics = (
        ()
        if media_type not in {"application/xhtml+xml", "application/x-dtbncx+xml", "application/oebps-package+xml"}
        else (f"media type {media_type} does not match the root namespace",)
    )
    return "xml", diagnostics


__all__ = ["ParsedResource", "ResourceKind", "parse_resource"]
