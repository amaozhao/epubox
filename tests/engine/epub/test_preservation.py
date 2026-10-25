from __future__ import annotations

import copy
import hashlib
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from engine.core.markup import element_path, parse_xml_bytes
from engine.epub.parsing import parse_resource
from engine.epub.preservation import (
    CODE_PLAN_FORMAT,
    CODE_POLICY,
    apply_code_plan,
    build_code_plan,
    decode_code_maps,
    discover_code_plan,
    effective_code_plan,
    encode_code_maps,
    verify_code_plan,
)
from engine.epub.ranges import index_resource
from engine.epub.upgrade import _normalize_xhtml
from engine.schemas.contracts import canonical_hash

XHTML = "http://www.w3.org/1999/xhtml"
RESOURCE = "OEBPS/chapter.xhtml"


def _markup() -> bytes:
    return (
        f'<html xmlns="{XHTML}" lang="en" xml:lang="en"><head><title>Rust</title></head><body>'
        '<p id="rust" style="white-space:pre-wrap;font-family:Menlo,Consolas,monospace">'
        '<span style="color:#a71d5d">pub</span> <span style="color:#795da3">fn</span> main() {\n'
        '    <span style="color:#a71d5d">if</span> count <span style="color:#333">/=</span> '
        '<span style="color:#0086b3">2</span>;\n}</p>'
        '<p id="logs" style="white-space:pre-wrap;font-family:monospace">INFO ready\nWARN retry</p>'
        '<p id="prose" title="English tooltip">Translate this ordinary paragraph.</p>'
        '<p id="inline">Use <span id="identifier" style="font-family:monospace">'
        "Result&lt;T&gt;<!-- exact comment --></span> in prose.</p>"
        '<p id="link"><a href="chapter2.xhtml#next">Next chapter</a></p>'
        "</body></html>"
    ).encode()


def _archive(path: Path, resource: bytes) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:identifier>urn:test</dc:identifier><dc:title>Test</dc:title><dc:language>en</dc:language>"
            '<meta property="dcterms:modified">2026-10-24T00:00:00Z</meta></metadata>'
            '<manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="chapter"/></spine></package>',
        )
        archive.writestr(RESOURCE, resource)
    return path


def _path_by_id(raw: bytes) -> dict[str, tuple[int, ...]]:
    parsed = parse_resource(raw, "application/xhtml+xml")
    assert parsed.tree is not None
    return {
        value: element_path(node)
        for node in parsed.tree.iter()
        if isinstance(node.tag, str) and (value := node.get("id"))
    }


def _selections(raw: bytes) -> dict[str, dict[tuple[int, ...], str]]:
    paths = _path_by_id(raw)
    return {RESOURCE: {paths["rust"]: "block", paths["logs"]: "block", paths["identifier"]: "inline"}}


def _candidate(raw: bytes) -> bytes:
    return (
        raw.replace(b'lang="en" xml:lang="en"', b'lang="zh-Hans" xml:lang="zh-Hans"')
        .replace(b"font-family:Menlo,Consolas,monospace", b"font-family:serif")
        .replace(b">pub<", b">PUBLIC<")
        .replace(b"INFO ready\nWARN retry", "信息 就绪\n警告 重试".encode())
        .replace(b"Translate this ordinary paragraph.", "翻译这个普通段落。".encode())
        .replace(b"English tooltip", "中文提示".encode())
        .replace(b"Result&lt;T&gt;", "结果&lt;T&gt;".encode())
        .replace(b"Use <span", "使用 <span".encode())
        .replace(b" in prose.", " 于正文中。".encode())
        .replace(b"Next chapter", "下一章".encode())
    )


def _node_bytes(raw: bytes, path: tuple[int, ...]) -> bytes:
    parsed = parse_resource(raw, "application/xhtml+xml")
    assert parsed.tree is not None
    span = index_resource(parsed).nodes[path].full
    return raw[span.start : span.end]


