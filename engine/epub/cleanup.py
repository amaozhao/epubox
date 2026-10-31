"""Conservative, byte-local Chinese typography cleanup of publication candidates."""

from __future__ import annotations

import re
import zipfile
from collections import Counter
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

import regex
import tinycss2

from engine.core.code import (
    CodeContext,
    _import_target,
    _resolve,
    _selectors,
    _specificity,
    _stylesheet_namespaces,
    _Winner,
    _without_pseudos,
)
from engine.core.markup import element_path
from engine.epub.parsing import XHTML_NAMESPACE, parse_resource
from engine.epub.ranges import CharSpan, RawSpan, index_resource

CLEANUP_POLICY = "zh-inline-v3"
_EMPTY = {"span", "em", "strong"}
_BLOCKS = {
    "body",
    "p",
    "div",
    "li",
    "dt",
    "dd",
    "td",
    "th",
    "caption",
    "figcaption",
    "blockquote",
    *{f"h{i}" for i in range(1, 7)},
}
_PROTECTED = {"pre", "code", "kbd", "samp", "var", "tt", "script", "style", "math", "svg", "textarea"}
_BREAKS = {"br", "hr", "img", "audio", "video", "iframe", "object", "canvas", "input"}
_SPACE = " \t\r\n\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"
_PUNCTUATION = frozenset("，。！？；：、（）【】《》〈〉「」『』“”‘’…—")
_OPENING = frozenset("（【《〈「『“‘")
_CLOSING = frozenset("，。！？；：、）】》〉」』”’…")
_HAN = regex.compile(r"\p{Script=Han}\Z")
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_NESTED = {"media", "supports", "layer", "container", "document", "scope"}


def cleanup_resources(
    resources: Mapping[str, bytes],
    *,
    media_types: Mapping[str, str] | None = None,
    source: Path | None = None,
    node_maps: dict[str, dict[tuple[int, ...], tuple[int, ...]]] | None = None,
) -> tuple[dict[str, bytes], dict[str, int]]:
    """Clean final XHTML bytes without editing source archives, styles, or targets."""
    candidates = dict(resources)
    if source is not None:
        with zipfile.ZipFile(source) as archive:
            for name in archive.namelist():
                if name not in candidates and (
                    _markup(name, media_types)
                    or name.lower().endswith(".css")
                    or (media_types or {}).get(name) == "text/css"
                ):
                    candidates[name] = archive.read(name)
    styles = {}
    for name, raw in candidates.items():
        if name.lower().endswith(".css") or (media_types or {}).get(name) == "text/css":
            _rules, encoding = tinycss2.parse_stylesheet_bytes(raw)
            styles[name] = raw.decode(encoding.codec_info.name)
    result = dict(resources)
    stats = {"removed_empty_tags": 0, "removed_spaces": 0, "changed_documents": 0}
    for name, raw in candidates.items():
        if not _markup(name, media_types):
            continue
        mapping: dict[tuple[int, ...], tuple[int, ...]] | None = {} if node_maps is not None else None
        changed, removed, spaces = _clean(raw, node_map=mapping, resource_path=name, styles=styles)
        if node_maps is not None and mapping is not None:
            node_maps[name] = mapping
        if changed != raw:
            result[name] = changed
            stats["removed_empty_tags"] += removed
            stats["removed_spaces"] += spaces
            stats["changed_documents"] += 1
    return result, stats


def _markup(name: str, media_types: Mapping[str, str] | None) -> bool:
    supplied = (media_types or {}).get(name)
    return (
        supplied in {"application/xhtml+xml", "text/html"}
        if supplied
        else PurePosixPath(name).suffix.lower() in {".xhtml", ".html", ".htm", ".xht"}
    )


def _name(node: Any) -> str:
    return node.tag.rsplit("}", 1)[-1].lower()


