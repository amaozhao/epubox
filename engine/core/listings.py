"""Recognize code listings whose EPUB markup lost its original code semantics."""

from __future__ import annotations

import ast
import re
import textwrap

from lxml import etree  # type: ignore[attr-defined]

_LEGACY_GENERATED_CLASS = re.compile(r"class-[0-9]+-[0-9]+")
_GENERATED_CLASS = re.compile(r"class(?:[0-9]+|-[0-9]+-[0-9]+)")
_XHTML = "{http://www.w3.org/1999/xhtml}"
_CALIBRE_CLASS = re.compile(r"calibre[0-9]*")
_RUST_LISTING_SYNTAX = re.compile(
    r"(?:"
    r"(?:pub(?:\([^)]*\))?\s+)?(?:(?:const|async|unsafe)\s+)*fn\s+\w+(?:<[^>]+>)?\s*\("
    r"|(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|union|trait)\s+\w+[^\n]*\{\s*$"
    r"|(?:unsafe\s+)?impl(?:\s|<)[^\n]*\{\s*$"
    r"|(?:pub(?:\([^)]*\))?\s+)?mod\s+\w+\s*;"
    r"|(?:pub(?:\([^)]*\))?\s+)?use\s+(?:::)?\w+(?:::[\w*]+)*(?:\s+as\s+\w+)?(?:\s*;|::\{)"
    r"|pub(?:\([^)]*\))?\s+\w+:\s*[^,]+,"
    r"|let\s+(?:mut\s+)?\S.*\s*=\s*[^=].*;"
    r"|for\s+.+?\s+in\s+\S.*\{\s*$"
    r"|#!?\[[a-z_]\w*(?:::\w+)*(?:(?:\(.*\))?\]|\()\s*$"
    r"|\w+!\s*\(.*\)\s*;"
    r")"
)
_TERMINAL_COMMAND = re.compile(
    r"(?:[A-Z_][A-Z0-9_]*=\S+\s+)*(?:sudo\s+)?(?:"
    r"cargo\s+(?:add|bench|build|check|clippy|doc|fix|fmt|info|init|install|login|new|package|publish|run|test|tree|update|vendor|--version)"
    r"|rustc\s+(?:--\S+|\S+\.rs)|rustup\s+(?:component|default|show|target|toolchain|update|--version)"
    r"|apt(?:-get)?\s+(?:install|update)|go\s+run\s+\S+\.go|ls\s+-\S+|make\s+\w+=\S+|cbindgen\s+--\S+"
    r")(?:\s|$)"
)
_CONFIG_SECTION = re.compile(r"\[[\w.-]+\]")
_CONFIG_ASSIGNMENT = re.compile(r'[\w.-]+\s*=\s*(?:["\[{\d-]|true\b|false\b)')
_C_DIRECTIVE = re.compile(r"#(?:ifn?def|define|include|endif)\b")
_METRIC_OUTPUT = re.compile(r"(?:cpu|gpu|memory|load):\s+\d+(?:\.\d+)?(?:%|\s+[KMG]iB)", re.IGNORECASE)


def is_rendered_python_listing(node: etree._Element, *, numbered: bool) -> bool:
    """Recognize Python listings flattened into generated line spans and breaks."""
    generated = _GENERATED_CLASS if numbered else _LEGACY_GENERATED_CLASS
    text_classes = {"bookSpan", "bookspan"} if numbered else {"bookSpan"}
    if node.tag != f"{_XHTML}div" or not generated.fullmatch(node.get("class", "")) or (node.text or "").strip():
        return False
    lines = []
    line_class = None
    for index, child in enumerate(node):
        if (child.tail or "").strip():
            return False
        if index % 2:
            if child.tag != f"{_XHTML}br" or len(child) or (child.text or "").strip():
                return False
            continue
        css_class = child.get("class", "")
        if (
            child.tag != f"{_XHTML}span"
            or not generated.fullmatch(css_class)
            or (line_class is not None and line_class != css_class)
            or len(child) != 1
            or (child.text or "").strip()
        ):
            return False
        line_class = css_class
        text = child[0]
        if (
            text.tag != f"{_XHTML}span"
            or text.get("class") not in text_classes
            or len(text)
            or (text.tail or "").strip()
        ):
            return False
        lines.append((text.text or "").replace("\xa0", " "))
    if sum(bool(line.strip()) for line in lines) < 2:
        return False
    try:
        statements = ast.parse(textwrap.dedent("\n".join(lines))).body
    except IndentationError:
        # Some conversions put the first field's indentation in CSS.
        try:
            fields = ast.parse("\n".join(line.lstrip() for line in lines)).body
        except (SyntaxError, ValueError):
            return False
        return (
            bool(fields)
            and all(isinstance(field, ast.AnnAssign) for field in fields)
            and any(isinstance(field, ast.AnnAssign) and field.value is not None for field in fields)
        )
    except (SyntaxError, ValueError):
        return False
    return bool(statements) and all(
        isinstance(
            statement,
            (
                ast.Assign,
                ast.AugAssign,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.If,
                ast.Try,
                ast.With,
                ast.Import,
                ast.ImportFrom,
            ),
        )
        or isinstance(statement, ast.AnnAssign)
        and statement.value is not None
        or isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        for statement in statements
    )


def is_rendered_listing(node: etree._Element) -> bool:
    """Recognize flattened code, configuration and terminal listings from their source markup and syntax."""
    if (
        node.tag != f"{_XHTML}div"
        or not _GENERATED_CLASS.fullmatch(node.get("class", ""))
        or (node.text or "").strip()
    ):
        return False
    lines = []
    for row in node:
        line = _rendered_listing_row(row)
        if line is None:
            return False
        lines.append(line)
    return (
        any(_RUST_LISTING_SYNTAX.match(line) or _TERMINAL_COMMAND.match(line) for line in lines)
        or any(_CONFIG_SECTION.fullmatch(line) for line in lines)
        and any(_CONFIG_ASSIGNMENT.match(line) for line in lines)
        or sum(bool(_C_DIRECTIVE.match(line)) for line in lines) >= 2
        or sum(line.startswith("$(MAKE) ") for line in lines) >= 2
        or len(lines) >= 2
        and all(_METRIC_OUTPUT.fullmatch(line) for line in lines)
    )


def _rendered_listing_row(row: etree._Element) -> str | None:
    if (
        row.tag != f"{_XHTML}div"
        or not _GENERATED_CLASS.fullmatch(row.get("class", ""))
        or len(row) != 1
        or (row.text or "").strip()
        or (row.tail or "").strip()
    ):
        return None
    content = row[0]
    style = content.get("class", "")
    if content.tag != f"{_XHTML}div" or not (_CALIBRE_CLASS.fullmatch(style) or _GENERATED_CLASS.fullmatch(style)):
        return None
    if not len(content) or (content.text or "").strip() or (content.tail or "").strip():
        return None
    spans = []
    for child in content:
        if child.tag != f"{_XHTML}span" or (child.tail or "").strip():
            return None
        if child.get("class") in {"bookspan", "bookSpan"}:
            spans.append(child)
        elif _GENERATED_CLASS.fullmatch(child.get("class", "")) and len(child) and not (child.text or "").strip():
            spans.extend(child)
        else:
            return None
    if any(
        span.tag != f"{_XHTML}span" or span.get("class") not in {"bookspan", "bookSpan"} or len(span) for span in spans
    ):
        return None
    return "".join(content.itertext()).strip()