def test_source_bound_code_plan_restores_only_selected_regions_and_is_idempotent(tmp_path: Path) -> None:
    raw = _markup()
    source = _archive(tmp_path / "source.epub", raw)
    selections = _selections(raw)

    plan = build_code_plan(source, selections)
    again = build_code_plan(source, selections)
    first, stats = apply_code_plan(source, {RESOURCE: _candidate(raw)}, plan)
    second, second_stats = apply_code_plan(source, first, plan)

    assert CODE_POLICY == "source-code-v2"
    assert CODE_PLAN_FORMAT == "epubox-code-preservation-1"
    assert plan == again and canonical_hash(plan) == canonical_hash(again)
    assert plan["format"] == CODE_PLAN_FORMAT
    assert first == second and stats == second_stats
    assert stats["checked_resources"] == 1
    assert stats["selected_regions"] == 3 and stats["selected_bytes"] > 0
    paths = _path_by_id(raw)
    for node_id in ("rust", "logs", "identifier"):
        assert _node_bytes(first[RESOURCE], paths[node_id]) == _node_bytes(raw, paths[node_id])
    assert "翻译这个普通段落。".encode() in first[RESOURCE]
    assert "中文提示".encode() in first[RESOURCE]
    assert "使用 ".encode() in first[RESOURCE] and " 于正文中。".encode() in first[RESOURCE]
    assert "下一章".encode() in first[RESOURCE]
    assert b"<!-- exact comment -->" in first[RESOURCE]

    output = _archive(tmp_path / "output.epub", first[RESOURCE])
    assert verify_code_plan(source, output, plan) == stats


@pytest.mark.parametrize("field", ["source_hash", "resource_hash", "region_hash"])
def test_code_plan_rejects_source_and_plan_tampering(tmp_path: Path, field: str) -> None:
    raw = _markup()
    source = _archive(tmp_path / "source.epub", raw)
    plan = build_code_plan(source, _selections(raw))
    tampered = copy.deepcopy(plan)
    if field == "source_hash":
        tampered["source_hash"] = "0" * 64
    elif field == "resource_hash":
        tampered["resources"][RESOURCE]["source_hash"] = "0" * 64
    else:
        tampered["resources"][RESOURCE]["regions"][0]["sha256"] = "0" * 64

    with pytest.raises(ValueError):
        apply_code_plan(source, {RESOURCE: _candidate(raw)}, tampered)


def test_code_plan_rejects_topology_and_frozen_attribute_changes(tmp_path: Path) -> None:
    raw = _markup()
    source = _archive(tmp_path / "source.epub", raw)
    plan = build_code_plan(source, _selections(raw))

    with pytest.raises(ValueError, match="topology"):
        apply_code_plan(source, {RESOURCE: _candidate(raw).replace(b"</span> main", b" main", 1)}, plan)
    with pytest.raises(ValueError, match="attribute"):
        apply_code_plan(source, {RESOURCE: _candidate(raw).replace(b'id="prose"', b'id="changed"')}, plan)
    with pytest.raises(ValueError, match="attribute"):
        apply_code_plan(source, {RESOURCE: _candidate(raw).replace(b'id="rust"', b'id="changed"')}, plan)
    with pytest.raises(ValueError, match="attribute"):
        apply_code_plan(source, {RESOURCE: _candidate(raw).replace(b'id="identifier"', b'id="changed"')}, plan)
    with pytest.raises(ValueError, match="attribute"):
        apply_code_plan(
            source,
            {RESOURCE: _candidate(raw).replace(b'href="chapter2.xhtml#next"', b'href="other.xhtml"')},
            plan,
        )


def test_code_plan_rejects_unauthorized_or_overlapping_source_regions(tmp_path: Path) -> None:
    raw = _markup()
    source = _archive(tmp_path / "source.epub", raw)
    paths = _path_by_id(raw)

    with pytest.raises(ValueError):
        build_code_plan(source, {RESOURCE: {paths["prose"]: "block"}})
    with pytest.raises(ValueError):
        build_code_plan(source, {RESOURCE: {paths["inline"]: "inline"}})
    with pytest.raises(ValueError, match="overlap"):
        build_code_plan(source, {RESOURCE: {paths["inline"]: "block", paths["identifier"]: "inline"}})
    with pytest.raises(ValueError):
        build_code_plan(source, {RESOURCE: {paths["rust"]: "unsupported"}})


def test_code_plan_rejects_duplicate_regions_and_verify_detects_changed_code(tmp_path: Path) -> None:
    raw = _markup()
    source = _archive(tmp_path / "source.epub", raw)
    plan = build_code_plan(source, _selections(raw))
    duplicate = copy.deepcopy(plan)
    duplicate["resources"][RESOURCE]["regions"].append(copy.deepcopy(duplicate["resources"][RESOURCE]["regions"][0]))
    with pytest.raises(ValueError):
        apply_code_plan(source, {RESOURCE: _candidate(raw)}, duplicate)

    changed = _archive(tmp_path / "changed.epub", _candidate(raw))
    with pytest.raises(ValueError, match="selected"):
        verify_code_plan(source, changed, plan)


