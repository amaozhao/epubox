"""Conservative CSS checks used only to decide inline reorder permission."""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

import tinycss2


class ReorderPolicy(StrEnum):
    REORDER_ALLOWED = "reorder_allowed"
    LOCKED = "locked"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class StyleIssue:
    code: str
    source: str
    detail: str


@dataclass(frozen=True, slots=True)
class StyleConstraint:
    selector: str
    mode: Literal["group", "element", "descendants"]


@dataclass(frozen=True, slots=True)
class StyleScan:
    policy: ReorderPolicy
    locked_selectors: tuple[str, ...] = ()
    constraints: tuple[StyleConstraint, ...] = ()
    issues: tuple[StyleIssue, ...] = ()
    visited: tuple[str, ...] = ()


def scan_css(css: str, *, source: str = "<style>") -> StyleScan:
    """Scan one complete stylesheet without pretending to compute the cascade."""
    return scan_stylesheets({source: css}, roots=(source,))


def scan_inline_style(style: str, *, source: str = "style attribute") -> StyleScan:
    declarations = tinycss2.parse_declaration_list(style, skip_comments=True, skip_whitespace=True)
    issues: list[StyleIssue] = []
    mode = _declaration_lock_mode(declarations, source, issues)
    if any(issue.code == "css_parse_error" for issue in issues):
        return StyleScan(ReorderPolicy.UNKNOWN, issues=tuple(issues), visited=(source,))
    return StyleScan(
        ReorderPolicy.LOCKED if mode else ReorderPolicy.REORDER_ALLOWED,
        constraints=(StyleConstraint(":scope", mode),) if mode else (),
        issues=tuple(issues),
        visited=(source,),
    )


def scan_stylesheets(
    stylesheets: Mapping[str, str] | Iterable[str],
    *,
    roots: Iterable[str] | None = None,
    loader: Callable[[str, str], tuple[str, str] | None] | None = None,
    max_import_depth: int = 4,
) -> StyleScan:
    """Scan local styles and bounded imports.

    ``loader(current_source, import_url)`` may resolve imports not present in the
    supplied mapping. Returning ``(canonical_name, css)`` keeps resolution local.
    """
    if max_import_depth < 0:
        raise ValueError("max_import_depth must be non-negative")
    if isinstance(stylesheets, Mapping):
        sources = dict(stylesheets)
    else:
        sources = {f"<style:{index}>": css for index, css in enumerate(stylesheets)}
    queue = [(name, 0) for name in (tuple(roots) if roots is not None else tuple(sources))]
    visited: list[str] = []
    seen: set[str] = set()
    locked_selectors: list[str] = []
    constraints: list[StyleConstraint] = []
    issues: list[StyleIssue] = []
    unknown = False

    while queue:
        name, depth = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        css = sources.get(name)
        if css is None:
            issues.append(StyleIssue("missing_stylesheet", name, "local stylesheet could not be read"))
            unknown = True
            continue
        visited.append(name)
        rules = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
        result = _scan_rules(rules, name, issues, locked_selectors, constraints)
        unknown |= result

        for import_url in _imports(rules):
            if depth >= max_import_depth:
                issues.append(StyleIssue("import_depth", name, import_url))
                unknown = True
                continue
            imported_name = import_url
            relative_name = posixpath.normpath(posixpath.join(posixpath.dirname(name), import_url))
            if imported_name not in sources and relative_name in sources:
                imported_name = relative_name
            if imported_name not in sources and loader is not None:
                loaded = loader(name, import_url)
                if loaded is not None:
                    imported_name, imported_css = loaded
                    sources.setdefault(imported_name, imported_css)
            if imported_name not in sources:
                issues.append(StyleIssue("unresolved_import", name, import_url))
                unknown = True
            else:
                queue.append((imported_name, depth + 1))

    policy = (
        ReorderPolicy.UNKNOWN
        if unknown
        else (ReorderPolicy.LOCKED if locked_selectors else ReorderPolicy.REORDER_ALLOWED)
    )
    return StyleScan(
        policy,
        tuple(dict.fromkeys(locked_selectors)),
        tuple(dict.fromkeys(constraints)),
        tuple(issues),
        tuple(visited),
    )


def selector_policy(selector: str) -> ReorderPolicy:
    """Classify the deliberately small selector subset supported by the MVP."""
    tokens = tinycss2.parse_component_value_list(selector, skip_comments=True)
    if any(token.type == "error" for token in tokens):
        return ReorderPolicy.UNKNOWN
    if any(token.type == "literal" and getattr(token, "value", "") == "|" for token in tokens):
        return ReorderPolicy.UNKNOWN
    if _contains_function(tokens, "has"):
        return ReorderPolicy.UNKNOWN
    if any(
        token.type in {"[] block", "() block", "function"}
        or (token.type == "literal" and getattr(token, "value", "") in {":", "+", "~"})
        for token in tokens
    ):
        return ReorderPolicy.LOCKED
    return ReorderPolicy.REORDER_ALLOWED if _supported_selector(tokens) else ReorderPolicy.UNKNOWN


