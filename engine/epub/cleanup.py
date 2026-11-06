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
from engine.epub.chapter import CHAPTER_LABEL, HEADING_TAGS
from engine.epub.parsing import XHTML_NAMESPACE, parse_resource
from engine.epub.ranges import CharSpan, RawSpan, index_resource

CLEANUP_POLICY = "zh-inline-v5"
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
    *HEADING_TAGS,
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
        for node in nodes:
            if self.direction(node) is None or self.writing_mode(node) != "horizontal-tb":
                # Bidirectional auto-detection and vertical inline axes require a full layout engine.
                self.whitespace.add(node)
        for (node, pseudo), values in self.values.items():
            direction = self.direction(node)
            writing_mode = self.writing_mode(node)
            if direction is None or writing_mode != "horizontal-tb":
                self.whitespace.add(node)
                continue
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
                if _visual(name, winner.value, mode, block=not pseudo and _name(node) in _BLOCKS):
                    self.visual.add(node)
            if not (not pseudo and _name(node) in _BLOCKS) and _border_box_advances(values, direction):
                self.visual.add(node)

    def direction(self, node: Any) -> str | None:
        values = self.values.get((node, ""), {})
        winner = values.get("direction")
        if winner is not None:
            if winner.value in {"ltr", "rtl"}:
                return winner.value
            if winner.value == "initial":
                return "ltr"
            if winner.value not in {"inherit", "unset"}:
                return None
        attribute = (node.get("dir") or "").lower()
        if attribute in {"ltr", "rtl"}:
            return attribute
        if attribute:
            return None
        parent = node.getparent()
        return self.direction(parent) if parent is not None else "ltr"

    def writing_mode(self, node: Any) -> str | None:
        values = self.values.get((node, ""), {})
        winner = values.get("writing-mode")
        if winner is not None:
            if winner.value == "horizontal-tb":
                return winner.value
            if winner.value == "initial":
                return "horizontal-tb"
            if winner.value not in {"inherit", "unset"}:
                return None
        parent = node.getparent()
        return self.writing_mode(parent) if parent is not None else "horizontal-tb"

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
                    subject, pseudo = _pseudo_subject(selector)
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
                        pseudo,
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
        conditional_values: dict[str, _Winner] = {}
        for declaration in declarations:
            if declaration.type == "error":
                raise ValueError("invalid CSS declaration prevents typography cleanup")
            elif declaration.type == "declaration":
                name = declaration.lower_name
                value = tinycss2.serialize(declaration.value).strip().lower()
                self.order += 1
                candidate = _Winner(value, bool(declaration.important), specificity, self.order)
                assignments = [(name, value), *_border_assignments(name, value)]
                if conditional:
                    # Viewport-dependent rules may apply on a reader; keep their potential boundaries.
                    if name == "white-space" and value not in {"normal", "nowrap", "inherit", "unset", "initial"}:
                        self.whitespace.update(matches)
                    if name in {"direction", "writing-mode"} and value not in {
                        "initial",
                        "inherit",
                        "ltr",
                        "horizontal-tb",
                    }:
                        self.whitespace.update(matches)
                    self.visual.update(
                        node
                        for node in matches
                        if _visual(name, value, "inline-block", block=not pseudo and _name(node) in _BLOCKS)
                    )
                    for assigned_name, assigned_value in assignments:
                        assigned = _Winner(
                            assigned_value,
                            candidate.important,
                            candidate.specificity,
                            candidate.order,
                        )
                        current = conditional_values.get(assigned_name)
                        if current is None or (assigned.important, assigned.specificity, assigned.order) >= (
                            current.important,
                            current.specificity,
                            current.order,
                        ):
                            conditional_values[assigned_name] = assigned
                    continue
                for node in matches:
                    values = self.values.setdefault((node, pseudo), {})
                    for assigned_name, assigned_value in assignments:
                        assigned = _Winner(
                            assigned_value,
                            candidate.important,
                            candidate.specificity,
                            candidate.order,
                        )
                        current = values.get(assigned_name)
                        if current is None or (assigned.important, assigned.specificity, assigned.order) >= (
                            current.important,
                            current.specificity,
                            current.order,
                        ):
                            values[assigned_name] = assigned
        if conditional:
            self.visual.update(
                node
                for node in matches
                if (pseudo or _name(node) not in _BLOCKS)
                and (direction := self.direction(node)) is not None
                and self.writing_mode(node) == "horizontal-tb"
                and _border_box_advances(conditional_values, direction)
            )


