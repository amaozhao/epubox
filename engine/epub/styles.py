"""Remove publisher fonts and defer typography to the reading system."""

from __future__ import annotations

import codecs
import posixpath
import re
import zipfile
from collections.abc import Iterable, Mapping
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

import tinycss2
from lxml import etree  # type: ignore[attr-defined]

from engine.epub.parsing import ParsedResource, parse_resource
from engine.epub.ranges import RawSpan, ResourceIndex, SlotSpan, index_resource
from engine.epub.validation import (
    FONT_MEDIA_PREFIXES,
    FONT_SUFFIXES,
    PackageInventory,
    _encryption_resource_path,
)

FONT_POLICY = "reader-fonts-v1"

_MARKUP_TYPES = {
    "application/xhtml+xml",
    "text/html",
    "image/svg+xml",
}
_NESTED_RULES = {"container", "document", "keyframes", "layer", "media", "scope", "starting-style", "supports"}
_FONT_SIZE_KEYWORDS = {
    "large",
    "larger",
    "medium",
    "small",
    "smaller",
    "x-large",
    "x-small",
    "xx-large",
    "xx-small",
    "xxx-large",
}
_CSS_WIDE = {"inherit", "initial", "revert", "revert-layer", "unset"}
_SYSTEM_FONTS = {"caption", "icon", "menu", "message-box", "small-caption", "status-bar"}


def use_reader_fonts(
    source: Path,
    inventory: PackageInventory,
    replacements: Mapping[str, bytes],
) -> tuple[dict[str, bytes], frozenset[str]]:
    """Return minimal replacements and ZIP members to remove for reader-controlled fonts.

    The source archive is only read. Existing replacement bytes take precedence, which
    lets this pass run after translation and package upgrade without losing their work.
    EPUBs that deliberately use a private-use/icon font outside ``@font-face`` fail
    closed because deleting that font would destroy meaningful glyph content.
    """
    result = dict(replacements)
    manifest = {item.path: item for item in inventory.manifest}
    removed: set[str] = set()
    for path in inventory.entries:
        item = manifest.get(path)
        if _is_font(path, item.media_type if item is not None else None):
            removed.add(path)
    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())

        def source_bytes(path: str) -> bytes:
            if path in replacements:
                return replacements[path]
            return archive.read(path)

        def record(path: str, data: bytes) -> None:
            original = archive.read(path) if path in names else None
            if original == data:
                result.pop(path, None)
            else:
                result[path] = data

        for item in inventory.manifest:
            if item.path in removed or item.path not in names and item.path not in replacements:
                continue
            if item.media_type == "text/css":
                current = source_bytes(item.path)
                _assert_no_private_use_css_bytes(current, item.path, removed)
                changed = _clean_css_bytes(current, item.path)
                _assert_no_font_urls(changed, item.path, removed)
                record(item.path, changed)
            elif item.media_type in _MARKUP_TYPES:
                current = source_bytes(item.path)
                _assert_no_private_use_markup(current, item.media_type, item.path, removed)
                changed = _clean_markup(current, item.media_type, item.path, removed)
                _assert_no_markup_font_references(changed, item.media_type, item.path, removed)
                record(item.path, changed)

        opf = source_bytes(inventory.opf_path)
        record(inventory.opf_path, _clean_opf(opf, inventory.opf_path, removed))

        encryption_path = "META-INF/encryption.xml"
        if encryption_path in names or encryption_path in replacements:
            cleaned = _clean_encryption(source_bytes(encryption_path), encryption_path, removed)
            if cleaned is None:
                removed.add(encryption_path)
                result.pop(encryption_path, None)
            else:
                record(encryption_path, cleaned)

    for path in removed:
        result.pop(path, None)
    return result, frozenset(removed)


def _is_font(path: str, media_type: str | None) -> bool:
    normalized = (media_type or "").lower()
    return normalized.startswith(FONT_MEDIA_PREFIXES) or PurePosixPath(path).suffix.lower() in FONT_SUFFIXES


