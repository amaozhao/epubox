"""Shared source-grounded classification of code regions in XHTML resources."""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

import soupsieve
import tinycss2
from bs4 import BeautifulSoup, Tag
from lxml import etree  # type: ignore[attr-defined]

from engine.item.policy import _BLOCK_TAGS

CODE_CLASSIFICATION_VERSION = "code-context-1"

_SEMANTIC_BLOCK = {"pre"}
_SEMANTIC_INLINE = {"code", "kbd", "samp", "var", "tt"}
_BLOCK_CLASSES = {"code", "codeblock", "programlisting", "sourcecode"}
_BLOCK_ELEMENTS = _BLOCK_TAGS - {"body", "caption", "figcaption", "hr"}
_INLINE_CLASSES = {"code", "codeinline", "mono", "monospace", "literal"}
_INLINE_ELEMENTS = {"span", "em", "i", "strong", "b", "a", "mark"}
_LINE_CLASSES = {"linegroup", "fixedline"}
_HIGHLIGHTER_CLASSES = {"hljs", "prettify", "prettyprint"}
_PREFORMATTED = {"pre", "pre-wrap", "break-spaces"}
_CSS_WIDE = {"inherit", "initial", "revert", "revert-layer", "unset"}
_GENERICS = {
    "serif",
    "sans-serif",
    "monospace",
    "cursive",
    "fantasy",
    "system-ui",
    "ui-serif",
    "ui-sans-serif",
    "ui-monospace",
    "ui-rounded",
    "math",
    "fangsong",
}
_KNOWN_MONOSPACE = {
    "andale mono",
    "consolas",
    "courier",
    "courier new",
    "dejavu sans mono",
    "fira code",
    "jetbrains mono",
    "liberation mono",
    "lucida console",
    "menlo",
    "monaco",
    "noto sans mono",
    "roboto mono",
    "sf mono",
    "sfmono-regular",
    "source code pro",
    "ubuntu mono",
}
_NESTED = {"container", "document", "layer", "media", "scope", "starting-style", "supports"}
_RELEVANT = {"white-space", "font-family", "font", "all"}
_SUSPICIOUS = re.compile(r"(?:white-space|font(?:-family)?|mono(?:space)?|code|pre-wrap)", re.IGNORECASE)
_CLASS_ALIASES = {
    "code-block": "codeblock",
    "code_block": "codeblock",
    "source-code": "sourcecode",
    "source_code": "sourcecode",
    "inline-code": "codeinline",
    "inline_code": "codeinline",
    "code-inline": "codeinline",
}


@dataclass(frozen=True)
class _Winner:
    value: str
    important: bool
    specificity: tuple[int, int, int]
    order: int
    direct: bool = True


@dataclass(frozen=True)
class _Conditional:
    node: etree._Element
    values: Mapping[str, _Winner]
    resource: str


