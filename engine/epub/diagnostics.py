"""Compare conformance defects at stable resource and element identities."""

import hashlib
import re
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

from engine.epub.parsing import parse_resource
from engine.epub.ranges import ResourceIndex, index_resource
from engine.epub.validation import EpubCheckResult, EpubValidationError, ValidationIssue

_MESSAGE = re.compile(r"^(ERROR|FATAL)\(([^)]+)\): (.+?)(?:\((-?\d+),(-?\d+)\))?: (.*)$")


def validate(source: Path, result: EpubCheckResult) -> None:
    if result.returncode not in (0, 1) or (not result.passed and not (result.errors or result.fatals)):
        raise EpubValidationError("epubcheck_failed", "EPUBCheck did not return a usable conformance report")
    if result.errors or result.fatals:
        _keys(source, result)


def compare(source: Path, output: Path, baseline: EpubCheckResult, result: EpubCheckResult) -> dict[str, object]:
    """Allow only defects proven to belong to the same original element."""
    if baseline.command != result.command:
        raise EpubValidationError("checker_changed", "Source and output EPUBCheck commands differ")
    for checked in (baseline, result):
        if checked.returncode not in (0, 1) or (not checked.passed and not (checked.errors or checked.fatals)):
            raise EpubValidationError("epubcheck_failed", "EPUBCheck did not return a usable conformance report")
    original = _keys(source, baseline)
    current = _keys(output, result)
    added = current - original
    if added:
        severity, code, resource, node, message = next(iter(added))
        raise EpubValidationError(
            "new_epubcheck_errors",
            f"New EPUBCheck {severity} {code} in {resource} at {node}: {message}",
        )
    return {
        "format": "epubox-diagnostics-1",
        "source_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
        "output_hash": hashlib.sha256(output.read_bytes()).hexdigest(),
        "source_epubcheck": baseline.to_dict(),
        "inherited_errors": sum(current.values()),
    }


def references(
    baseline: tuple[ValidationIssue, ...], current: tuple[ValidationIssue, ...]
) -> tuple[ValidationIssue, ...]:
    remaining = Counter((issue.code, issue.resource, issue.message) for issue in baseline)
    added = []
    for issue in current:
        key = (issue.code, issue.resource, issue.message)
        if remaining[key]:
            remaining[key] -= 1
        else:
            added.append(issue)
    return tuple(added)


def same_messages(path: Path, saved: tuple[str, ...], actual: tuple[str, ...]) -> bool:
    """Compare reports for identical bytes after moving the candidate EPUB."""
    with ZipFile(path) as archive:
        resources = sorted(archive.namelist(), key=len, reverse=True)

    def normalized(lines: tuple[str, ...]) -> Counter:
        values = Counter()
        for line in lines:
            match = _MESSAGE.fullmatch(line)
            if match is None:
                raise EpubValidationError("unmatched_diagnostic", "Persisted EPUBCheck diagnostic is invalid")
            severity, code, name, row, column, message = match.groups()
            resource = next((item for item in resources if name.endswith("/" + item)), None)
            if resource is None and name.lower().endswith(".epub"):
                resource = ""
            if resource is None:
                raise EpubValidationError("unmatched_diagnostic", "Persisted EPUBCheck resource is invalid")
            values[(severity, code, resource, row, column, message)] += 1
        return values

    return normalized(saved) == normalized(actual)


def _keys(path: Path, result: EpubCheckResult) -> Counter:
    keys = Counter()
    indices: dict[str, ResourceIndex | None] = {}
    with ZipFile(path) as archive:
        for line in (*result.errors, *result.fatals):
            match = _MESSAGE.fullmatch(line)
            if match is None:
                raise EpubValidationError("unmatched_diagnostic", f"Unrecognized EPUBCheck diagnostic: {line}")
            severity, code, name, row, column, message = match.groups()
            name = name.removeprefix("file:")
            roots = {str(path), str(path.resolve())}
            if name in roots:
                resource, anchor = "", ("package",)
            else:
                resource = next((name[len(root) + 1 :] for root in roots if name.startswith(root + "/")), None)
                if resource is None or resource not in archive.namelist():
                    raise EpubValidationError("unmatched_diagnostic", f"EPUBCheck resource cannot be located: {name}")
                raw = archive.read(resource)
                if resource not in indices:
                    try:
                        indices[resource] = index_resource(parse_resource(raw, "application/xml"))
                    except ValueError:
                        indices[resource] = None
                index = indices[resource]
                if index is None or row is None or int(row) < 1 or column is None or int(column) < 1:
                    anchor = ("bytes", hashlib.sha256(raw).hexdigest())
                else:
                    anchor = _node(index, int(row), int(column))
            keys[(severity, code, resource, anchor, message)] += 1
    return keys


def _node(index: ResourceIndex, row: int, column: int) -> tuple:
    lines = index.text.splitlines(keepends=True)
    if row > len(lines):
        raise EpubValidationError("unmatched_diagnostic", "EPUBCheck line lies outside its resource")
    units = lines[row - 1].encode("utf-16-le")
    if (column - 1) * 2 > len(units):
        raise EpubValidationError("unmatched_diagnostic", "EPUBCheck column lies outside its resource")
    try:
        prefix = "".join(lines[: row - 1]) + units[: (column - 1) * 2].decode("utf-16-le")
    except UnicodeDecodeError as error:
        raise EpubValidationError("unmatched_diagnostic", "EPUBCheck column splits a Unicode character") from error
    bom = len(index.raw) - len(index.text.encode(index.encoding))
    position = bom + len(prefix.encode(index.encoding))
    nodes = [node for node in index.nodes.values() if node.full.start <= position < node.full.end]
    if not nodes:
        raise EpubValidationError("unmatched_diagnostic", "EPUBCheck position cannot be bound to an element")
    node = max(nodes, key=lambda value: len(value.path))
    return ("element", *node.path, node.qname)