def _clean_css_bytes(raw: bytes, resource: str) -> bytes:
    _validate_css_lexical(raw, resource)
    try:
        rules, encoding = tinycss2.parse_stylesheet_bytes(raw, skip_comments=False, skip_whitespace=False)
    except UnicodeDecodeError as error:
        raise ValueError(f"CSS decoding failed for {resource}: {error}") from error
    changed = _clean_rules(rules, resource)
    if not changed:
        return raw
    text = tinycss2.serialize(rules)
    encoded = text.encode(encoding.codec_info.name)
    if raw.startswith(codecs.BOM_UTF8) and not encoded.startswith(codecs.BOM_UTF8):
        encoded = codecs.BOM_UTF8 + encoded
    elif raw.startswith(codecs.BOM_UTF16_LE) and not encoded.startswith(codecs.BOM_UTF16_LE):
        encoded = codecs.BOM_UTF16_LE + encoded
    elif raw.startswith(codecs.BOM_UTF16_BE) and not encoded.startswith(codecs.BOM_UTF16_BE):
        encoded = codecs.BOM_UTF16_BE + encoded
    return encoded


def _clean_css_text(css: str, resource: str) -> str:
    _validate_css_text(css, resource)
    rules = tinycss2.parse_stylesheet(css, skip_comments=False, skip_whitespace=False)
    if _clean_rules(rules, resource):
        return tinycss2.serialize(rules)
    return css


def _clean_style_attribute(style: str, resource: str) -> str:
    _validate_css_text(style, resource, declarations=True)
    declarations = tinycss2.parse_declaration_list(style, skip_comments=False, skip_whitespace=False)
    if _clean_declarations(declarations, resource):
        return tinycss2.serialize(declarations)
    return style


def _clean_rules(rules: list[Any], resource: str) -> bool:
    changed = False
    kept: list[Any] = []
    for rule in rules:
        kind = getattr(rule, "type", None)
        if kind == "error":
            raise ValueError(f"CSS parse failed for {resource}: {getattr(rule, 'message', 'invalid rule')}")
        if kind == "at-rule" and getattr(rule, "lower_at_keyword", "") == "font-face":
            changed = True
            continue
        content = getattr(rule, "content", None)
        if kind == "qualified-rule" and content is not None:
            declarations = tinycss2.parse_declaration_list(content, skip_comments=False, skip_whitespace=False)
            if _clean_declarations(declarations, resource):
                rule.content = tinycss2.parse_component_value_list(tinycss2.serialize(declarations))
                changed = True
        elif kind == "at-rule" and content is not None:
            keyword = getattr(rule, "lower_at_keyword", "")
            if keyword in _NESTED_RULES or keyword.endswith("keyframes"):
                nested = tinycss2.parse_rule_list(content, skip_comments=False, skip_whitespace=False)
                if _clean_rules(nested, resource):
                    rule.content = tinycss2.parse_component_value_list(tinycss2.serialize(nested))
                    changed = True
            else:
                declarations = tinycss2.parse_declaration_list(content, skip_comments=False, skip_whitespace=False)
                if _clean_declarations(declarations, resource):
                    rule.content = tinycss2.parse_component_value_list(tinycss2.serialize(declarations))
                    changed = True
        kept.append(rule)
    if len(kept) != len(rules):
        rules[:] = kept
    return changed


def _clean_declarations(nodes: list[Any], resource: str) -> bool:
    changed = False
    index = 0
    while index < len(nodes):
        declaration = nodes[index]
        kind = getattr(declaration, "type", None)
        if kind == "error":
            raise ValueError(
                f"CSS declaration parse failed for {resource}: {getattr(declaration, 'message', 'invalid declaration')}"
            )
        if kind == "at-rule" and getattr(declaration, "content", None) is not None:
            nested = tinycss2.parse_declaration_list(
                declaration.content, skip_comments=False, skip_whitespace=False
            )
            if _clean_declarations(nested, resource):
                declaration.content = tinycss2.parse_component_value_list(tinycss2.serialize(nested))
                changed = True
            index += 1
            continue
        if kind != "declaration":
            index += 1
            continue
        name = getattr(declaration, "lower_name", "")
        if name == "font-family" and not _reader_family(declaration.value):
            declaration.value = tinycss2.parse_component_value_list("inherit")
            changed = True
        elif name == "font":
            replacement = _font_shorthand(declaration.value, bool(declaration.important))
            if replacement is not None:
                expanded = tinycss2.parse_declaration_list(
                    replacement, skip_comments=False, skip_whitespace=False
                )
                if any(getattr(item, "type", None) == "error" for item in expanded):
                    raise ValueError(f"cannot safely expand CSS font shorthand in {resource}")
                nodes[index : index + 1] = expanded
                changed = True
                index += len(expanded)
                continue
        index += 1
    return changed


def _reader_family(tokens: Iterable[Any]) -> bool:
    value = tinycss2.serialize(tokens).strip().lower()
    return value in _CSS_WIDE or value == "monospace"