class _Safety:
    def __init__(self, code: CodeContext, nodes: list[Any], styles: Mapping[str, str]) -> None:
        self.code = code
        self.styles = styles
        self.empty: set[Any] = set()
        self.whitespace: set[Any] = set()
        self.visual: set[Any] = set()
        self.hidden: set[Any] = set()
        self.values: dict[tuple[Any, str], dict[str, _Winner]] = {}
        self.order = 0
        for node in nodes:
            if _name(node) == "style":
                self.add(
                    tinycss2.parse_stylesheet("".join(node.itertext()), skip_comments=True, skip_whitespace=True),
                    code.resource_path,
                )
            elif _name(node) == "link" and "stylesheet" in node.get("rel", "").lower().split():
                self.load(code.resource_path, node.get("href", ""))
        for node in nodes:
            if node.get("style"):
                self.declarations(
                    {node},
                    tinycss2.parse_declaration_list(node.get("style"), skip_comments=True, skip_whitespace=True),
                    (1_000_000, 0, 0),
                )
        for (node, pseudo), values in self.values.items():
            display = values.get("display")
            mode = (
                display.value
                if display is not None
                else "block"
                if not pseudo and _name(node) in _BLOCKS
                else "inline"
            )
            if not pseudo and mode == "none":
                self.hidden.add(node)
            if pseudo and (
                "content" not in values or values["content"].value in {"none", "normal", "initial", "unset"}
            ):
                continue
            for name, winner in values.items():
                if (
                    not pseudo
                    and name == "white-space"
                    and winner.value not in {"normal", "nowrap", "inherit", "unset", "initial"}
                ):
                    self.whitespace.add(node)
                if _visual(name, winner.value, mode):
                    self.visual.add(node)

    def load(self, base: str, href: str) -> None:
        path = _resolve(base, href)
        if path is not None:
            # CodeContext already validated imports and their local resource paths.
            self.add(tinycss2.parse_stylesheet(self.styles[path], skip_comments=True, skip_whitespace=True), path)

    def add(
        self, rules: Any, path: str, namespaces: Mapping[str, str] | None = None, *, conditional: bool = False
    ) -> None:
        if namespaces is None:
            namespaces = _stylesheet_namespaces(rules, path)
        for rule in rules:
            if rule.type == "error":
                raise ValueError(f"invalid CSS prevents typography cleanup in {path}")
            elif rule.type == "at-rule" and rule.lower_at_keyword == "import":
                href = _import_target(rule.prelude)
                if href is not None:
                    self.load(path, href)
            elif rule.type == "at-rule" and rule.lower_at_keyword in _NESTED and rule.content is not None:
                self.add(
                    tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True),
                    path,
                    namespaces,
                    conditional=True,
                )
            elif rule.type == "qualified-rule":
                declarations = tinycss2.parse_declaration_list(
                    rule.content or [], skip_comments=True, skip_whitespace=True
                )
                for selector in _selectors(rule.prelude):
                    pseudo = re.search(r"::?(before|after|first-letter|first-line)\b", selector, re.IGNORECASE)
                    subject = re.sub(
                        r"::?(?:before|after|first-letter|first-line)\b", "", selector, flags=re.IGNORECASE
                    )
                    try:
                        matches = set(self.code.select(subject, namespaces))
                    except (ValueError, NotImplementedError):
                        # Drop unsupported pseudo-classes to find a conservative superset of subjects.
                        subject = _without_pseudos(subject)
                        try:
                            matches = set(self.code.select(subject, namespaces))
                        except (ValueError, NotImplementedError) as error:
                            raise ValueError(
                                f"cannot determine typography selector safety in {path}: {selector}"
                            ) from error
                    self.declarations(
                        matches,
                        declarations,
                        _specificity(selector),
                        pseudo.group(1).lower() if pseudo is not None else "",
                        conditional=conditional,
                    )

    def declarations(
        self,
        matches: set[Any],
        declarations: Any,
        specificity: tuple[int, int, int],
        pseudo: str = "",
        *,
        conditional: bool = False,
    ) -> None:
        if declarations:
            self.empty.update(matches)
        for declaration in declarations:
            if declaration.type == "error":
                raise ValueError("invalid CSS declaration prevents typography cleanup")
            elif declaration.type == "declaration":
                name = declaration.lower_name
                value = tinycss2.serialize(declaration.value).strip().lower()
                self.order += 1
                candidate = _Winner(value, bool(declaration.important), specificity, self.order)
                if conditional:
                    # Viewport-dependent rules may apply on a reader; keep their potential boundaries.
                    if name == "white-space" and value not in {"normal", "nowrap", "inherit", "unset", "initial"}:
                        self.whitespace.update(matches)
                    if _visual(name, value, "inline-block"):
                        self.visual.update(matches)
                    continue
                for node in matches:
                    values = self.values.setdefault((node, pseudo), {})
                    current = values.get(name)
                    if current is None or (candidate.important, candidate.specificity, candidate.order) >= (
                        current.important,
                        current.specificity,
                        current.order,
                    ):
                        values[name] = candidate