class CodeContext:
    """Classify maximal source elements whose content must remain byte-faithful code."""

    def __init__(
        self,
        root: etree._Element | etree._ElementTree,
        resource_path: str,
        styles: Mapping[str, str] | None = None,
    ) -> None:
        self.root = root.getroot() if isinstance(root, etree._ElementTree) else root
        if not isinstance(self.root.tag, str):
            raise TypeError("code classification requires an XML element root")
        self.resource_path = _package_path(resource_path)
        self._styles = self._normalize_styles(styles or {})
        self.stylesheet_hashes: dict[str, str] = {}
        self._elements = tuple(node for node in self.root.iter() if isinstance(node.tag, str))
        self._winners: dict[etree._Element, dict[str, _Winner]] = {node: {} for node in self._elements}
        self._conditionals: list[_Conditional] = []
        self._order = 0
        self._soup, self._soup_to_lxml = self._bind_selector_tree()
        self._loading: set[str] = set()
        self._load_document_styles()
        self._load_inline_styles()
        self._validate_conditionals()
        candidates = self._candidates()
        self.regions: dict[etree._Element, Literal["block", "inline"]] = self._maximal(candidates)
        self._code_nodes = {
            descendant for region in self.regions for descendant in region.iter() if isinstance(descendant.tag, str)
        }

    def is_code(self, node: etree._Element) -> bool:
        """Return whether node belongs to one of the proven code regions."""
        return node in self._code_nodes

    @staticmethod
    def _normalize_styles(styles: Mapping[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for path, text in styles.items():
            key = _package_path(path)
            text = text.removeprefix("\ufeff")
            if key in normalized and normalized[key] != text:
                raise ValueError(f"conflicting stylesheet bytes for {key}")
            normalized[key] = text
        return normalized

    def _bind_selector_tree(self) -> tuple[BeautifulSoup, dict[int, etree._Element]]:
        soup = BeautifulSoup(etree.tostring(self.root, encoding="utf-8"), "xml")
        rendered = tuple(soup.find_all(True))
        if len(rendered) != len(self._elements):
            raise ValueError("selector tree does not map one-to-one to the source XML")
        mapping: dict[int, etree._Element] = {}
        for source, selected in zip(self._elements, rendered, strict=True):
            selected_class = selected.get("class")
            if isinstance(selected_class, list):
                selected_class = " ".join(selected_class)
            if (
                _name(source) != selected.name.rsplit(":", 1)[-1].lower()
                or source.get("id") != selected.get("id")
                or (source.get("class") or "") != (selected_class or "")
            ):
                raise ValueError("selector tree identity differs from the source XML")
            mapping[id(selected)] = source
        if len(mapping) != len(self._elements):
            raise ValueError("selector tree contains duplicate element identities")
        return soup, mapping

    def _load_document_styles(self) -> None:
        for node in self._elements:
            name = _name(node)
            if name == "style":
                self._apply_stylesheet("".join(node.itertext()), self.resource_path)
            elif name == "link" and "stylesheet" in (node.get("rel") or "").lower().split():
                target = _resolve(self.resource_path, node.get("href", ""))
                if target is not None:
                    self._load_external(target)

    def _load_external(self, path: str) -> None:
        if path in self._loading:
            raise ValueError(f"css import cycle includes stylesheet {path}")
        text = self._styles.get(path)
        if text is None:
            raise ValueError(f"missing stylesheet required by code classification: {path}")
        self._loading.add(path)
        self.stylesheet_hashes[path] = hashlib.sha256(text.encode()).hexdigest()
        try:
            self._apply_stylesheet(text, path)
        finally:
            self._loading.remove(path)

    def _apply_stylesheet(self, text: str, path: str) -> None:
        text = text.removeprefix("\ufeff")
        if "\x00" in text:
            raise ValueError(f"decoded stylesheet contains NUL: {path}")
        rules = tinycss2.parse_stylesheet(text, skip_comments=True, skip_whitespace=True)
        self._apply_rules(rules, path, raw=text, namespaces=_stylesheet_namespaces(rules, path))

    def _apply_rules(self, rules: list[Any], path: str, *, raw: str, namespaces: Mapping[str, str]) -> None:
        for rule in rules:
            kind = getattr(rule, "type", "")
            if kind == "error":
                if _SUSPICIOUS.search(raw):
                    raise ValueError(f"invalid code-related CSS in {path}: {rule.message}")
                continue
            if kind == "at-rule":
                keyword = getattr(rule, "lower_at_keyword", "")
                if keyword == "import":
                    imported = _import_target(rule.prelude)
                    if imported is None:
                        if _SUSPICIOUS.search(raw):
                            raise ValueError(f"invalid code-related @import in {path}")
                        continue
                    target = _resolve(path, imported)
                    if target is not None:
                        self._load_external(target)
                    continue
                if keyword == "namespace":
                    continue
                if rule.content is not None and keyword in _NESTED:
                    nested = tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True)
                    self._collect_conditionals(
                        nested,
                        path,
                        raw=tinycss2.serialize(rule.content),
                        namespaces=namespaces,
                    )
                continue
            if kind != "qualified-rule" or rule.content is None:
                continue
            declarations = _declarations(rule.content, path)
            if not declarations:
                continue
            for selector in _selectors(rule.prelude):
                _reject_complex_selector(selector, path)
                _require_declared_namespaces(selector, namespaces, path)
                try:
                    matches = soupsieve.select(selector, self._soup, namespaces=dict(namespaces))
                except soupsieve.SelectorSyntaxError as error:
                    raise ValueError(f"unsupported selector for code-related CSS in {path}: {selector}") from error
                specificity = _specificity(selector)
                for match in matches:
                    if not isinstance(match, Tag) or id(match) not in self._soup_to_lxml:
                        raise ValueError("selector result cannot be rebound to the source XML")
                    node = self._soup_to_lxml[id(match)]
                    for name, value, important in declarations:
                        self._apply(node, name, value, important, specificity)

    def _collect_conditionals(self, rules: list[Any], path: str, *, raw: str, namespaces: Mapping[str, str]) -> None:
        for rule in rules:
            kind = getattr(rule, "type", "")
            if kind == "error":
                if _SUSPICIOUS.search(raw):
                    raise ValueError(f"invalid conditional code style in {path}: {rule.message}")
                continue
            if kind == "at-rule" and rule.content is not None:
                nested = tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True)
                self._collect_conditionals(
                    nested,
                    path,
                    raw=tinycss2.serialize(rule.content),
                    namespaces=namespaces,
                )
                continue
            if kind != "qualified-rule" or rule.content is None:
                continue
            declarations = _declarations(rule.content, path)
            if not declarations:
                continue
            for selector in _selectors(rule.prelude):
                _reject_complex_selector(selector, path)
                _require_declared_namespaces(selector, namespaces, path)
                try:
                    matches = soupsieve.select(selector, self._soup, namespaces=dict(namespaces))
                except soupsieve.SelectorSyntaxError as error:
                    raise ValueError(
                        f"unsupported selector for conditional code style in {path}: {selector}"
                    ) from error
                specificity = _specificity(selector)
                for match in matches:
                    if not isinstance(match, Tag) or id(match) not in self._soup_to_lxml:
                        raise ValueError("conditional selector cannot be rebound to the source XML")
                    values: dict[str, _Winner] = {}
                    for name, value, important in declarations:
                        self._order += 1
                        candidate = _Winner(value, important, specificity, self._order)
                        current = values.get(name)
                        if current is None or (candidate.important, candidate.order) >= (
                            current.important,
                            current.order,
                        ):
                            values[name] = candidate
                    self._conditionals.append(_Conditional(self._soup_to_lxml[id(match)], values, path))

    def _load_inline_styles(self) -> None:
        inline = (1_000_000, 0, 0)
        for node in self._elements:
            style = node.get("style")
            if not style:
                continue
            content = tinycss2.parse_component_value_list(style)
            for name, value, important in _declarations(content, self.resource_path, declaration_list=True):
                self._apply(node, name, value, important, inline)

    def _apply(
        self,
        node: etree._Element,
        name: str,
        value: str,
        important: bool,
        specificity: tuple[int, int, int],
    ) -> None:
        self._order += 1
        candidate = _Winner(value, important, specificity, self._order)
        current = self._winners[node].get(name)
        if current is None or (candidate.important, candidate.specificity, candidate.order) >= (
            current.important,
            current.specificity,
            current.order,
        ):
            self._winners[node][name] = candidate

    def _validate_conditionals(self) -> None:
        for conditional in self._conditionals:
            node = conditional.node
            before_white = self._effective(node, "white-space")
            before_family = self._effective(node, "font-family")
            after = {"white-space": before_white, "font-family": before_family}
            changed = False
            for name, candidate in conditional.values.items():
                current = after[name]
                wins = (
                    current is None
                    or not current.direct
                    or (
                        candidate.important,
                        candidate.specificity,
                        candidate.order,
                    )
                    >= (current.important, current.specificity, current.order)
                )
                if wins:
                    changed = (
                        changed
                        or current is None
                        or current.value.strip().casefold() != candidate.value.strip().casefold()
                    )
                    after[name] = candidate
            if not changed:
                continue
            for affected in (node, *node.iterdescendants()):
                if not isinstance(affected.tag, str):
                    continue
                before_values = {
                    "white-space": self._effective(affected, "white-space"),
                    "font-family": self._effective(affected, "font-family"),
                }
                after_values = dict(before_values)
                for name in after_values:
                    if affected is node:
                        after_values[name] = after[name]
                    elif _inherits_through(affected, node, name, self._winners):
                        inherited = after[name]
                        after_values[name] = (
                            _Winner(
                                inherited.value,
                                inherited.important,
                                inherited.specificity,
                                inherited.order,
                                direct=False,
                            )
                            if inherited is not None
                            else None
                        )
                before = _computed_code(_name(affected), before_values["white-space"], before_values["font-family"])
                after_code = _computed_code(_name(affected), after_values["white-space"], after_values["font-family"])
                if before != after_code:
                    raise ValueError(
                        "conditional code style changes classification for "
                        f"<{_name(affected)}> in {conditional.resource}"
                    )

    def _effective(self, node: etree._Element, name: str) -> _Winner | None:
        if _is_caption(node):
            return None
        current = self._winners[node].get(name)
        if current is not None:
            lowered = current.value.strip().lower()
            if lowered not in _CSS_WIDE:
                return current
            if lowered in {"initial", "revert", "revert-layer"}:
                return None
        parent = node.getparent()
        if parent is None or parent not in self._winners:
            return None
        inherited = self._effective(parent, name)
        return (
            _Winner(inherited.value, inherited.important, inherited.specificity, inherited.order, direct=False)
            if inherited is not None
            else None
        )

    def _candidates(self) -> dict[etree._Element, Literal["block", "inline"]]:
        result: dict[etree._Element, Literal["block", "inline"]] = {}
        for node in self._elements:
            name = _name(node)
            classes = _classes(node)
            if name in _SEMANTIC_BLOCK:
                result[node] = "block"
                continue
            if name in _SEMANTIC_INLINE:
                result[node] = "inline"
                continue
            if _is_caption(node):
                continue
            if "programcode" in classes:
                for descendant in node.iterdescendants():
                    if isinstance(descendant.tag, str) and _classes(descendant) & _LINE_CLASSES:
                        result[descendant] = "block"
                continue
            if name not in {"html", "head", "body"} and _language_code_class(classes):
                result[node] = "block" if name in _BLOCK_ELEMENTS else "inline"
                continue
            if name in _BLOCK_ELEMENTS and classes & _BLOCK_CLASSES:
                self._strong_code_regions(node, result)
                continue
            if name in _INLINE_ELEMENTS and classes & _INLINE_CLASSES:
                result[node] = "inline"
                continue
            whitespace = self._effective(node, "white-space")
            family = self._effective(node, "font-family")
            white_kind = _whitespace_kind(whitespace.value) if whitespace is not None else "other"
            family_kind = _family_kind(family.value) if family is not None else "other"
            suspicious = bool(
                classes
                & (
                    _BLOCK_CLASSES
                    | _INLINE_CLASSES
                    | _HIGHLIGHTER_CLASSES
                    | {"highlight", "programcode", *_LINE_CLASSES}
                )
            )
            if (
                name in _INLINE_ELEMENTS
                and family is not None
                and family.direct
                and family_kind in {"dynamic", "invalid"}
            ):
                raise ValueError(f"dynamic code style cannot be classified safely on <{name}>")
            if (white_kind == "dynamic" and family_kind == "mono") or (
                family_kind in {"dynamic", "invalid"} and white_kind == "pre"
            ):
                raise ValueError(f"dynamic code style cannot be classified safely on <{name}>")
            if suspicious and (white_kind in {"dynamic", "invalid"} or family_kind in {"dynamic", "invalid"}):
                raise ValueError(f"dynamic code style cannot be classified safely on <{name}>")
            computed = _computed_code(name, whitespace, family)
            if computed == "block":
                result[node] = "block"
            elif computed == "inline":
                result[node] = "inline"
        return result

    @staticmethod
    def _strong_code_regions(
        node: etree._Element,
        result: dict[etree._Element, Literal["block", "inline"]],
    ) -> None:
        captions = [
            descendant
            for descendant in node.iterdescendants()
            if isinstance(descendant.tag, str) and _is_caption(descendant)
        ]
        if not captions:
            result[node] = "block"
            return
        if any(caption.getparent() is not node for caption in captions):
            raise ValueError("cannot partition code container with a nested caption safely")
        if (node.text or "").strip():
            raise ValueError("cannot partition code container with raw code and a caption")
        content: list[etree._Element] = []
        for child in node:
            if not isinstance(child.tag, str):
                continue
            if (child.tail or "").strip():
                raise ValueError("cannot partition code container with raw code and a caption")
            if not _is_caption(child):
                content.append(child)
        if not content:
            raise ValueError("cannot partition code container that contains only a caption")
        for child in content:
            result[child] = "block"

    def _maximal(
        self, candidates: Mapping[etree._Element, Literal["block", "inline"]]
    ) -> dict[etree._Element, Literal["block", "inline"]]:
        result: dict[etree._Element, Literal["block", "inline"]] = {}
        for node in self._elements:
            kind = candidates.get(node)
            if kind is None or any(ancestor in result for ancestor in node.iterancestors()):
                continue
            result[node] = kind
        return result


