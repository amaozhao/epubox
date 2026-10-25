"""Conservative, byte-local Chinese typography cleanup of publication candidates."""

from __future__ import annotations

import re
import zipfile
from collections import Counter
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import regex
import tinycss2

from engine.core.code import CodeContext
from engine.core.markup import element_path
from engine.epub.parsing import XHTML_NAMESPACE, parse_resource
from engine.epub.ranges import CharSpan, RawSpan, index_resource

CLEANUP_POLICY = "zh-inline-v2"
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
_SPACE = " \t\r\n"
_PUNCTUATION = frozenset("，。！？；：、（）【】《》〈〉「」『』“”‘’…—")
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
    safety = _Safety()
    for name, raw in candidates.items():
        if name.lower().endswith(".css") or (media_types or {}).get(name) == "text/css":
            rules, _encoding = tinycss2.parse_stylesheet_bytes(raw, skip_comments=True, skip_whitespace=True)
            safety.add(rules)
        elif _markup(name, media_types):
            parsed = parse_resource(raw, "application/xhtml+xml")
            if parsed.tree is not None:
                for node in parsed.tree.iter():
                    if isinstance(node.tag, str) and _name(node) == "style":
                        safety.add(
                            tinycss2.parse_stylesheet(
                                "".join(node.itertext()), skip_comments=True, skip_whitespace=True
                            )
                        )
    result = dict(resources)
    stats = {"removed_empty_tags": 0, "removed_spaces": 0, "changed_documents": 0}
    for name, raw in candidates.items():
        if not _markup(name, media_types):
            continue
        mapping: dict[tuple[int, ...], tuple[int, ...]] | None = {} if node_maps is not None else None
        changed, removed, spaces = _clean(raw, safety, node_map=mapping, resource_path=name, styles=styles)
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
    def __init__(self) -> None:
        self.empty: list[str] = []
        self.whitespace: list[str] = []
        self.visual: list[str] = []

    def add(self, rules: Any) -> None:
        for rule in rules:
            if rule.type == "error" or rule.type == "at-rule" and rule.lower_at_keyword == "import":
                self.empty.append("*")
                self.whitespace.append("*")
            elif rule.type == "at-rule" and rule.lower_at_keyword in _NESTED and rule.content is not None:
                self.add(tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True))
            elif rule.type == "qualified-rule":
                selectors = tinycss2.serialize(rule.prelude).strip()
                declarations = tinycss2.parse_declaration_list(
                    rule.content or [], skip_comments=True, skip_whitespace=True
                )
                if declarations:
                    self.empty.append(selectors)
                for declaration in declarations:
                    if declaration.type == "error":
                        self.whitespace.append("*")
                    elif declaration.type == "declaration":
                        value = tinycss2.serialize(declaration.value).strip().lower()
                        if declaration.lower_name == "white-space" and value not in {
                            "normal",
                            "nowrap",
                            "inherit",
                            "unset",
                        }:
                            self.whitespace.append(selectors)
                        if declaration.lower_name in {
                            "content",
                            "width",
                            "height",
                            "min-width",
                            "min-height",
                            "background",
                            "background-image",
                            "counter-increment",
                        }:
                            self.visual.append(selectors)

    @staticmethod
    def matches(node: Any, selectors: list[str]) -> bool:
        return any(_possibly_matches(node, part) for selector in selectors for part in selector.split(","))


def _possibly_matches(node: Any, selector: str) -> bool:
    """Overmatch unsupported selectors; uncertainty preserves existing presentation."""
    if any(character in selector for character in "([\\|"):
        return True
    compound = re.split(r"\s+|[>+~]", selector.strip())[-1]
    compound = re.sub(r":{1,2}[a-zA-Z_-][\w-]*", "", compound)
    if not re.fullmatch(r"(?:[a-zA-Z][\w-]*|\*)?(?:[.#][\w-]+)*", compound):
        return True
    tag = re.match(r"^[a-zA-Z][\w-]*", compound)
    if tag and tag.group().lower() != _name(node):
        return False
    if any(node.get("id") != value for value in re.findall(r"#([\w-]+)", compound)):
        return False
    classes = set((node.get("class") or "").split())
    return set(re.findall(r"\.([\w-]+)", compound)) <= classes


def _protected(node: Any, safety: _Safety) -> bool:
    name = _name(node)
    if (
        not node.tag.startswith("{" + XHTML_NAMESPACE + "}")
        or name in _PROTECTED
        or name == "a"
        and node.get("href") is not None
        or node.get(_XML_SPACE) == "preserve"
        or safety.matches(node, safety.whitespace)
    ):
        return True
    declarations = tinycss2.parse_declaration_list(node.get("style", ""), skip_comments=True, skip_whitespace=True)
    return any(
        declaration.type == "declaration"
        and declaration.lower_name == "white-space"
        and tinycss2.serialize(declaration.value).strip().lower() not in {"normal", "nowrap", "inherit", "unset"}
        for declaration in declarations
    )


def _clean(
    raw: bytes,
    safety: _Safety,
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
    bodies = [node for node in nodes if node.tag == "{" + XHTML_NAMESPACE + "}body"]
    if len(bodies) != 1:
        return raw, 0, 0
    body = bodies[0]
    descendants = set(body.iter())
    blocked: set[Any] = set()
    for node in nodes:
        if node.getparent() in blocked or code.is_code(node) or _protected(node, safety):
            blocked.add(node)
    removable: set[Any] = set()
    patches: list[RawSpan] = []
    for node in reversed(nodes):
        if (
            node not in descendants
            or node in blocked
            or _name(node) not in _EMPTY
            or node.attrib
            or node.text
            or any(child not in removable or child.tail for child in node)
            or safety.matches(node, safety.empty)
        ):
            continue
        removable.add(node)
        span = index.nodes[paths[node]]
        patches.append(span.starttag)
        if not span.self_closing:
            patches.append(span.endtag)
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
        if node in blocked or name in _BREAKS:
            barrier(block)
            return
        # Whitespace-only inline nodes may intentionally separate English words.
        if name in _EMPTY and len(node) == 0 and node.text and not node.text.strip():
            barrier(block)
            return
        local = path if name in _BLOCKS else block
        if local != block:
            barrier(block)
        visual = safety.matches(node, safety.visual)
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
                _spaces([char for char in flow[start:end] if char is not None], start == 0, spaces)
                start = end + 1
    literal_spaces = {
        span
        for span in spaces
        if raw[span.start : span.end].decode(index.encoding)
        and all(character in _SPACE for character in raw[span.start : span.end].decode(index.encoding))
    }
    patches.extend(literal_spaces)
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
    return changed, len(removable), len(literal_spaces)


def _spaces(chars: list[CharSpan], leading: bool, removed: set[RawSpan]) -> None:
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
        if (start == 0 and leading and _chinese(right)) or (_chinese(left) and _chinese(right)):
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