def test_code_plan_is_bound_to_the_exact_source_archive(tmp_path: Path) -> None:
    raw = _markup()
    source = _archive(tmp_path / "source.epub", raw)
    plan = build_code_plan(source, _selections(raw))
    changed_source = _archive(tmp_path / "other.epub", raw.replace(b"<title>Rust</title>", b"<title>Other</title>"))

    assert hashlib.sha256(source.read_bytes()).hexdigest() == plan["source_hash"]
    with pytest.raises(ValueError, match="source"):
        apply_code_plan(changed_source, {RESOURCE: _candidate(raw)}, plan)


def test_code_plan_helpers_never_construct_a_model_runtime(tmp_path: Path) -> None:
    raw = _markup()
    source = _archive(tmp_path / "source.epub", raw)

    with patch(
        "engine.agents.runtime.ModelRuntime.__init__",
        side_effect=AssertionError("code preservation must remain model-free"),
    ):
        plan = build_code_plan(source, _selections(raw))
        restored, _stats = apply_code_plan(source, {RESOURCE: _candidate(raw)}, plan)

    assert b">pub<" in restored[RESOURCE]


def test_code_plan_handles_many_canonical_sibling_regions(tmp_path: Path) -> None:
    count = 1_024
    raw = (
        f'<html xmlns="{XHTML}"><head><title>Logs</title></head><body>'
        + "".join(
            f'<p id="log-{index}" style="white-space:pre-wrap;font-family:monospace">line {index}</p>'
            for index in range(count)
        )
        + "</body></html>"
    ).encode()
    source = _archive(tmp_path / "many.epub", raw)
    paths = _path_by_id(raw)

    plan = build_code_plan(
        source,
        {RESOURCE: {paths[f"log-{index}"]: "block" for index in range(count)}},
    )

    assert len(plan["resources"][RESOURCE]["regions"]) == count


def test_automatic_plan_discovers_semantic_inline_and_linked_css_code(tmp_path: Path) -> None:
    raw = (
        f'<html xmlns="{XHTML}"><head><title>Code</title>'
        '<link rel="stylesheet" href="code"/></head><body>'
        '<pre id="semantic">  exact\n  indent</pre>'
        '<p class="listing" id="listing">let value = 1;</p>'
        '<p>Use <code id="literal">Result&lt;T&gt;</code> and '
        '<span class="literal" id="styled">OutPoint</span>.</p>'
        '<p id="prose">Translate prose.</p></body></html>'
    ).encode()
    source = tmp_path / "source.epub"
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:identifier>urn:test</dc:identifier><dc:title>Test</dc:title><dc:language>en</dc:language>"
            '<meta property="dcterms:modified">2026-10-24T00:00:00Z</meta></metadata>'
            '<manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="css" href="code" media-type="text/css"/></manifest>'
            '<spine><itemref idref="chapter"/></spine></package>',
        )
        archive.writestr(RESOURCE, raw)
        archive.writestr(
            "OEBPS/code",
            ".listing { white-space: pre-wrap; font-family: Menlo, monospace } .literal { font-family: monospace }",
        )

    plan = discover_code_plan(source)

    assert plan is not None and plan["format"] == CODE_PLAN_FORMAT
    paths = _path_by_id(raw)
    selected = {(tuple(region["path"]), region["kind"]) for region in plan["resources"][RESOURCE]["regions"]}
    assert selected == {
        (paths["semantic"], "block"),
        (paths["listing"], "block"),
        (paths["literal"], "inline"),
        (paths["styled"], "inline"),
    }
    assert paths["prose"] not in {path for path, _kind in selected}


def test_automatic_plan_returns_none_when_source_has_no_code(tmp_path: Path) -> None:
    raw = f'<html xmlns="{XHTML}"><head><title>Book</title></head><body><p>Ordinary prose.</p></body></html>'.encode()
    source = _archive(tmp_path / "source.epub", raw)

    assert discover_code_plan(source) is None