def _name(node: etree._Element) -> str:
    return etree.QName(node).localname.lower()


def _classes(node: etree._Element) -> set[str]:
    return {_CLASS_ALIASES.get(value.casefold(), value.casefold()) for value in (node.get("class") or "").split()}


def _is_caption(node: etree._Element) -> bool:
    return _name(node) in {"caption", "figcaption"} or "caption" in _classes(node)


def _language_code_class(classes: set[str]) -> bool:
    return bool(classes & _HIGHLIGHTER_CLASSES) or any(
        value.startswith("language-") and len(value) > len("language-") for value in classes
    )


def _inherits_through(
    node: etree._Element,
    ancestor: etree._Element,
    name: str,
    winners: Mapping[etree._Element, Mapping[str, _Winner]],
) -> bool:
    current = node
    while current is not ancestor:
        winner = winners[current].get(name)
        if winner is not None and winner.value.strip().casefold() not in {"inherit", "unset"}:
            return False
        current = current.getparent()
        if current is None:
            return False
    return True


def _package_path(path: str) -> str:
    normalized = posixpath.normpath(unquote(urlsplit(path).path))
    if not normalized or normalized == "." or normalized.startswith(("../", "/")):
        raise ValueError(f"unsafe package stylesheet path: {path!r}")
    return normalized