def _visual(name: str, value: str, display: str) -> bool:
    if display in {"none", "contents"}:
        return False
    if name == "content":
        return value not in {"none", "normal", '""', "''"}
    if name in {"background", "background-image", "background-color"}:
        return re.sub(r"\s+", "", value) not in {"none", "transparent", "rgba(0,0,0,0)", "#0000", "#00000000"}
    if name == "counter-increment":
        return value != "none"
    if name == "display":
        return value in {"block", "flex", "grid", "table", "list-item", "flow-root"}
    if name in {
        "width",
        "height",
        "min-width",
        "min-height",
        "inline-size",
        "block-size",
        "min-inline-size",
        "min-block-size",
    }:
        if display == "inline":
            return False
    elif not name.startswith(("margin", "padding", "border")):
        return False
    return value not in {"auto", "none", "normal", "transparent", "initial", "unset"} and not all(
        re.fullmatch(r"[+-]?(?:0+(?:\.0*)?|\.0+)(?:[a-z]+|%)?", part) for part in value.split()
    )


def _url_label(node: Any) -> bool:
    label = "".join(node.itertext()).strip()
    href = unquote(node.get("href", ""))
    return bool(label) and (
        label == href
        or label == href.removeprefix("mailto:")
        or label == re.sub(r"^[a-z][a-z0-9+.-]*://", "", href, flags=re.IGNORECASE)
        or bool(
            re.fullmatch(
                r"(?:(?:https?|ftp|file)://|//|www\.)[^\s/]+(?:/[^\r\n]*)?"
                r"|(?:\.{1,2}/|/)[^\r\n]+|[^\s/@]+\.[a-z]{2,}(?:/[^\r\n]*)?"
                r"|[^\s@]+@[^\s@]+\.[^\s@]+",
                label,
                re.IGNORECASE,
            )
        )
    )


def _protected(node: Any, safety: _Safety) -> bool:
    name = _name(node)
    return bool(
        not node.tag.startswith("{" + XHTML_NAMESPACE + "}")
        or name in _PROTECTED
        or name == "a"
        and node.get("href") is not None
        and _url_label(node)
        or node.get(_XML_SPACE) == "preserve"
        or node in safety.whitespace
    )