def _font_shorthand(tokens: list[Any], important: bool) -> str | None:
    serialized = tinycss2.serialize(tokens).strip().lower()
    if serialized in _CSS_WIDE:
        return None
    if serialized in _SYSTEM_FONTS:
        suffix = " !important" if important else ""
        return f"font:inherit{suffix}"
    if "var(" in serialized:
        raise ValueError(f"CSS variable font shorthand cannot be expanded safely: {tinycss2.serialize(tokens).strip()}")
    significant = [(index, token) for index, token in enumerate(tokens) if token.type not in {"whitespace", "comment"}]
    size_position = next(
        (position for position, (_, token) in enumerate(significant) if _is_font_size(token)),
        None,
    )
    if size_position is None:
        raise ValueError(f"unsupported CSS system font shorthand: {tinycss2.serialize(tokens).strip()}")
    family_position = size_position + 1
    line_height = "normal"
    if family_position < len(significant) and getattr(significant[family_position][1], "value", None) == "/":
        if family_position + 1 >= len(significant):
            raise ValueError(f"CSS font shorthand has no line-height: {tinycss2.serialize(tokens).strip()}")
        line_height = tinycss2.serialize([significant[family_position + 1][1]])
        family_position += 2
    if family_position >= len(significant):
        raise ValueError(f"CSS font shorthand has no family: {tinycss2.serialize(tokens).strip()}")
    family_index = significant[family_position][0]
    if _reader_family(tokens[family_index:]):
        return None
    before_size = [token for _, token in significant[:size_position]]
    style = "normal"
    variant = "normal"
    weight = "normal"
    stretch = "normal"
    for token in before_size:
        value = tinycss2.serialize([token]).strip()
        lower = value.lower()
        if lower in {"italic", "oblique"} or lower.startswith("oblique("):
            style = value
        elif lower in {"small-caps"}:
            variant = value
        elif lower in {"bold", "bolder", "lighter"} or getattr(token, "type", "") == "number":
            weight = value
        elif lower.endswith(("condensed", "expanded")):
            stretch = value
        elif lower != "normal":
            raise ValueError(f"unsupported CSS font shorthand component: {value}")
    size = tinycss2.serialize([significant[size_position][1]])
    suffix = " !important" if important else ""
    return (
        f"font-style:{style}{suffix};font-variant:{variant}{suffix};"
        f"font-weight:{weight}{suffix};font-stretch:{stretch}{suffix};"
        f"font-size:{size}{suffix};line-height:{line_height}{suffix};font-family:inherit{suffix}"
    )


def _is_font_size(token: Any) -> bool:
    kind = getattr(token, "type", "")
    if kind == "dimension":
        return getattr(token, "lower_unit", "") not in {"deg", "grad", "rad", "turn"}
    if kind in {"percentage", "function"}:
        return True
    if kind == "number" and getattr(token, "value", None) == 0:
        return True
    return kind == "ident" and getattr(token, "lower_value", "") in _FONT_SIZE_KEYWORDS


def _clean_markup(raw: bytes, media_type: str, resource: str, removed: set[str]) -> bytes:
    parsed = parse_resource(raw, media_type)
    if parsed.tree is None or parsed.diagnostics:
        raise ValueError(f"XML byte-safe font cleanup is unavailable for {resource}")
    index = index_resource(parsed)
    paths = _element_paths(parsed.tree)
    patches: list[tuple[RawSpan, bytes]] = []
    removed_nodes: set[tuple[int, ...]] = set()
    for element in parsed.tree.getroot().iter():
        if not isinstance(element.tag, str):
            continue
        path = paths[element]
        local = etree.QName(element).localname.lower()
        if local == "link" and _is_font_preload(element, resource, removed):
            patches.append((index.nodes[path].full, b""))
            removed_nodes.add(path)
            continue
        if local == "style":
            slot = _slot(index, path, "text")
            if slot is not None and slot.text:
                changed = _clean_css_text(slot.text, resource)
                if changed != slot.text:
                    patches.append(_style_patch(raw, parsed.encoding, slot, changed))
        for name, value in element.attrib.items():
            attribute = etree.QName(name).localname.lower()
            changed = value
            if attribute == "style":
                changed = _clean_style_attribute(value, resource)
            elif attribute == "font-family":
                if value.strip().lower() not in _CSS_WIDE and value.strip().lower() != "monospace":
                    changed = "inherit"
            elif local == "font" and attribute == "face":
                patches.append((_attribute_span(parsed, index, path, name), b""))
                continue
            if changed != value:
                slot = _slot(index, path, "attribute", name)
                if slot is None:
                    raise ValueError(f"cannot locate XML attribute {name!r} in {resource}")
                patches.append((_slot_span(slot), _xml_attribute(changed).encode(parsed.encoding)))
    return _apply_patches(raw, patches, removed_nodes)