def _pseudo_subject(selector: str) -> tuple[str, str]:
    """Return the originating-element selector and generated pseudo name."""
    tokens = tinycss2.parse_component_value_list(selector)
    names = {"before", "after", "first-letter", "first-line"}
    for index, token in enumerate(tokens):
        if token.type != "literal" or getattr(token, "value", None) != ":":
            continue
        name_index = index + 1
        if (
            name_index < len(tokens)
            and tokens[name_index].type == "literal"
            and getattr(tokens[name_index], "value", None) == ":"
        ):
            name_index += 1
        if name_index >= len(tokens) or tokens[name_index].type != "ident":
            continue
        name = str(getattr(tokens[name_index], "lower_value", ""))
        if name not in names:
            continue
        prefix = tokens[:index]
        meaningful = [part for part in prefix if part.type != "comment"]
        implicit_universal = (
            not meaningful
            or meaningful[-1].type == "whitespace"
            or (meaningful[-1].type == "literal" and getattr(meaningful[-1], "value", None) in {">", "+", "~"})
        )
        subject = tinycss2.serialize(prefix)
        if implicit_universal:
            subject += "*"
        subject += tinycss2.serialize(tokens[name_index + 1 :])
        return subject, name
    return selector, ""


_ZERO_LENGTH = re.compile(r"[+-]?(?:0+(?:\.0*)?|\.0+)(?:[a-z]+|%)?\Z")
_BORDER_STYLES = {"solid", "dotted", "dashed", "double", "groove", "ridge", "inset", "outset"}
_BORDER_SIDES = ("left", "right")


def _advances(value: str) -> bool:
    return value not in {"auto", "none", "normal", "transparent", "initial", "unset"} and not bool(
        _ZERO_LENGTH.fullmatch(value)
    )


def _horizontal_shorthand(value: str) -> bool:
    parts = value.split()
    if not parts:
        return False
    if len(parts) == 1:
        horizontal = parts
    elif len(parts) in {2, 3}:
        horizontal = parts[1:2]
    elif len(parts) == 4:
        horizontal = [parts[1], parts[3]]
    else:
        return True
    return any(_advances(part) for part in horizontal)


def _border_components(value: str) -> tuple[str, str]:
    style, width = "none", "medium"
    dynamic = False
    for token in tinycss2.parse_component_value_list(value):
        if token.type in {"whitespace", "comment"}:
            continue
        serialized = tinycss2.serialize([token]).strip().lower()
        if token.type == "ident" and serialized in _BORDER_STYLES | {"none", "hidden"}:
            style = serialized
        elif (token.type == "ident" and serialized in {"thin", "medium", "thick"}) or token.type in {
            "dimension",
            "number",
        }:
            width = serialized
        elif token.type == "function" and getattr(token, "lower_name", "") in {"calc", "clamp", "max", "min", "var"}:
            dynamic = True
    if dynamic and style == "none":
        return "dynamic", "dynamic"
    return style, width


def _quad_horizontal(value: str) -> tuple[str, str]:
    parts = value.split()
    if len(parts) == 1:
        return parts[0], parts[0]
    if len(parts) in {2, 3}:
        return parts[1], parts[1]
    if len(parts) == 4:
        return parts[3], parts[1]
    return "dynamic", "dynamic"


def _inline_pair(value: str) -> tuple[str, str]:
    parts = value.split()
    if len(parts) == 1:
        return parts[0], parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    return "dynamic", "dynamic"