def _clean(
    raw: bytes,
    *,
    node_map: dict[tuple[int, ...], tuple[int, ...]] | None = None,
    resource_path: str = "cleanup.xhtml",
    styles: Mapping[str, str] | None = None,
) -> tuple[bytes, int, int]:
    parsed = parse_resource(raw, "application/xhtml+xml")
    if parsed.tree is None or parsed.diagnostics:
        return raw, 0, 0
    index = index_resource(parsed)
    nodes = [node for node in parsed.tree.iter() if isinstance(node.tag, str)]
    paths = {node: element_path(node) for node in nodes}
    try:
        code = CodeContext(parsed.tree, resource_path, styles)
    except ValueError:
        # Cleanup-only callers preserve an uncertain document wholesale; source
        # extraction/publication perform their own strict code-style gate.
        if node_map is not None:
            node_map.update({path: path for path in paths.values()})
        return raw, 0, 0
    safety = _Safety(code, nodes, styles or {})
    bodies = [node for node in nodes if node.tag == "{" + XHTML_NAMESPACE + "}body"]
    if len(bodies) != 1:
        return raw, 0, 0
    body = bodies[0]
    descendants = set(body.iter())
    blocked: set[Any] = set()
    for node in nodes:
        if node.getparent() in blocked or code.is_code(node) or _protected(node, safety):
            blocked.add(node)
    lexical = {(slot.path, slot.field, slot.special_index): slot for slot in index.slots if slot.field != "attribute"}
    flows: dict[tuple[int, ...], list[CharSpan | None]] = {}

    def barrier(block: tuple[int, ...]) -> None:
        flows.setdefault(block, []).append(None)

    def text(path: tuple[int, ...], field: str, block: tuple[int, ...], special: int | None = None) -> None:
        slot = lexical.get((path, field, special))
        if slot is not None:
            flows.setdefault(block, []).extend(slot.chars)

    def walk(node: Any, block: tuple[int, ...]) -> None:
        path = paths[node]
        name = _name(node)
        if node in safety.hidden:
            return
        if node in blocked or name in _BREAKS:
            barrier(block)
            return
        local = path if name in _BLOCKS else block
        if local != block:
            barrier(block)
        visual = node in safety.visual
        if visual:
            barrier(local)
        text(path, "text", local)
        for child_index, child in enumerate(node):
            if isinstance(child.tag, str):
                walk(child, local)
                text(paths[child], "tail", local)
            else:
                text(path, "tail", local, child_index)
        if local != block:
            barrier(block)
        if visual:
            barrier(local)

    walk(body, paths[body])
    spaces: set[RawSpan] = set()
    for flow in flows.values():
        start = 0
        for end in range(len(flow) + 1):
            if end == len(flow) or flow[end] is None:
                _spaces([char for char in flow[start:end] if char is not None], start == 0, end == len(flow), spaces)
                start = end + 1
    # Character spans already bind decoded whitespace (including entities) to its exact original bytes.
    patches = list(spaces)
    removable: set[Any] = set()

    def emptied(path: tuple[int, ...], field: str) -> bool:
        slot = lexical.get((path, field, None))
        return slot is None or all(char.raw in spaces for char in slot.chars)

    for node in reversed(nodes):
        if (
            node not in descendants
            or node in blocked
            or _name(node) not in _EMPTY
            or node.attrib
            or not emptied(paths[node], "text")
            or any(child not in removable or not emptied(paths[child], "tail") for child in node)
            or node in safety.empty
        ):
            continue
        removable.add(node)
        span = index.nodes[paths[node]]
        patches.append(span.starttag)
        if not span.self_closing:
            patches.append(span.endtag)
    if not patches:
        if node_map is not None:
            node_map.update({path: path for path in paths.values()})
        return raw, 0, 0
    result: list[bytes] = []
    cursor = 0
    for patch in sorted(patches):
        if patch.start < cursor:
            raise ValueError("publication cleanup patches overlap")
        result.append(raw[cursor : patch.start])
        cursor = patch.end
    result.append(raw[cursor:])
    changed = b"".join(result)
    after = _verify(parsed.tree, changed, Counter(node.tag for node in removable))
    if node_map is not None:
        retained = [node for node in nodes if node not in removable]
        actual = [node for node in after.iter() if isinstance(node.tag, str)]
        for old, new in zip(retained, actual, strict=True):
            if old.tag != new.tag or dict(old.attrib) != dict(new.attrib):
                raise ValueError("publication cleanup changed retained element order or identity")
            node_map[paths[old]] = element_path(new)
    return changed, len(removable), len(spaces)


def _spaces(chars: list[CharSpan], leading: bool, trailing: bool, removed: set[RawSpan]) -> None:
    position = 0
    while position < len(chars):
        if chars[position].text not in _SPACE:
            position += 1
            continue
        start = position
        while position < len(chars) and chars[position].text in _SPACE:
            position += 1
        left = chars[start - 1].text if start else ""
        right = chars[position].text if position < len(chars) else ""
        if (
            (start == 0 and leading and _chinese(right))
            or (position == len(chars) and trailing and _chinese(left))
            or (_chinese(left) and _chinese(right))
            or (left in _OPENING and bool(right))
            or (right in _CLOSING and bool(left))
        ):
            removed.update(char.raw for char in chars[start:position])


def _chinese(character: str) -> bool:
    return bool(character) and (bool(_HAN.fullmatch(character)) or character in _PUNCTUATION)


def _verify(before: Any, raw: bytes, removed: Counter[str]) -> Any:
    parsed = parse_resource(raw, "application/xhtml+xml")
    if parsed.tree is None or parsed.diagnostics:
        raise ValueError("publication cleanup produced invalid XHTML")
    after = parsed.tree
    inventory = lambda tree: Counter(node.tag for node in tree.iter() if isinstance(node.tag, str))
    attributes = lambda tree: Counter(
        (node.tag, tuple(sorted(node.attrib.items())))
        for node in tree.iter()
        if isinstance(node.tag, str) and node.attrib
    )
    visible = lambda tree: "".join(
        character for text in tree.getroot().itertext() for character in text if character not in _SPACE
    )
    if (
        inventory(before) - removed != inventory(after)
        or attributes(before) != attributes(after)
        or visible(before) != visible(after)
    ):
        raise ValueError("publication cleanup changed protected structure, attributes, or non-whitespace text")
    return after


__all__ = ["CLEANUP_POLICY", "cleanup_resources"]
