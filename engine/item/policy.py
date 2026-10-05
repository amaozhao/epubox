"""Stable extraction policy, constants, and classification helpers."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

import regex
from lxml import etree  # type: ignore[attr-defined]

from engine.core.markup import qname_local_name
from engine.core.styles import ReorderPolicy, scan_inline_style

if TYPE_CHECKING:
    from engine.item.structure import _Extractor

_EPUB_TYPE = "{http://www.idpf.org/2007/ops}type"
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
_TRANSLATABLE_ATTRS = {"alt", "title", "aria-label", "aria-description"}
_HARD_TAGS = {"code", "pre", "script", "style", "svg", "math", "kbd", "samp", "var", "tt"}
_MEDIA_TAGS = {"img", "audio", "video", "source", "canvas", "iframe", "object", "embed"}
_EMPTY_BOUNDARIES = {"br", "wbr"}
_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "body",
    "caption",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "section",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}
_SEMANTIC_TAGS = {
    "blockquote",
    "caption",
    "dd",
    "dt",
    "figcaption",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "p",
    "td",
    "th",
    "title",
}
_CONTAINER_TAGS = {"article", "aside", "body", "div", "footer", "header", "main", "nav", "section"}
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_LATIN_RE = re.compile(r"[A-Za-z]")
_MONOSPACE_CLASS_RE = re.compile(
    r"mono(?:space|cd)?|(?:^|[_-])(?:code|terminal|console|syntax)(?:[_-]|$)", re.IGNORECASE
)
_IDENTIFIER_RE = re.compile(
    r"(?:@?[A-Za-z_][A-Za-z0-9_]*\([^\n)]*\)|\b[A-Za-z_]\w*(?:::[A-Za-z_]\w*)+\b|"
    r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b|\b[A-Za-z_]\w*_[A-Za-z0-9_]+\b)"
)
_RUST_COMMAND_RE = re.compile(
    r"^(?:cargo(?:\s+(?:check|build|run|test|new|doc|install|update)(?:\s+[-\w=./]+)*)?|"
    r"rustup(?:\s+(?:update|doc|self\s+uninstall)(?:\s+[-\w=./]+)*)?|"
    r"rustc(?:\s+[-\w=./]+)*)$",
    re.IGNORECASE,
)
_CONTEXT_LIMIT = 400
_HINT_LIMIT = 160
_TERM_MODES = {"preferred", "required", "keep_source"}


def _effective_language(node: etree._Element) -> str:
    for item in [node, *node.iterancestors()]:
        if language := (item.get(_XML_LANG) or item.get("lang") or "").strip():
            return language.casefold()
    return ""


def _primary_language(root: etree._Element) -> str:
    languages = {
        (node.text or "").strip().casefold()
        for node in root.iter()
        if isinstance(node.tag, str) and qname_local_name(node.tag) == "language" and (node.text or "").strip()
    }
    return next(iter(languages)) if len(languages) == 1 else ""


def _unique_language_match(root: etree._Element, elements: list[etree._Element]) -> etree._Element | None:
    language = _primary_language(root)
    if not language:
        return None
    matches = [element for element in elements if _effective_language(element) == language]
    return matches[0] if len(matches) == 1 else None


def select_primary_title(root: etree._Element | etree._ElementTree) -> etree._Element | None:
    """Select only an unambiguous OPF primary title; preserve all others."""

    root_element = root.getroot() if isinstance(root, etree._ElementTree) else root
    titles = [
        node for node in root_element.iter() if isinstance(node.tag, str) and qname_local_name(node.tag) == "title"
    ]
    if not titles:
        return None
    by_id = {node.get("id"): node for node in titles if node.get("id")}
    main_refs = {
        (node.get("refines") or "").removeprefix("#")
        for node in root_element.iter()
        if isinstance(node.tag, str)
        and qname_local_name(node.tag) == "meta"
        and (node.get("property") or "").strip().casefold() == "title-type"
        and (node.text or "").strip().casefold() == "main"
    }
    if main_refs:
        candidates = {by_id[ref] for ref in main_refs if ref in by_id}
        return next(iter(candidates)) if len(candidates) == 1 else None
    if len(titles) == 1:
        return titles[0]
    return _unique_language_match(root_element, titles)


def _compile_translate_exceptions(self: _Extractor) -> dict[etree._Element, bool]:
    raw = self.config.get("translate_exceptions", ())
    if not raw:
        return {}
    if isinstance(raw, Mapping):
        rules = [{"selector": selector, "action": action} for selector, action in raw.items()]
    elif isinstance(raw, (list, tuple)):
        rules = list(raw)
    else:
        raise TypeError("translate_exceptions must be a mapping or list")

    overrides: dict[etree._Element, bool] = {}
    for rule in rules:
        if not isinstance(rule, Mapping):
            raise TypeError("each translate exception must be an object")
        resource = str(rule.get("resource", rule.get("resource_path", self.resource_path)))
        if resource != self.resource_path:
            continue
        selector = str(rule.get("selector", ""))
        element_id = str(rule.get("element_id", selector[1:] if selector.startswith("#") else ""))
        if not element_id or selector and selector != f"#{element_id}":
            raise ValueError("translate exceptions support only exact #id selectors")
        action = rule.get("action", rule.get("translate"))
        if isinstance(action, bool):
            translated = action
        elif str(action).lower() in {"translate", "yes", "true"}:
            translated = True
        elif str(action).lower() in {"keep", "no", "false"}:
            translated = False
        else:
            raise ValueError(f"unknown translate exception action: {action}")
        matches = [node for node in self.elements if node.get("id") == element_id]
        if not matches:
            self.issues.append(
                {
                    "scope": "document",
                    "code": "unmatched_translate_exception",
                    "element_id": element_id,
                }
            )
            continue
        for node in matches:
            overrides[node] = translated
    return overrides


def _translate_state(self: _Extractor, node: etree._Element, inherited: bool) -> bool:
    if node in self.translate_overrides:
        return self.translate_overrides[node]
    value = (node.get("translate") or "").strip().lower()
    if value in {"no", "false", "0"}:
        return False
    if value in {"yes", "true", "1"}:
        return True
    return inherited


def _translate_state_chain(self: _Extractor, node: etree._Element) -> bool:
    chain = list(node.iterancestors())[::-1] + [node]
    translated = True
    for item in chain:
        if self._is_hard(item):
            return False
        translated = self._translate_state(item, translated)
    return translated


def _has_translate_yes(self: _Extractor, node: etree._Element) -> bool:
    return any(
        self.translate_overrides.get(item) is True
        or (item.get("translate") or "").strip().lower() in {"yes", "true", "1"}
        for item in node.iterdescendants()
        if isinstance(item.tag, str)
    )


def _is_hard(self: _Extractor, node: etree._Element) -> bool:
    return isinstance(node.tag, str) and (qname_local_name(node.tag) in _HARD_TAGS or self._is_short_inline_code(node))


def _is_short_inline_code(self: _Extractor, node: etree._Element) -> bool:
    if qname_local_name(node.tag) != "span" or len(node):
        return False
    classes = " ".join((node.get("class") or "").split())
    if not _MONOSPACE_CLASS_RE.search(classes):
        return False
    text = "".join(node.itertext()).strip()
    if not text or len(text) > 80 or "\n" in text or len(text.split()) > 8:
        return False
    if _RUST_COMMAND_RE.fullmatch(text) or _IDENTIFIER_RE.search(text):
        return True
    if re.search(r"(?:^|\s)(?:--?[A-Za-z][\w-]*|[~./\\][^\s]+)(?:\s|$)", text):
        return True
    if text.startswith(("$ ", "> ", ">>> ", "# ")):
        return True
    return bool(re.fullmatch(r"(?:println!|[(){};]|%[A-Za-z_][A-Za-z0-9_]*%)", text))


def _inside_hard(self: _Extractor, node: etree._Element) -> bool:
    return any(self._is_hard(item) for item in [node, *node.iterancestors()])


def _is_atom(self: _Extractor, node: etree._Element) -> bool:
    name = qname_local_name(node.tag)
    return (
        self._is_hard(node)
        or name in _MEDIA_TAGS | _EMPTY_BOUNDARIES
        or self._is_footnote_ref(node)
        or self._is_pagebreak(node)
        or self._is_empty_anchor(node)
    )


def _is_footnote_ref(self: _Extractor, node: etree._Element) -> bool:
    epub_type = (node.get(_EPUB_TYPE) or node.get("epub:type") or "").lower()
    roles = (node.get("role") or "").casefold().split()
    return "noteref" in epub_type.split() or "doc-noteref" in roles


def _is_pagebreak(self: _Extractor, node: etree._Element) -> bool:
    epub_type = (node.get(_EPUB_TYPE) or node.get("epub:type") or "").casefold().split()
    roles = (node.get("role") or "").casefold().split()
    return "pagebreak" in epub_type or "doc-pagebreak" in roles


def _is_empty_anchor(self: _Extractor, node: etree._Element) -> bool:
    if len(node) or "".join(node.itertext()).strip():
        return False
    return bool(node.get("id") or (qname_local_name(node.tag) == "a" and node.get("name")))


def _boundary_type(self: _Extractor, node: etree._Element) -> str:
    name = qname_local_name(node.tag)
    if name in {"code", "pre", "kbd", "samp", "var", "tt"} or self._is_short_inline_code(node):
        return "code"
    if name in {"math", "svg"}:
        return "math"
    if name in _MEDIA_TAGS:
        return "media"
    if self._is_footnote_ref(node):
        return "footnote"
    if self._is_pagebreak(node):
        return "page"
    if self._is_empty_anchor(node):
        return "anchor"
    return name if name in _EMPTY_BOUNDARIES else "protected"


def _inline_reorder_allowed(self: _Extractor, node: etree._Element) -> bool:
    if qname_local_name(node.tag) in {"ruby", "rb", "rt", "rp", "bdo", "bdi"}:
        return False
    style = node.get("style")
    if style and scan_inline_style(style).policy != ReorderPolicy.REORDER_ALLOWED:
        return False
    for ancestor in node.iterancestors():
        inherited_style = ancestor.get("style")
        if not inherited_style:
            continue
        scan = scan_inline_style(inherited_style)
        if scan.policy == ReorderPolicy.UNKNOWN or any(
            constraint.mode == "descendants" for constraint in scan.constraints
        ):
            return False
    return True


def _style_reorder_allowed(self: _Extractor, node: etree._Element) -> bool:
    return (
        not self.style_document_fallback
        and node not in self.style_locked_elements
        and node.getparent() not in self.style_locked_parents
    )


def _kind(self: _Extractor, node: etree._Element, virtual: bool = False) -> str:
    name = qname_local_name(node.tag)
    if virtual:
        return f"{name}_text_region"
    if name in {"h1", "h2", "h3", "h4", "h5", "h6", "title"}:
        return "heading"
    if name in {"td", "th", "caption"}:
        return "table_cell"
    if name == "figcaption":
        return "figure_caption"
    if name in {"li", "dt", "dd"}:
        return "list_item"
    return "paragraph"


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _resolve_local_resource(base_resource: str, href: str) -> str | None:
    parsed = urlsplit(href)
    if not parsed.path or parsed.scheme or parsed.netloc or parsed.path.startswith("/"):
        return None
    path = PurePosixPath(base_resource).parent / PurePosixPath(unquote(parsed.path))
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
    return "/".join(parts) if parts else None


def _bounded(value: str, limit: int, *, tail: bool = False) -> str:
    clusters = regex.findall(r"\X", value)
    if len(value) <= limit:
        return value
    selected: list[str] = []
    size = 0
    iterable = reversed(clusters) if tail else iter(clusters)
    for cluster in iterable:
        if size + len(cluster) > limit:
            break
        selected.append(cluster)
        size += len(cluster)
    if tail:
        selected.reverse()
    return "".join(selected)