def test_verification_accepts_only_approved_empty_decoration_deletions_before_code(tmp_path: Path) -> None:
    raw = (
        f'<html xmlns="{XHTML}"><head><title>Code</title></head><body>'
        '<p id="prose">中文<span></span>正文</p>'
        '<pre id="code">  let value = 1;\n</pre></body></html>'
    ).encode()
    source = _archive(tmp_path / "source.epub", raw)
    plan = discover_code_plan(source)
    assert plan is not None
    candidate = raw.replace(b"<span></span>", b"").replace(b"let value = 1", b"let changed = 2")
    candidate = candidate.replace(b"let changed = 2", b"let value = 1")
    output = _archive(tmp_path / "output.epub", candidate)
    source_parsed = parse_resource(raw, "application/xhtml+xml")
    target_parsed = parse_resource(candidate, "application/xhtml+xml")
    assert source_parsed.tree is not None and target_parsed.tree is not None
    source_nodes = {element_path(node): node for node in source_parsed.tree.iter() if isinstance(node.tag, str)}
    target_nodes = {element_path(node): node for node in target_parsed.tree.iter() if isinstance(node.tag, str)}
    retained = [path for path, node in source_nodes.items() if not (node.tag.endswith("}span") and not node.attrib)]
    mapping = dict(zip(retained, target_nodes, strict=True))

    stats = verify_code_plan(source, output, plan, node_maps={RESOURCE: mapping})

    assert stats["selected_regions"] == 1
    bad = _archive(tmp_path / "bad.epub", candidate.replace(b'id="prose"', b'id="changed"'))
    with pytest.raises(ValueError, match="attribute|mapping"):
        verify_code_plan(source, bad, plan, node_maps={RESOURCE: mapping})


def test_code_node_maps_round_trip_canonically_and_reject_swapped_siblings(tmp_path: Path) -> None:
    raw = (
        f'<html xmlns="{XHTML}"><head><title>Code</title></head><body>'
        "<pre>first()</pre><pre>second()</pre></body></html>"
    ).encode()
    source = _archive(tmp_path / "source.epub", raw)
    plan = discover_code_plan(source)
    assert plan is not None
    parsed = parse_resource(raw, "application/xhtml+xml")
    assert parsed.tree is not None
    paths = [element_path(node) for node in parsed.tree.iter() if isinstance(node.tag, str)]
    identity = {path: path for path in paths}
    encoded = encode_code_maps({RESOURCE: identity})

    assert decode_code_maps(encoded) == {RESOURCE: identity}
    output = _archive(
        tmp_path / "output.epub", raw.replace(b"first()", b"second()").replace(b"second()", b"first()", 1)
    )
    first, second = tuple(tuple(region["path"]) for region in plan["resources"][RESOURCE]["regions"])
    swapped = identity | {first: second, second: first}
    with pytest.raises(ValueError, match="canonical order-preserving"):
        verify_code_plan(source, output, plan, node_maps={RESOURCE: swapped})
    with pytest.raises(ValueError):
        decode_code_maps({RESOURCE: [[list(first), list(second)], [list(first), list(first)]]})


def test_effective_plan_never_omits_automatic_code_and_rejects_stale_explicit_plan(tmp_path: Path) -> None:
    raw = _markup()
    source = _archive(tmp_path / "source.epub", raw)
    paths = _path_by_id(raw)
    explicit = build_code_plan(source, {RESOURCE: {paths["logs"]: "block"}})

    plan = effective_code_plan(source, explicit)

    assert plan is not None
    selected = {tuple(region["path"]) for region in plan["resources"][RESOURCE]["regions"]}
    assert {paths["rust"], paths["logs"], paths["identifier"]} <= selected
    stale = copy.deepcopy(explicit)
    stale["source_hash"] = "0" * 64
    with pytest.raises(ValueError, match="source"):
        effective_code_plan(source, stale)


def test_code_overlay_accepts_lossless_dtd_entity_normalization(tmp_path: Path) -> None:
    raw = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
        b'"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Code</title></head><body>'
        b'<pre title="A &copy; B">  &copy; code&nbsp;indent &amp;copy; &#169; &amp;\n'
        b"<![CDATA[literal &copy;]]><!-- literal &nbsp; --><?keep &copy;?></pre>"
        b"</body></html>"
    )
    source = _archive(tmp_path / "source.epub", raw)
    plan = discover_code_plan(source)
    assert plan is not None
    normalized = _normalize_xhtml(raw)

    restored, stats = apply_code_plan(source, {RESOURCE: normalized}, plan)

    result = restored[RESOURCE]
    assert stats["selected_regions"] == 1
    assert b"<!DOCTYPE html>" in result
    assert b' title="A &#169; B"' in result
    assert b"  &#169; code&#160;indent &amp;copy; &#169; &amp;\n" in result
    assert b"<![CDATA[literal &copy;]]>" in result
    assert b"<!-- literal &nbsp; -->" in result
    assert b"<?keep &copy;?>" in result
    assert tuple(parse_xml_bytes(result).getroot().itertext()) == tuple(parse_xml_bytes(raw).getroot().itertext())

    output = _archive(tmp_path / "output.epub", result)
    assert verify_code_plan(source, output, plan) == stats
