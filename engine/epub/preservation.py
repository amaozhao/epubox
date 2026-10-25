"""Explicit source-bound restoration of authorized code regions."""

from __future__ import annotations

import hashlib
import re
import zipfile
from collections.abc import Mapping
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Any, TypedDict

import tinycss2

from engine.core.code import CodeContext
from engine.core.markup import element_path, qname_local_name
from engine.epub.parsing import ParsedResource, parse_resource
from engine.epub.ranges import ResourceIndex, index_resource
from engine.epub.styles import source_stylesheets
from engine.epub.upgrade import _entity_patches
from engine.schemas.bridge import TRANSLATABLE_ATTRIBUTES

CODE_POLICY = "source-code-v2"
CODE_PLAN_FORMAT = "epubox-code-preservation-1"
_KINDS = frozenset({"block", "inline"})
_BLOCK_TAGS = frozenset({"p", "div", "pre", "code"})
_INLINE_TAGS = frozenset({"span", "code"})
_SEMANTIC_CODE = frozenset({"pre", "code"})
_PRE = frozenset({"pre", "pre-wrap", "break-spaces"})
_CSS_WIDE = frozenset({"inherit", "initial", "revert", "revert-layer", "unset"})
_GENERIC_FAMILIES = frozenset(
    {
        "cursive",
        "emoji",
        "fangsong",
        "fantasy",
        "math",
        "monospace",
        "sans-serif",
        "serif",
        "system-ui",
        "ui-monospace",
        "ui-rounded",
        "ui-sans-serif",
        "ui-serif",
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class CodeRegion(TypedDict):
    path: list[int]
    kind: str
    sha256: str


class CodeResource(TypedDict):
    source_hash: str
    regions: list[CodeRegion]


class CodePlan(TypedDict):
    format: str
    source_hash: str
    resources: dict[str, CodeResource]


def build_code_plan(
    source: Path,
    selections: Mapping[str, Mapping[tuple[int, ...], str]],
) -> CodePlan:
    """Bind explicitly authorized source code nodes to immutable archive bytes."""
    return _build_code_plan(source, selections, automatic=False)


def _build_code_plan(
    source: Path,
    selections: Mapping[str, Mapping[tuple[int, ...], str]],
    *,
    automatic: bool,
) -> CodePlan:
    source = Path(source).resolve(strict=True)
    if not isinstance(selections, Mapping) or not selections:
        raise ValueError("code selections must be a non-empty mapping")
    resources: dict[str, CodeResource] = {}
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        for resource in sorted(selections):
            _safe_resource(resource)
            if names.count(resource) != 1:
                raise ValueError(f"selected code resource is missing or duplicated: {resource}")
            selected = selections[resource]
            if not isinstance(selected, Mapping) or not selected:
                raise ValueError(f"code resource has no selected regions: {resource}")
            raw = archive.read(resource)
            parsed, indexed, nodes = _indexed(raw, "source")
            if parsed.kind != "xhtml":
                raise ValueError(f"selected code resource is not XHTML: {resource}")
            paths = sorted(selected)
            _paths(paths)
            regions: list[CodeRegion] = []
            for path in paths:
                kind = selected[path]
                if kind not in _KINDS:
                    raise ValueError(f"unsupported code region kind: {kind!r}")
                node = nodes.get(path)
                if node is None or not automatic and not _authorized(node, kind):
                    raise ValueError(f"source node is not an authorized {kind} code region: {resource}/{path}")
                span = indexed.nodes[path].full
                data = raw[span.start : span.end]
                regions.append({"path": list(path), "kind": kind, "sha256": _hash(data)})
            resources[resource] = {
                "source_hash": _hash(raw),
                "regions": regions,
            }
    return {
        "format": CODE_PLAN_FORMAT,
        "source_hash": _file_hash(source),
        "resources": resources,
    }


def discover_code_plan(source: Path) -> CodePlan | None:
    """Discover every proven source code region without translating or mutating source."""
    source = Path(source).resolve(strict=True)
    selections: dict[str, dict[tuple[int, ...], str]] = {}
    with zipfile.ZipFile(source) as archive:
        styles = source_stylesheets(archive)
        for resource in _manifest_documents(archive):
            raw = archive.read(resource)
            parsed = parse_resource(raw, "application/xhtml+xml")
            if parsed.tree is None or parsed.diagnostics:
                continue
            context = CodeContext(parsed.tree, resource, styles)
            selected = {element_path(node): kind for node, kind in context.regions.items()}
            if selected:
                selections[resource] = dict(sorted(selected.items()))
    return _build_code_plan(source, selections, automatic=True) if selections else None


def effective_code_plan(source: Path, explicit: Mapping[str, object] | None = None) -> CodePlan | None:
    """Return all automatically discovered code plus any valid legacy explicit selections."""
    source = Path(source).resolve(strict=True)
    automatic = discover_code_plan(source)
    if explicit is not None:
        _source_contexts(source, explicit)
    plans = tuple(plan for plan in (automatic, explicit) if plan is not None)
    if not plans:
        return None
    selections: dict[str, dict[tuple[int, ...], str]] = {}
    for plan in plans:
        resources = plan.get("resources")
        if not isinstance(resources, Mapping):
            raise TypeError("validated code plan lost its resource inventory")
        for resource, record in resources.items():
            if not isinstance(resource, str) or not isinstance(record, Mapping):
                raise TypeError("validated code plan contains an invalid resource")
            regions = record.get("regions")
            if not isinstance(regions, list):
                raise TypeError("validated code plan contains invalid regions")
            selected = selections.setdefault(resource, {})
            for region in regions:
                if not isinstance(region, Mapping):
                    raise TypeError("validated code plan contains an invalid region")
                path_value, kind = region.get("path"), region.get("kind")
                if not isinstance(path_value, list) or not isinstance(kind, str):
                    raise TypeError("validated code plan contains an invalid region identity")
                path = tuple(path_value)
                current = selected.get(path)
                if current is not None and current != kind:
                    raise ValueError(f"conflicting automatic and explicit code region kind: {resource}/{path}")
                selected[path] = kind
    maximal: dict[str, dict[tuple[int, ...], str]] = {}
    for resource, selected in selections.items():
        kept: dict[tuple[int, ...], str] = {}
        for path in sorted(selected, key=lambda value: (len(value), value)):
            if not any(_contains(parent, path) for parent in kept):
                kept[path] = selected[path]
        maximal[resource] = dict(sorted(kept.items()))
    return _build_code_plan(source, maximal, automatic=True)


def apply_code_plan(
    source: Path,
    replacements: Mapping[str, bytes],
    plan: Mapping[str, object],
) -> tuple[dict[str, bytes], dict[str, int]]:
    """Overlay only selected source nodes onto structurally identical candidates."""
    source = Path(source).resolve(strict=True)
    contexts, stats = _source_contexts(source, plan)
    result = dict(replacements)
    if any(not isinstance(path, str) or not isinstance(data, bytes) for path, data in result.items()):
        raise TypeError("EPUB replacements must map resource paths to bytes")
    for resource, context in contexts.items():
        candidate = result.get(resource, context.raw)
        indexed, paths = _validate_candidate(context, candidate)
        patches = [
            (
                indexed.nodes[paths[region.path]].full,
                _source_region_bytes(context, region),
            )
            for region in context.regions
        ]
        restored = candidate
        for span, data in sorted(patches, key=lambda item: item[0].start, reverse=True):
            restored = restored[: span.start] + data + restored[span.end :]
        _validate_restored(context, restored)
        if restored != context.raw or resource in result:
            result[resource] = restored
    return result, stats


def verify_code_plan(
    source: Path,
    output: Path,
    plan: Mapping[str, object],
    *,
    node_maps: Mapping[str, Mapping[tuple[int, ...], tuple[int, ...]]] | None = None,
) -> dict[str, int]:
    """Verify that every authorized node in an output EPUB is byte-identical to source."""
    source = Path(source).resolve(strict=True)
    output = Path(output).resolve(strict=True)
    contexts, stats = _source_contexts(source, plan)
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        for resource, context in contexts.items():
            if names.count(resource) != 1:
                raise ValueError(f"output is missing selected code resource: {resource}")
            candidate = archive.read(resource)
            mapping = (node_maps or {}).get(resource)
            indexed, paths = _validate_candidate(context, candidate, mapping)
            _validate_restored(context, candidate, indexed=indexed, paths=paths)
    return stats


def encode_code_maps(
    maps: Mapping[str, Mapping[tuple[int, ...], tuple[int, ...]]],
) -> dict[str, list[list[list[int]]]]:
    """Encode source-to-output code resource paths as canonical JSON values."""
    encoded: dict[str, list[list[list[int]]]] = {}
    for resource in sorted(maps):
        _safe_resource(resource)
        paths = maps[resource]
        if not isinstance(paths, Mapping) or not paths:
            raise ValueError(f"code node map is empty: {resource}")
        _validate_path_map(paths)
        encoded[resource] = [[list(source), list(target)] for source, target in sorted(paths.items())]
    return encoded


def decode_code_maps(value: object) -> dict[str, dict[tuple[int, ...], tuple[int, ...]]]:
    """Decode and strictly validate canonical persisted code node mappings."""
    if not isinstance(value, Mapping) or not value or list(value) != sorted(value):
        raise ValueError("invalid persisted code node maps")
    decoded: dict[str, dict[tuple[int, ...], tuple[int, ...]]] = {}
    for resource, entries in value.items():
        if not isinstance(resource, str) or not isinstance(entries, list) or not entries:
            raise ValueError("invalid persisted code resource map")
        _safe_resource(resource)
        mapping: dict[tuple[int, ...], tuple[int, ...]] = {}
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != 2:
                raise ValueError(f"invalid persisted code path pair: {resource}")
            source, target = (_json_path(entry[0]), _json_path(entry[1]))
            if source in mapping:
                raise ValueError(f"duplicate persisted source code path: {resource}/{source}")
            mapping[source] = target
        if list(mapping) != sorted(mapping):
            raise ValueError(f"persisted code paths are not canonical: {resource}")
        _validate_path_map(mapping)
        decoded[resource] = mapping
    return decoded


class _Region:
    __slots__ = ("kind", "path", "sha256")

    def __init__(self, path: tuple[int, ...], kind: str, sha256: str):
        self.path = path
        self.kind = kind
        self.sha256 = sha256


class _Context:
    __slots__ = ("entity_patches", "indexed", "nodes", "parsed", "raw", "regions")

    def __init__(
        self,
        raw: bytes,
        parsed: ParsedResource,
        indexed: ResourceIndex,
        nodes: dict[tuple[int, ...], Any],
        regions: tuple[_Region, ...],
        entity_patches: tuple[tuple[int, int, bytes], ...],
    ):
        self.raw = raw
        self.parsed = parsed
        self.indexed = indexed
        self.nodes = nodes
        self.regions = regions
        self.entity_patches = entity_patches


def _source_contexts(source: Path, plan: Mapping[str, object]) -> tuple[dict[str, _Context], dict[str, int]]:
    if not isinstance(plan, Mapping) or set(plan) != {"format", "source_hash", "resources"}:
        raise ValueError("invalid source code plan shape")
    if plan.get("format") != CODE_PLAN_FORMAT or not _digest(plan.get("source_hash")):
        raise ValueError("invalid source code plan identity")
    if _file_hash(source) != plan["source_hash"]:
        raise ValueError("source EPUB does not match the code plan")
    resources = plan.get("resources")
    if not isinstance(resources, Mapping) or not resources or list(resources) != sorted(resources):
        raise ValueError("invalid source code resource inventory")
    contexts: dict[str, _Context] = {}
    selected_bytes = 0
    selected_regions = 0
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        styles = source_stylesheets(archive)
        for resource, record in resources.items():
            if not isinstance(resource, str):
                raise TypeError("invalid source code resource path")
            _safe_resource(resource)
            if names.count(resource) != 1 or not isinstance(record, Mapping):
                raise ValueError(f"invalid source code resource: {resource}")
            if set(record) != {"source_hash", "regions"} or not _digest(record.get("source_hash")):
                raise ValueError(f"invalid source code resource proof: {resource}")
            raw = archive.read(resource)
            if _hash(raw) != record["source_hash"]:
                raise ValueError(f"source resource hash differs from code plan: {resource}")
            parsed, indexed, nodes = _indexed(raw, "source")
            if parsed.kind != "xhtml":
                raise ValueError(f"selected code resource is not XHTML: {resource}")
            context = CodeContext(parsed.tree, resource, styles)
            automatic = {element_path(node): kind for node, kind in context.regions.items()}
            values = record.get("regions")
            if not isinstance(values, list) or not values:
                raise ValueError(f"invalid source code regions: {resource}")
            regions: list[_Region] = []
            for value in values:
                if not isinstance(value, Mapping) or set(value) != {"path", "kind", "sha256"}:
                    raise ValueError(f"invalid source code region proof: {resource}")
                path_value = value.get("path")
                kind, sha256 = value.get("kind"), value.get("sha256")
                if (
                    not isinstance(path_value, list)
                    or not path_value
                    or any(type(index) is not int or index < 0 for index in path_value)
                    or not isinstance(kind, str)
                    or kind not in _KINDS
                    or not isinstance(sha256, str)
                    or not _digest(sha256)
                ):
                    raise ValueError(f"invalid source code region identity: {resource}")
                path = tuple(path_value)
                node = nodes.get(path)
                if node is None or (automatic.get(path) != kind and not _authorized(node, kind)):
                    raise ValueError(f"source code region is no longer authorized: {resource}/{path}")
                span = indexed.nodes[path].full
                data = raw[span.start : span.end]
                if _hash(data) != sha256:
                    raise ValueError(f"selected source region hash differs: {resource}/{path}")
                regions.append(_Region(path, kind, sha256))
                selected_bytes += len(data)
            paths = [region.path for region in regions]
            if paths != sorted(paths):
                raise ValueError(f"source code regions are not canonical: {resource}")
            _paths(paths)
            selected_regions += len(regions)
            contexts[resource] = _Context(raw, parsed, indexed, nodes, tuple(regions), _entity_patches(raw, indexed))
    return contexts, {
        "checked_resources": len(contexts),
        "selected_bytes": selected_bytes,
        "selected_regions": selected_regions,
    }


def _validate_candidate(
    context: _Context,
    raw: bytes,
    node_map: Mapping[tuple[int, ...], tuple[int, ...]] | None = None,
) -> tuple[ResourceIndex, dict[tuple[int, ...], tuple[int, ...]]]:
    try:
        _parsed, indexed, nodes = _indexed(raw, "candidate")
    except (TypeError, ValueError) as error:
        raise ValueError(f"candidate topology cannot be verified: {error}") from error
    source_topology = {path: span.qname for path, span in context.indexed.nodes.items()}
    candidate_topology = {path: span.qname for path, span in indexed.nodes.items()}
    paths = dict(node_map or {path: path for path in source_topology})
    _validate_path_map(paths)
    if set(paths.values()) != set(candidate_topology):
        raise ValueError("candidate element mapping is incomplete or non-bijective")
    selected = tuple(region.path for region in context.regions)
    missing = set(source_topology) - set(paths)
    if any(any(_contains(root, path) for root in selected) or not _removable(context.nodes[path]) for path in missing):
        raise ValueError("candidate removed a non-decorative or selected source element")
    if paths != _canonical_path_map(context, missing):
        raise ValueError("candidate element mapping is not the canonical order-preserving deletion map")
    if any(
        source_topology[source_path] != candidate_topology[target_path] for source_path, target_path in paths.items()
    ):
        raise ValueError("candidate element topology differs from source")
    for path, source_node in context.nodes.items():
        if path not in paths:
            continue
        if _frozen_attributes(source_node.attrib, root=not path) != _frozen_attributes(
            nodes[paths[path]].attrib, root=not path
        ):
            raise ValueError(f"candidate frozen attribute differs from source at {path}")
    return indexed, paths


def _validate_restored(
    context: _Context,
    raw: bytes,
    *,
    indexed: ResourceIndex | None = None,
    paths: Mapping[tuple[int, ...], tuple[int, ...]] | None = None,
) -> None:
    if indexed is None:
        _parsed, indexed, _nodes = _indexed(raw, "restored")
    paths = paths or {path: path for path in context.indexed.nodes}
    for region in context.regions:
        target_path = paths.get(region.path)
        if target_path is None:
            raise ValueError(f"selected code region is missing: {region.path}")
        output_span = indexed.nodes[target_path].full
        source_data = _source_region_bytes(context, region)
        if raw[output_span.start : output_span.end] != source_data:
            raise ValueError(f"selected code region is not source-exact: {region.path}")


def _source_region_bytes(context: _Context, region: _Region) -> bytes:
    """Return source bytes with only lossless legacy named-entity normalization."""
    span = context.indexed.nodes[region.path].full
    result = context.raw[span.start : span.end]
    patches = (
        (start - span.start, end - span.start, replacement)
        for start, end, replacement in context.entity_patches
        if span.start <= start and end <= span.end
    )
    for start, end, replacement in sorted(patches, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def _indexed(raw: bytes, label: str) -> tuple[ParsedResource, ResourceIndex, dict[tuple[int, ...], Any]]:
    if not isinstance(raw, bytes):
        raise TypeError(f"{label} resource must be bytes")
    parsed = parse_resource(raw, "application/xhtml+xml")
    if parsed.tree is None or parsed.diagnostics:
        raise ValueError(f"{label} resource is not strict XHTML")
    indexed = index_resource(parsed)
    nodes = {element_path(node): node for node in parsed.tree.iter() if isinstance(node.tag, str)}
    return parsed, indexed, nodes


def _authorized(node: Any, kind: str) -> bool:
    name = qname_local_name(node.tag).lower()
    if kind == "block":
        return name in _BLOCK_TAGS and (name in _SEMANTIC_CODE or _preformatted_monospace(node.get("style", "")))
    return name in _INLINE_TAGS and (name == "code" or _monospace(node.get("style", "")))


def _preformatted_monospace(style: str) -> bool:
    values = _declarations(style)
    if values is None:
        return False
    whitespace = values.get("white-space")
    return whitespace is not None and _single_ident(whitespace) in _PRE and _family(values.get("font-family"))


def _monospace(style: str) -> bool:
    values = _declarations(style)
    return values is not None and _family(values.get("font-family"))


def _declarations(style: str) -> dict[str, Any] | None:
    values: dict[str, Any] = {}
    for declaration in tinycss2.parse_declaration_list(style, skip_comments=True, skip_whitespace=True):
        if declaration.type != "declaration" or declaration.lower_name in {"all", "font"}:
            return None
        prior = values.get(declaration.lower_name)
        if prior is None or declaration.important or not prior.important:
            values[declaration.lower_name] = declaration
    return values


def _single_ident(declaration: Any) -> str | None:
    tokens = [token for token in declaration.value if token.type not in {"whitespace", "comment"}]
    if len(tokens) != 1 or tokens[0].type != "ident" or tokens[0].value.lower() in _CSS_WIDE:
        return None
    return str(tokens[0].value).lower()


def _family(declaration: Any) -> bool:
    if declaration is None:
        return False
    groups: list[list[Any]] = [[]]
    for token in declaration.value:
        if token.type in {"whitespace", "comment"}:
            continue
        if token.type == "literal" and token.value == ",":
            if not groups[-1]:
                return False
            groups.append([])
        elif token.type in {"ident", "string"}:
            groups[-1].append(token)
        else:
            return False
    if not groups[-1]:
        return False
    for group in groups:
        identifiers = [str(token.value).lower() for token in group if token.type == "ident"]
        if any(value in _CSS_WIDE for value in identifiers):
            return False
        generic = [value for value in identifiers if value in _GENERIC_FAMILIES]
        if generic and (len(group) != 1 or len(generic) != 1):
            return False
    return any(
        len(group) == 1 and group[0].type == "ident" and group[0].value.lower() == "monospace" for group in groups
    )


def _frozen_attributes(attributes: Mapping[str, str], *, root: bool) -> dict[str, str]:
    allowed = TRANSLATABLE_ATTRIBUTES | {"style"}
    return {
        name: value
        for name, value in attributes.items()
        if qname_local_name(name).lower() not in allowed
        and name not in {"lang", "{http://www.w3.org/XML/1998/namespace}lang"}
    }


def _paths(paths: list[tuple[int, ...]]) -> None:
    if any(not path or any(type(index) is not int or index < 0 for index in path) for path in paths):
        raise ValueError("code region paths must contain non-negative element indexes")
    ordered = sorted(paths)
    for left, right in pairwise(ordered):
        if left == right:
            raise ValueError("code plan repeats a selected region")
        if _contains(left, right):
            raise ValueError("code plan regions overlap")


def _contains(root: tuple[int, ...], path: tuple[int, ...]) -> bool:
    return len(path) >= len(root) and path[: len(root)] == root


def _removable(node: Any) -> bool:
    return (
        qname_local_name(node.tag).lower() in {"span", "em", "strong"}
        and not node.attrib
        and not node.text
        and all(_removable(child) and not child.tail for child in node if isinstance(child.tag, str))
    )


def _canonical_path_map(
    context: _Context,
    missing: set[tuple[int, ...]],
) -> dict[tuple[int, ...], tuple[int, ...]]:
    if () in missing:
        raise ValueError("candidate cannot remove the source document root")
    result: dict[tuple[int, ...], tuple[int, ...]] = {(): ()}

    def visit(source_path: tuple[int, ...]) -> None:
        source_node = context.nodes[source_path]
        retained_index = 0
        for child in source_node:
            if not isinstance(child.tag, str):
                continue
            child_path = element_path(child)
            if child_path in missing:
                if not _removable(child) or any(_contains(child_path, path) for path in result):
                    raise ValueError("candidate deletion does not match an empty decorative subtree")
                continue
            target_path = result[source_path] + (retained_index,)
            result[child_path] = target_path
            retained_index += 1
            visit(child_path)

    visit(())
    if set(result) != set(context.nodes) - missing:
        raise ValueError("candidate deletion promotes or loses source descendants")
    return result


def _manifest_documents(archive: zipfile.ZipFile) -> tuple[str, ...]:
    from engine.epub.validation import (
        XHTML_MEDIA_TYPES,
        _container_rootfiles,
        _manifest_items,
        _read_xml,
    )

    entries = {item.filename: item.file_size for item in archive.infolist()}
    roots = _container_rootfiles(_read_xml(archive, "META-INF/container.xml"))
    if len(roots) != 1:
        raise ValueError("code discovery requires exactly one EPUB package")
    return tuple(
        sorted(
            item.path
            for item in _manifest_items(_read_xml(archive, roots[0]), roots[0], entries)
            if item.media_type in XHTML_MEDIA_TYPES
        )
    )


def _json_path(value: object) -> tuple[int, ...]:
    if not isinstance(value, list) or any(type(index) is not int or index < 0 for index in value):
        raise ValueError("persisted code path must contain non-negative integers")
    return tuple(value)


def _validate_path_map(paths: Mapping[tuple[int, ...], tuple[int, ...]]) -> None:
    if any(
        not isinstance(source, tuple)
        or not isinstance(target, tuple)
        or any(type(index) is not int or index < 0 for index in (*source, *target))
        for source, target in paths.items()
    ):
        raise ValueError("code node paths must contain non-negative integer tuples")
    if len(set(paths.values())) != len(paths):
        raise ValueError("code node mapping targets must be unique")


def _safe_resource(path: str) -> None:
    value = PurePosixPath(path)
    if not path or value.is_absolute() or ".." in value.parts or "\\" in path:
        raise ValueError(f"unsafe code resource path: {path!r}")


def _digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "CODE_PLAN_FORMAT",
    "CODE_POLICY",
    "apply_code_plan",
    "build_code_plan",
    "decode_code_maps",
    "discover_code_plan",
    "effective_code_plan",
    "encode_code_maps",
    "verify_code_plan",
]