def _resolve(base: str, href: str) -> str | None:
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        raise ValueError(f"remote stylesheet cannot be resolved for code classification: {href}")
    if not parsed.path:
        return None
    return _package_path(posixpath.join(posixpath.dirname(base), unquote(parsed.path)))


def _import_target(tokens: list[Any]) -> str | None:
    significant = [token for token in tokens if token.type not in {"whitespace", "comment"}]
    if not significant:
        return None
    first = significant[0]
    if first.type in {"string", "url"}:
        return str(first.value)
    if first.type == "function" and first.lower_name == "url":
        return tinycss2.serialize(first.arguments).strip().strip("\"'")
    return None


def _stylesheet_namespaces(rules: list[Any], resource: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for rule in rules:
        if getattr(rule, "type", "") != "at-rule" or getattr(rule, "lower_at_keyword", "") != "namespace":
            continue
        significant = [token for token in rule.prelude if token.type not in {"whitespace", "comment"}]
        prefix = ""
        if len(significant) == 2 and significant[0].type == "ident":
            prefix = str(significant.pop(0).value)
        if len(significant) != 1:
            raise ValueError(f"invalid @namespace declaration in {resource}")
        token = significant[0]
        if token.type in {"string", "url"}:
            uri = str(token.value)
        elif token.type == "function" and token.lower_name == "url":
            uri = tinycss2.serialize(token.arguments).strip().strip("\"'")
        else:
            raise ValueError(f"invalid @namespace declaration in {resource}")
        if not uri:
            raise ValueError(f"empty @namespace URI in {resource}")
        result[prefix] = uri
    return result


def _require_declared_namespaces(selector: str, namespaces: Mapping[str, str], resource: str) -> None:
    prefixes = _selector_namespace_prefixes(tinycss2.parse_component_value_list(selector))
    missing = sorted(prefix for prefix in prefixes if prefix not in namespaces)
    if missing:
        raise ValueError(f"undeclared namespace prefix {missing[0]!r} in code-related CSS: {resource}")


def _selector_namespace_prefixes(tokens: list[Any]) -> set[str]:
    result: set[str] = set()
    for index, token in enumerate(tokens):
        content = getattr(token, "content", None)
        if isinstance(content, list):
            result.update(_selector_namespace_prefixes(content))
        if token.type != "literal" or getattr(token, "value", None) != "|" or index == 0:
            continue
        prefix = tokens[index - 1]
        if prefix.type == "ident":
            result.add(str(prefix.value))
    return result


def _selectors(tokens: list[Any]) -> tuple[str, ...]:
    groups: list[list[Any]] = [[]]
    for token in tokens:
        if token.type == "literal" and getattr(token, "value", None) == ",":
            if not groups[-1]:
                raise ValueError("empty selector in code-related CSS")
            groups.append([])
        else:
            groups[-1].append(token)
    selectors = tuple(tinycss2.serialize(group).strip() for group in groups)
    if not selectors or any(not selector for selector in selectors):
        raise ValueError("empty selector in code-related CSS")
    return selectors


def _specificity(selector: str) -> tuple[int, int, int]:
    tokens = tinycss2.parse_component_value_list(selector)
    ids = classes = elements = 0
    index = 0
    legacy_elements = {"before", "after", "first-line", "first-letter"}
    while index < len(tokens):
        token = tokens[index]
        kind = token.type
        if kind == "hash" and getattr(token, "is_identifier", False):
            ids += 1
        elif kind == "[] block":
            classes += 1
        elif kind == "literal" and getattr(token, "value", None) == ".":
            if index + 1 >= len(tokens) or tokens[index + 1].type != "ident":
                raise ValueError(f"invalid class selector in code-related CSS: {selector}")
            classes += 1
            index += 1
        elif kind == "literal" and getattr(token, "value", None) == ":":
            double = (
                index + 1 < len(tokens)
                and tokens[index + 1].type == "literal"
                and getattr(tokens[index + 1], "value", None) == ":"
            )
            position = index + 2 if double else index + 1
            if position >= len(tokens) or tokens[position].type not in {"ident", "function"}:
                raise ValueError(f"invalid pseudo selector in code-related CSS: {selector}")
            name = str(getattr(tokens[position], "lower_value", None) or getattr(tokens[position], "lower_name", ""))
            if double or name in legacy_elements:
                elements += 1
            else:
                classes += 1
            index = position
        elif kind == "ident":
            namespace = (
                index + 1 < len(tokens)
                and tokens[index + 1].type == "literal"
                and getattr(tokens[index + 1], "value", None) == "|"
            )
            if not namespace:
                elements += 1
        index += 1
    return ids, classes, elements


def _reject_complex_selector(selector: str, resource: str) -> None:
    tokens = tinycss2.parse_component_value_list(selector)
    supported = {"nth-child", "nth-last-child", "nth-of-type", "nth-last-of-type", "lang", "dir"}
    for token in tokens:
        if token.type != "function":
            continue
        name = str(getattr(token, "lower_name", ""))
        arguments = getattr(token, "arguments", ())
        has_of = any(
            argument.type == "ident" and getattr(argument, "lower_value", "") == "of" for argument in arguments
        )
        if name not in supported or has_of:
            raise ValueError(f"unsupported functional selector for code-related CSS in {resource}: {selector}")


def _declarations(
    content: list[Any], resource: str, *, declaration_list: bool = False
) -> tuple[tuple[str, str, bool], ...]:
    nodes = tinycss2.parse_declaration_list(
        tinycss2.serialize(content) if declaration_list else content,
        skip_comments=True,
        skip_whitespace=True,
    )
    result: list[tuple[str, str, bool]] = []
    for node in nodes:
        if node.type == "error":
            if _SUSPICIOUS.search(tinycss2.serialize(content)):
                raise ValueError(f"invalid code-related declaration in {resource}: {node.message}")
            continue
        if node.type != "declaration" or node.lower_name not in _RELEVANT:
            continue
        if node.lower_name == "all":
            value = tinycss2.serialize(node.value).strip()
            if value.casefold() not in _CSS_WIDE:
                value = "var(--invalid-all)"
            result.extend(
                (
                    ("white-space", value, bool(node.important)),
                    ("font-family", value, bool(node.important)),
                )
            )
        elif node.lower_name == "font":
            value = _font_family(node.value)
            if value is not None:
                result.append(("font-family", value, bool(node.important)))
        else:
            result.append((node.lower_name, tinycss2.serialize(node.value).strip(), bool(node.important)))
    return tuple(result)


def _font_family(tokens: list[Any]) -> str | None:
    text = tinycss2.serialize(tokens).strip()
    if not text or text.casefold() in _CSS_WIDE:
        return text or None
    if "var(" in text.casefold():
        return text
    significant = [token for token in tokens if token.type not in {"whitespace", "comment"}]
    size = next(
        (
            index
            for index, token in enumerate(significant)
            if token.type in {"dimension", "percentage"}
            or token.type == "number"
            and getattr(token, "value", None) == 0
            or token.type == "ident"
            and token.lower_value
            in {
                "xx-small",
                "x-small",
                "small",
                "medium",
                "large",
                "x-large",
                "xx-large",
                "xxx-large",
                "smaller",
                "larger",
            }
        ),
        None,
    )
    if size is None:
        return text
    position = size + 1
    if position < len(significant) and significant[position].type == "literal" and significant[position].value == "/":
        position += 2
    if position >= len(significant):
        return text
    start = tokens.index(significant[position])
    return tinycss2.serialize(tokens[start:]).strip()


def _whitespace_kind(value: str) -> Literal["pre", "other", "dynamic", "invalid"]:
    lowered = value.strip().casefold()
    if "var(" in lowered or "env(" in lowered:
        return "dynamic"
    if lowered in _PREFORMATTED:
        return "pre"
    if lowered in {"normal", "nowrap", *_CSS_WIDE}:
        return "other"
    return "invalid"


def _family_kind(value: str) -> Literal["mono", "other", "dynamic", "invalid"]:
    tokens = tinycss2.parse_component_value_list(value)
    if any(token.type == "function" for token in tokens):
        return "dynamic"
    if monospace_family(value):
        return "mono"
    families: list[list[Any]] = [[]]
    for token in tokens:
        if token.type in {"whitespace", "comment"}:
            continue
        if token.type == "literal" and getattr(token, "value", None) == ",":
            if not families[-1]:
                return "invalid"
            families.append([])
        elif token.type in {"ident", "string"}:
            families[-1].append(token)
        else:
            return "invalid"
    if not families[-1]:
        return "invalid"
    for index, group in enumerate(families):
        generics = [token for token in group if token.type == "ident" and token.lower_value in _GENERICS]
        if generics and (len(group) != 1 or index != len(families) - 1):
            return "invalid"
    return "other"


def monospace_family(value: str) -> bool:
    """Return whether a valid family list explicitly selects a known monospace face."""
    tokens = tinycss2.parse_component_value_list(value)
    if any(token.type == "function" for token in tokens):
        return False
    families: list[list[Any]] = [[]]
    for token in tokens:
        if token.type in {"whitespace", "comment"}:
            continue
        if token.type == "literal" and getattr(token, "value", None) == ",":
            if not families[-1]:
                return False
            families.append([])
        elif token.type in {"ident", "string"}:
            families[-1].append(token)
        else:
            return False
    if not families[-1]:
        return False
    names: list[str] = []
    for index, group in enumerate(families):
        generics = [token for token in group if token.type == "ident" and token.lower_value in _GENERICS]
        if generics and (len(group) != 1 or index != len(families) - 1):
            return False
        names.append(" ".join(str(token.value) for token in group).casefold())
    final = families[-1]
    generic_mono = (
        len(final) == 1 and final[0].type == "ident" and final[0].lower_value in {"monospace", "ui-monospace"}
    )
    return generic_mono or any(name in _KNOWN_MONOSPACE for name in names)


def _computed_code(
    name: str,
    whitespace: _Winner | None,
    family: _Winner | None,
) -> Literal["block", "inline"] | None:
    white_kind = _whitespace_kind(whitespace.value) if whitespace is not None else "other"
    family_kind = _family_kind(family.value) if family is not None else "other"
    if name in _BLOCK_ELEMENTS and white_kind == "pre" and family_kind == "mono":
        return "block"
    if name in _INLINE_ELEMENTS and family is not None and family.direct and family_kind == "mono":
        return "inline"
    return None


__all__ = ["CODE_CLASSIFICATION_VERSION", "CodeContext", "monospace_family"]