def _border_assignments(name: str, value: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []

    def assign(sides: tuple[str, ...], style: str | None = None, width: str | None = None) -> None:
        for side in sides:
            if style is not None:
                result.append((f"@border-{side}-style", style))
            if width is not None:
                result.append((f"@border-{side}-width", width))

    if name == "border":
        style, width = _border_components(value)
        assign(_BORDER_SIDES, style, width)
    elif name == "border-left":
        style, width = _border_components(value)
        assign(("left",), style, width)
    elif name == "border-right":
        style, width = _border_components(value)
        assign(("right",), style, width)
    elif name == "border-inline":
        style, width = _border_components(value)
        assign(("inline-start", "inline-end"), style, width)
    elif name == "border-inline-start":
        style, width = _border_components(value)
        assign(("inline-start",), style, width)
    elif name == "border-inline-end":
        style, width = _border_components(value)
        assign(("inline-end",), style, width)
    elif name == "border-left-style":
        assign(("left",), style=value)
    elif name == "border-right-style":
        assign(("right",), style=value)
    elif name == "border-inline-start-style":
        assign(("inline-start",), style=value)
    elif name == "border-inline-end-style":
        assign(("inline-end",), style=value)
    elif name == "border-left-width":
        assign(("left",), width=value)
    elif name == "border-right-width":
        assign(("right",), width=value)
    elif name == "border-inline-start-width":
        assign(("inline-start",), width=value)
    elif name == "border-inline-end-width":
        assign(("inline-end",), width=value)
    elif name == "border-style":
        start, end = _quad_horizontal(value)
        assign(("left",), style=start)
        assign(("right",), style=end)
    elif name == "border-width":
        start, end = _quad_horizontal(value)
        assign(("left",), width=start)
        assign(("right",), width=end)
    elif name == "border-inline-style":
        start, end = _inline_pair(value)
        assign(("inline-start",), style=start)
        assign(("inline-end",), style=end)
    elif name == "border-inline-width":
        start, end = _inline_pair(value)
        assign(("inline-start",), width=start)
        assign(("inline-end",), width=end)
    return result


def _winner(values: Mapping[str, _Winner], *names: str) -> _Winner | None:
    candidates = [values[name] for name in names if name in values]
    return max(candidates, key=lambda item: (item.important, item.specificity, item.order), default=None)


def _border_box_advances(values: Mapping[str, _Winner], direction: str) -> bool:
    logical = {
        "left": "inline-start" if direction == "ltr" else "inline-end",
        "right": "inline-end" if direction == "ltr" else "inline-start",
    }
    for side in _BORDER_SIDES:
        style = _winner(values, f"@border-{side}-style", f"@border-{logical[side]}-style")
        width = _winner(values, f"@border-{side}-width", f"@border-{logical[side]}-width")
        style_value = style.value if style is not None else "none"
        width_value = width.value if width is not None else "medium"
        if width_value in {"initial", "unset"}:
            width_value = "medium"
        if style_value not in {"none", "hidden", "initial", "unset"} and _advances(width_value):
            return True
    return False


def _visual(name: str, value: str, display: str, *, block: bool = False) -> bool:
    if display in {"none", "contents"}:
        return False
    if name == "content":
        return value not in {"none", "normal", '""', "''"}
    if block:
        return False
    if name == "display":
        return value in {"block", "flex", "grid", "table", "list-item", "flow-root"}
    if name in {
        "width",
        "min-width",
        "inline-size",
        "min-inline-size",
    }:
        return display != "inline" and _advances(value)
    if name in {"margin", "padding"}:
        return _horizontal_shorthand(value)
    if name.startswith(
        ("margin-left", "margin-right", "margin-inline", "padding-left", "padding-right", "padding-inline")
    ):
        return any(_advances(part) for part in value.split())
    if name.startswith(
        ("margin-top", "margin-bottom", "margin-block", "padding-top", "padding-bottom", "padding-block")
    ):
        return False
    return False


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


def _empty_locator_anchor(node: Any) -> bool:
    return (
        _name(node) == "a"
        and bool(node.get("id") or node.get("name"))
        and node.get("href") is None
        and len(node) == 0
        and not "".join(node.itertext())
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
        if (
            node.getparent() in blocked
            or code.is_code(node)
            and not _empty_locator_anchor(node)
            or _protected(node, safety)
        ):
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
    heading_paths = {
        paths[node]
        for node in nodes
        if _name(node) in HEADING_TAGS
        or _name(node) == "li"
        and any(_name(parent) == "nav" for parent in node.iterancestors())
    }
    chapter_paths: set[tuple[int, ...]] = set()
    for path, flow in flows.items():
        start = 0
        for end in range(len(flow) + 1):
            if end == len(flow) or flow[end] is None:
                _spaces([char for char in flow[start:end] if char is not None], start == 0, end == len(flow), spaces)
                start = end + 1
        if path in heading_paths:
            chars = [char for char in flow if char is not None]
            if label := CHAPTER_LABEL.match("".join(char.text for char in chars)):
                chapter_paths.add(path)
                if label["gap"]:
                    start, end = label.span("gap")
                    spaces.difference_update(char.raw for char in chars[start:end])
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
            or any(paths[parent] in chapter_paths for parent in node.iterancestors())
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