def _clean_opf(raw: bytes, resource: str, removed: set[str]) -> bytes:
    parsed = parse_resource(raw, "application/oebps-package+xml")
    if parsed.tree is None or parsed.diagnostics:
        raise ValueError(f"invalid OPF for font cleanup: {resource}")
    index = index_resource(parsed)
    paths = _element_paths(parsed.tree)
    removed_ids: set[str] = set()
    remove_elements: list[etree._Element] = []
    for element in parsed.tree.getroot().iter():
        if not isinstance(element.tag, str) or etree.QName(element).localname != "item":
            continue
        href = element.get("href")
        if href and _resolve(resource, href) in removed:
            remove_elements.append(element)
            if element.get("id"):
                removed_ids.add(str(element.get("id")))
    patches: list[tuple[RawSpan, bytes]] = [(index.nodes[paths[element]].full, b"") for element in remove_elements]
    removed_nodes = {paths[element] for element in remove_elements}
    for element in parsed.tree.getroot().iter():
        if not isinstance(element.tag, str) or element in remove_elements:
            continue
        path = paths[element]
        refines = element.get("refines", "")
        if refines.startswith("#") and refines[1:] in removed_ids:
            patches.append((index.nodes[path].full, b""))
            removed_nodes.add(path)
            continue
        for attribute in ("fallback", "fallback-style"):
            if element.get(attribute) in removed_ids:
                patches.append((_attribute_span(parsed, index, path, attribute), b""))
    return _apply_patches(raw, patches, removed_nodes)


def _clean_encryption(raw: bytes, resource: str, removed: set[str]) -> bytes | None:
    parsed = parse_resource(raw, "application/xml")
    if parsed.tree is None or parsed.diagnostics:
        raise ValueError(f"invalid encryption metadata: {resource}")
    index = index_resource(parsed)
    paths = _element_paths(parsed.tree)
    encrypted = [
        element
        for element in parsed.tree.getroot().iter()
        if isinstance(element.tag, str) and etree.QName(element).localname == "EncryptedData"
    ]
    affected: list[etree._Element] = []
    for element in encrypted:
        uri = next(
            (
                child.get("URI", "")
                for child in element.iter()
                if isinstance(child.tag, str) and etree.QName(child).localname == "CipherReference"
            ),
            "",
        )
        if uri and _encryption_resource_path(uri) in removed:
            affected.append(element)
    if not affected:
        return raw
    if len(affected) == len(encrypted):
        return None
    patches = [(index.nodes[paths[element]].full, b"") for element in affected]
    return _apply_patches(raw, patches, {paths[element] for element in affected})


def _is_font_preload(element: etree._Element, resource: str, removed: set[str]) -> bool:
    rel = element.get("rel", "").lower().split()
    href = element.get("href", "")
    return "preload" in rel and (element.get("as", "").lower() == "font" or _resolve(resource, href) in removed)


def _assert_no_font_urls(raw: bytes, resource: str, removed: set[str]) -> None:
    rules, _encoding = tinycss2.parse_stylesheet_bytes(raw, skip_comments=False, skip_whitespace=False)
    for value in _css_urls(rules):
        if _resolve(resource, value) in removed:
            raise ValueError(f"font resource remains referenced outside @font-face in {resource}: {value}")


def _assert_no_private_use_css_bytes(raw: bytes, resource: str, removed: set[str]) -> None:
    if not removed:
        return
    rules, _encoding = tinycss2.parse_stylesheet_bytes(raw, skip_comments=False, skip_whitespace=False)
    _assert_no_private_use_css_nodes(rules, resource)


def _assert_no_private_use_css_text(css: str, resource: str) -> None:
    rules = tinycss2.parse_stylesheet(css, skip_comments=False, skip_whitespace=False)
    _assert_no_private_use_css_nodes(rules, resource)