def _scan_rules(
    rules: Sequence[Any],
    source: str,
    issues: list[StyleIssue],
    locked_selectors: list[str],
    constraints: list[StyleConstraint],
) -> bool:
    unknown = False
    for rule in rules:
        if rule.type == "error":
            issues.append(StyleIssue("css_parse_error", source, rule.message))
            unknown = True
            continue
        if rule.type == "qualified-rule":
            declarations = tinycss2.parse_declaration_list(rule.content, skip_comments=True, skip_whitespace=True)
            issue_count = len(issues)
            declaration_mode = _declaration_lock_mode(declarations, source, issues)
            unknown |= any(issue.code == "css_parse_error" for issue in issues[issue_count:])
            for selector in _selector_groups(rule.prelude):
                policy = selector_policy(selector)
                if policy == ReorderPolicy.UNKNOWN:
                    issues.append(StyleIssue("unknown_selector", source, selector))
                    unknown = True
                elif policy == ReorderPolicy.LOCKED:
                    locked_selectors.append(selector)
                    constraints.append(StyleConstraint(selector, "group"))
                if policy != ReorderPolicy.UNKNOWN and declaration_mode:
                    locked_selectors.append(selector)
                    constraints.append(StyleConstraint(selector, declaration_mode))
            continue
        if rule.type != "at-rule":
            issues.append(StyleIssue("unknown_rule", source, str(rule.type)))
            unknown = True
            continue
        keyword = rule.lower_at_keyword
        if keyword == "import":
            if _import_url(rule) is None:
                issues.append(StyleIssue("invalid_import", source, tinycss2.serialize(rule.prelude).strip()))
                unknown = True
            continue
        if keyword == "namespace":
            if not rule.prelude or any(token.type == "error" for token in rule.prelude):
                issues.append(StyleIssue("invalid_namespace", source, tinycss2.serialize(rule.prelude).strip()))
                unknown = True
            continue
        if keyword in {"media", "supports", "layer", "container", "scope"} and rule.content is not None:
            nested = tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True)
            unknown |= _scan_rules(nested, source, issues, locked_selectors, constraints)
        elif keyword in {"font-face", "page"}:
            declarations = tinycss2.parse_declaration_list(
                rule.content or (), skip_comments=True, skip_whitespace=True
            )
            for declaration in declarations:
                if declaration.type == "error":
                    issues.append(StyleIssue("css_parse_error", source, declaration.message))
                    unknown = True
        else:
            issues.append(StyleIssue("unknown_at_rule", source, f"@{keyword}"))
            unknown = True
    return unknown


def _supported_selector(tokens: Sequence[Any]) -> bool:
    significant = [token for token in tokens if token.type != "whitespace"]
    if not significant:
        return False
    expect_simple = True
    index = 0
    while index < len(significant):
        token = significant[index]
        if token.type == "literal" and token.value in {">", ","}:
            if expect_simple:
                return False
            expect_simple = True
        elif token.type == "literal" and token.value == ".":
            index += 1
            if index >= len(significant) or significant[index].type != "ident":
                return False
            expect_simple = False
        elif token.type in {"ident", "hash"} or (token.type == "literal" and token.value == "*"):
            expect_simple = False
        else:
            return False
        index += 1
    return not expect_simple


def _declaration_lock_mode(
    declarations: Sequence[Any], source: str, issues: list[StyleIssue]
) -> Literal["element", "descendants"] | None:
    mode: Literal["element", "descendants"] | None = None
    inherited = {
        "direction",
        "unicode-bidi",
        "writing-mode",
        "ruby-position",
        "ruby-align",
        "ruby-merge",
    }
    sensitive = {
        "content",
        "quotes",
        "counter-increment",
        "counter-reset",
        "counter-set",
    }
    structural_display = {
        "block",
        "contents",
        "flex",
        "grid",
        "inline-flex",
        "inline-grid",
        "inline-table",
        "list-item",
        "ruby",
        "ruby-base",
        "ruby-text",
        "table",
        "table-cell",
        "table-row",
    }
    for declaration in declarations:
        if declaration.type == "error":
            issues.append(StyleIssue("css_parse_error", source, declaration.message))
            continue
        if declaration.type != "declaration":
            continue
        name = declaration.lower_name
        value = tinycss2.serialize(declaration.value).strip().lower()
        if name in inherited:
            mode = "descendants"
        elif name in sensitive or (name == "display" and any(word in structural_display for word in value.split())):
            mode = mode or "element"
    return mode


def _selector_groups(tokens: Sequence[Any]) -> tuple[str, ...]:
    groups: list[str] = []
    current: list[Any] = []
    for token in tokens:
        if token.type == "literal" and token.value == ",":
            selector = tinycss2.serialize(current).strip()
            if selector:
                groups.append(selector)
            current = []
        else:
            current.append(token)
    selector = tinycss2.serialize(current).strip()
    if selector:
        groups.append(selector)
    return tuple(groups)


def _contains_function(tokens: Sequence[Any], name: str) -> bool:
    for token in tokens:
        if token.type == "function" and (
            getattr(token, "lower_name", "") == name or _contains_function(token.arguments, name)
        ):
            return True
        content = getattr(token, "content", None)
        if content is not None and _contains_function(content, name):
            return True
    return False


def _imports(rules: Sequence[Any]) -> tuple[str, ...]:
    imports: list[str] = []
    for rule in rules:
        if rule.type != "at-rule" or rule.lower_at_keyword != "import":
            continue
        if import_url := _import_url(rule):
            imports.append(import_url)
    return tuple(imports)


def _import_url(rule: Any) -> str | None:
    tokens = [token for token in rule.prelude if token.type != "whitespace"]
    if not tokens:
        return None
    first = tokens[0]
    if first.type in {"url", "string"}:
        return str(first.value)
    if first.type == "function" and first.lower_name == "url":
        return tinycss2.serialize(first.arguments).strip().strip("\"'") or None
    return None