def _assert_no_private_use_css_nodes(nodes: Iterable[Any], resource: str) -> None:
    for node in nodes:
        if getattr(node, "type", None) == "at-rule" and getattr(node, "lower_at_keyword", "") == "font-face":
            continue
        if getattr(node, "type", None) == "string" and _contains_private_use(str(node.value)):
            raise ValueError(f"private-use generated glyph depends on an embedded font in {resource}")
        for attribute in ("prelude", "content", "arguments", "value"):
            children = getattr(node, attribute, None)
            if isinstance(children, list):
                _assert_no_private_use_css_nodes(children, resource)


def _assert_no_private_use_markup(
    raw: bytes,
    media_type: str,
    resource: str,
    removed: set[str],
) -> None:
    if not removed:
        return
    parsed = parse_resource(raw, media_type)
    if parsed.tree is None:
        raise ValueError(f"XML byte-safe private-use validation is unavailable for {resource}")

    def walk(element: etree._Element) -> None:
        local = etree.QName(element).localname.lower() if isinstance(element.tag, str) else ""
        if local == "script":
            return
        if local == "style":
            if element.text:
                _assert_no_private_use_css_text(element.text, resource)
        elif element.text and _contains_private_use(element.text):
            raise ValueError(f"private-use text glyph depends on an embedded font in {resource}")
        for name, value in element.attrib.items():
            if etree.QName(name).localname.lower() == "style":
                declarations = tinycss2.parse_declaration_list(value, skip_comments=False, skip_whitespace=False)
                _assert_no_private_use_css_nodes(declarations, resource)
            elif _contains_private_use(value):
                raise ValueError(f"private-use attribute glyph depends on an embedded font in {resource}")
        for child in element:
            if isinstance(child.tag, str):
                walk(child)
            if child.tail and _contains_private_use(child.tail):
                raise ValueError(f"private-use text glyph depends on an embedded font in {resource}")

    walk(parsed.tree.getroot())


def _contains_private_use(value: str) -> bool:
    return any(
        0xE000 <= codepoint <= 0xF8FF
        or 0xF0000 <= codepoint <= 0xFFFFD
        or 0x100000 <= codepoint <= 0x10FFFD
        for codepoint in map(ord, value)
    )


def _assert_no_markup_font_references(raw: bytes, media_type: str, resource: str, removed: set[str]) -> None:
    parsed = parse_resource(raw, media_type)
    if parsed.tree is None:
        raise ValueError(f"XML byte-safe font reference validation is unavailable for {resource}")
    for element in parsed.tree.getroot().iter():
        if not isinstance(element.tag, str):
            continue
        local = etree.QName(element).localname.lower()
        if local == "style" and element.text:
            rules = tinycss2.parse_stylesheet(element.text, skip_comments=False, skip_whitespace=False)
            for value in _css_urls(rules):
                if _resolve(resource, value) in removed:
                    raise ValueError(f"font resource remains referenced by embedded CSS in {resource}: {value}")
        for name, value in element.attrib.items():
            attribute = etree.QName(name).localname.lower()
            if attribute in {"data", "href", "src"} and _resolve(resource, value) in removed:
                raise ValueError(f"font resource remains referenced by markup in {resource}: {value}")
            if attribute == "style":
                declarations = tinycss2.parse_declaration_list(value, skip_comments=False, skip_whitespace=False)
                for css_url in _css_urls(declarations):
                    if _resolve(resource, css_url) in removed:
                        raise ValueError(
                            f"font resource remains referenced by inline CSS in {resource}: {css_url}"
                        )


def _css_urls(nodes: Iterable[Any]) -> Iterable[str]:
    for node in nodes:
        kind = getattr(node, "type", None)
        if kind == "url":
            yield str(node.value)
        elif kind == "function" and getattr(node, "lower_name", "") == "url":
            value = tinycss2.serialize(node.arguments).strip().strip("\"'")
            if value:
                yield value
        for attribute in ("prelude", "content", "arguments", "value"):
            children = getattr(node, attribute, None)
            if isinstance(children, list):
                yield from _css_urls(children)


def _resolve(resource: str, reference: str) -> str:
    parsed = urlsplit(reference.strip())
    if parsed.scheme or parsed.netloc or not parsed.path:
        return ""
    value = posixpath.normpath(posixpath.join(posixpath.dirname(resource), unquote(parsed.path)))
    return value.removeprefix("./")


def _element_paths(tree: etree._ElementTree) -> dict[etree._Element, tuple[int, ...]]:
    paths: dict[etree._Element, tuple[int, ...]] = {}

    def walk(element: etree._Element, path: tuple[int, ...]) -> None:
        paths[element] = path
        children = [child for child in element if isinstance(child.tag, str)]
        for index, child in enumerate(children):
            walk(child, (*path, index))

    walk(tree.getroot(), ())
    return paths


def _slot(
    index: ResourceIndex,
    path: tuple[int, ...],
    field: str,
    attribute: str | None = None,
) -> SlotSpan | None:
    for slot in index.slots:
        if slot.path == path and slot.field == field and (attribute is None or slot.attribute_name == attribute):
            return slot
    return None


def _slot_span(slot: SlotSpan) -> RawSpan:
    chars = slot.chars
    if not chars:
        raise ValueError("cannot patch an empty XML slot")
    return RawSpan(chars[0].raw.start, chars[-1].raw.end)


def _attribute_span(parsed: ParsedResource, index: ResourceIndex, path: tuple[int, ...], name: str) -> RawSpan:
    starttag = index.nodes[path].starttag
    text = parsed.raw[starttag.start : starttag.end].decode(parsed.encoding)
    match = re.search(rf"\s+{re.escape(name)}\s*=\s*(['\"]).*?\1", text, re.DOTALL)
    if match is None:
        raise ValueError(f"cannot locate XML attribute {name!r}")
    prefix = text[: match.start()].encode(parsed.encoding)
    value = text[match.start() : match.end()].encode(parsed.encoding)
    start = starttag.start + len(prefix)
    return RawSpan(start, start + len(value))


def _xml_attribute(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _xml_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _style_patch(raw: bytes, encoding: str, slot: SlotSpan, value: str) -> tuple[RawSpan, bytes]:
    span = _slot_span(slot)
    opener = "<![CDATA[".encode(encoding)
    closer = "]]>".encode(encoding)
    cdata = raw[max(0, span.start - len(opener)) : span.start] == opener and raw[
        span.end : span.end + len(closer)
    ] == closer
    unique = [char.raw for index, char in enumerate(slot.chars) if not index or char.raw != slot.chars[index - 1].raw]
    if not cdata and any(left.end != right.start for left, right in pairwise(unique)):
        raise ValueError("mixed XML text/CDATA style content cannot be rewritten safely")
    rendered = value if cdata else _xml_text(value)
    return span, rendered.encode(encoding)


def _apply_patches(
    raw: bytes,
    patches: list[tuple[RawSpan, bytes]],
    removed_nodes: set[tuple[int, ...]],
) -> bytes:
    del removed_nodes  # documents why parent removals intentionally supersede child edits
    if not patches:
        return raw
    ordered = sorted(patches, key=lambda item: (item[0].start, item[0].end))
    filtered: list[tuple[RawSpan, bytes]] = []
    for patch in ordered:
        if filtered and patch[0].start < filtered[-1][0].end:
            if patch[0].end <= filtered[-1][0].end and filtered[-1][1] == b"":
                continue
            raise ValueError("overlapping XML font cleanup patches")
        filtered.append(patch)
    result = raw
    for span, value in reversed(filtered):
        result = result[: span.start] + value + result[span.end :]
    return result


def _validate_css_lexical(raw: bytes, resource: str) -> None:
    try:
        _rules, encoding = tinycss2.parse_stylesheet_bytes(raw, skip_comments=False, skip_whitespace=False)
        text = raw.decode(encoding.codec_info.name)
    except (UnicodeDecodeError, LookupError) as error:
        raise ValueError(f"CSS decoding failed for {resource}: {error}") from error
    _validate_css_text(text.lstrip("\ufeff"), resource)


def _validate_css_text(text: str, resource: str, *, declarations: bool = False) -> None:
    stack: list[str] = []
    pairs = {"{": "}", "[": "]", "(": ")"}
    index = 0
    quote: str | None = None
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char == "\\":
            if index + 1 >= len(text):
                raise ValueError(f"CSS ends with an incomplete escape in {resource}")
            index += 2
            continue
        if text.startswith("/*", index):
            close = text.find("*/", index + 2)
            if close < 0:
                raise ValueError(f"CSS contains an unterminated comment in {resource}")
            index = close + 2
            continue
        if char in "\"'":
            quote = char
        elif char in pairs:
            stack.append(pairs[char])
        elif char in "}])" and (not stack or stack.pop() != char):
            raise ValueError(f"CSS has unbalanced syntax in {resource}")
        index += 1
    if quote or stack:
        kind = "declaration" if declarations else "stylesheet"
        raise ValueError(f"CSS {kind} has unterminated syntax in {resource}")


__all__ = ["FONT_POLICY", "use_reader_fonts"]
