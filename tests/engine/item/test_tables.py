from __future__ import annotations

import pytest
from lxml import etree  # type: ignore[attr-defined]

from engine.epub.fill import fill_resource
from engine.item.atoms import extract_resource
from engine.item.inline import parse_projection, plain_text, validate_projection
from engine.item.members import materialize_members, merge_member_targets
from engine.schemas.budget import BudgetLimits
from engine.schemas.contracts import ItemRecord, canonical_hash
from engine.services.preflight import preflight_atomic_resources
from engine.services.tables import unpad, validate_table_pieces

XHTML = "http://www.w3.org/1999/xhtml"
PATH = "OPS/chapter.xhtml"
MODEL = "gpt-3.5-turbo"


def source(body: str) -> bytes:
    return f'<html xmlns="{XHTML}"><head/><body>{body}</body></html>'.encode()


def limits() -> BudgetLimits:
    return BudgetLimits(
        source_tokens=1500,
        source_hard_limit=1500,
        input_tokens=50_000,
        output_tokens=None,
        context_tokens=60_000,
        output_version=7,
    )


def preflight(body: str):
    raw = source(body)
    inventory = extract_resource(raw, PATH, "book")
    report = preflight_atomic_resources((inventory,), {PATH: raw}, limits(), MODEL)
    return raw, inventory, report


def record(item_id: str, target: str) -> ItemRecord:
    return ItemRecord(
        item_id=item_id,
        segment_id=item_id,
        terms_hash="terms",
        context_hash="context",
        target_projection=target,
        target_hash=canonical_hash(target),
    )


def long_cell(label: str, words: int = 500) -> str:
    return f"{label} " + "word " * words


def test_direct_tr_table_splits_only_after_complete_outer_rows() -> None:
    body = "<table>" + "".join(f"<tr><td>{long_cell(f'Row {number}')}</td></tr>" for number in range(4)) + "</table>"
    raw, inventory, report = preflight(body)
    parent = inventory.items[0]

    assert report.check is not None
    assert report.diagnostics[0].status == "split"
    assert len(report.pieces) == 2
    assert all(not piece.source_prefix and not piece.source_suffix for piece in report.pieces)
    assert all(piece.translate.source_tokens <= 1500 for piece in report.pieces)
    assert "Row 0" in plain_text(report.pieces[0].source_projection)
    assert "Row 1" in plain_text(report.pieces[0].source_projection)
    assert "Row 2" not in plain_text(report.pieces[0].source_projection)
    assert "Row 2" in plain_text(report.pieces[1].source_projection)
    assert "Row 3" in plain_text(report.pieces[1].source_projection)
    assert raw[parent.source_span.byte_start : report.pieces[0].source_span.byte_end].endswith(b"</tr>")
    assert "".join(unpad(piece, piece.source_projection) for piece in report.pieces) == parent.source_projection
    validate_table_pieces(parent, report.pieces)


def test_row_group_padding_preserves_caption_colgroup_sections_and_multiple_tbodies() -> None:
    body = (
        "<table><caption>Summary</caption><colgroup><col/><col/></colgroup>"
        f"<thead><tr><th>{long_cell('Heading', 450)}</th><th>Kind</th></tr></thead>"
        f'<tbody><tr><td rowspan="2">{long_cell("Alpha", 450)}</td><td>One</td></tr>'
        f'<tr><td colspan="2">{long_cell("Beta", 450)}</td></tr></tbody>'
        f"<tbody><tr><td>{long_cell('Gamma', 450)}</td><td>Three</td></tr></tbody>"
        f"<tfoot><tr><td>{long_cell('Total', 450)}</td><td>Four</td></tr></tfoot></table>"
    )
    _raw, inventory, report = preflight(body)
    parent = inventory.items[0]
    section_refs = {
        ref for ref, entry in parent.registry.items() if entry.hints.get("element") in {"thead", "tbody", "tfoot"}
    }

    assert report.check is not None and report.diagnostics[0].status == "split"
    assert len(report.pieces) > 1
    assert any(piece.source_prefix or piece.source_suffix for piece in report.pieces)
    for piece in report.pieces:
        validate_projection(piece.source_projection)
        padding_refs = {
            event.value[1:]
            for padding in (piece.source_prefix, piece.source_suffix)
            for event in parse_projection(padding)
            if event.kind == "marker"
        }
        assert padding_refs <= section_refs
    assert "".join(unpad(piece, piece.source_projection) for piece in report.pieces) == parent.source_projection
    assert "Summary" in plain_text(report.pieces[0].source_projection)
    validate_table_pieces(parent, report.pieces)


def test_nested_table_rows_and_protected_code_never_become_outer_cut_points() -> None:
    nested = (
        "<table><tr><td>Inner one</td></tr><tr><td>Inner two</td></tr></table>"
        "<pre><code>if value &lt; 2:\n  return value</code></pre>"
    )
    body = (
        f"<table><tr><td>{long_cell('Outer 1')}{nested}</td></tr>"
        + "".join(f"<tr><td>{long_cell(f'Outer {number}')}</td></tr>" for number in range(2, 5))
        + "</table>"
    )
    _raw, inventory, report = preflight(body)
    parent = inventory.items[0]
    outer_rows = {
        ref
        for ref, entry in parent.registry.items()
        if entry.hints.get("element") == "tr" and entry.parent_ref == parent.node_key
    }
    inner_rows = {
        ref
        for ref, entry in parent.registry.items()
        if entry.hints.get("element") == "tr" and entry.parent_ref != parent.node_key
    }

    assert report.check is not None and len(report.pieces) == 2
    assert len(outer_rows) == 4 and len(inner_rows) == 2
    piece_refs = [
        {
            event.value[1:]
            for event in parse_projection(piece.source_projection)
            if event.kind == "marker" and event.value.startswith("+g")
        }
        for piece in report.pieces
    ]
    assert all(sum(ref in refs for refs in piece_refs) == 1 for ref in outer_rows)
    assert all(ref in piece_refs[0] and ref not in piece_refs[1] for ref in inner_rows)
    validate_table_pieces(parent, report.pieces)


def test_single_oversized_row_is_blocked_instead_of_split_midrow() -> None:
    _raw, inventory, report = preflight(f"<table><tr><td>{long_cell('Only row', 2000)}</td></tr></table>")

    assert report.check is None
    assert report.pieces == ()
    assert report.diagnostics[0].item_id == inventory.items[0].item_id
    assert report.diagnostics[0].status == "blocked"
    assert report.diagnostics[0].source_tokens > 1500
    assert any("source budget" in failure for failure in report.diagnostics[0].failures)


def test_table_within_budget_stays_one_whole_unpadded_member() -> None:
    _raw, inventory, report = preflight(
        "<table><thead><tr><th>Name</th></tr></thead>"
        "<tbody><tr><td>Alpha</td></tr><tr><td>Beta</td></tr></tbody></table>"
    )
    parent = inventory.items[0]

    assert report.check is not None
    assert report.diagnostics[0].status == "passed"
    assert len(report.pieces) == 1
    assert report.pieces[0].piece_id == parent.item_id
    assert report.pieces[0].source_projection == parent.source_projection
    assert not report.pieces[0].source_prefix and not report.pieces[0].source_suffix


def test_materialize_merge_and_fill_roundtrip_complex_split_table() -> None:
    code = b"<pre><code>if value &lt; 2:\n  return value</code></pre>"
    body = (
        "<table><caption>English summary</caption><colgroup><col/><col/></colgroup>"
        f"<thead><tr><th>{long_cell('English heading', 450)}</th><th>Type</th></tr></thead>"
        f'<tbody><tr><td rowspan="2">{long_cell("English alpha", 450)}{code.decode()}</td><td>One</td></tr>'
        f'<tr><td colspan="2">{long_cell("English beta", 450)}</td></tr></tbody>'
        f"<tbody><tr><td>{long_cell('English gamma', 450)}</td><td>Three</td></tr></tbody>"
        f"<tfoot><tr><td>{long_cell('English total', 450)}</td><td>Four</td></tr></tfoot></table>"
    )
    raw, inventory, report = preflight(body)
    parent = inventory.items[0]
    members = materialize_members((inventory,), report)

    identity_records = {member.item_id: record(member.item_id, member.source_projection) for member in members}
    identity = merge_member_targets(parent, members, identity_records)
    assert identity == parent.source_projection
    assert fill_resource(raw, inventory, {parent.item_id: identity}, identity=True) == raw

    translated_records = {
        member.item_id: record(member.item_id, member.source_projection.replace("English", "中文"))
        for member in members
    }
    translated = merge_member_targets(parent, members, translated_records)
    rendered = fill_resource(raw, inventory, {parent.item_id: translated})
    root = etree.fromstring(rendered)

    assert len(root.xpath("//x:body/x:table", namespaces={"x": XHTML})) == 1
    assert len(root.xpath("//x:body/x:table/x:tbody", namespaces={"x": XHTML})) == 2
    assert len(root.xpath("//x:body/x:table//x:tr", namespaces={"x": XHTML})) == 5
    assert root.xpath("string(//x:td[@rowspan='2'])", namespaces={"x": XHTML}).startswith("中文 alpha")
    assert root.xpath("string(//x:td[@colspan='2'])", namespaces={"x": XHTML}).startswith("中文 beta")
    assert code in rendered
    assert "中文 summary".encode() in rendered


def test_table_piece_validation_rejects_changed_padding_and_midrow_forgery() -> None:
    grouped_body = (
        "<table><thead><tr><th>Head</th></tr></thead><tbody>"
        + "".join(f"<tr><td>{long_cell(f'Row {number}', 600)}</td></tr>" for number in range(4))
        + "</tbody></table>"
    )
    _raw, inventory, report = preflight(grouped_body)
    parent = inventory.items[0]
    assert report.check is not None and len(report.pieces) > 1
    padded_index = next(
        index for index, piece in enumerate(report.pieces) if piece.source_prefix or piece.source_suffix
    )
    padded = report.pieces[padded_index]
    forged_padding = padded.model_copy(
        update={"source_prefix": "" if padded.source_prefix else "\u27e6+gforged\u27e7"}
    )
    changed = list(report.pieces)
    changed[padded_index] = forged_padding

    with pytest.raises(ValueError, match="prefix differs|virtual wrappers changed"):
        validate_table_pieces(parent, changed)

    direct_body = (
        "<table>"
        + "".join(f"<tr><td>{long_cell(f'Direct {number}', 500)}</td></tr>" for number in range(4))
        + "</table>"
    )
    _raw, direct_inventory, direct_report = preflight(direct_body)
    direct_parent = direct_inventory.items[0]
    assert direct_report.check is not None and len(direct_report.pieces) == 2
    cut = direct_parent.source_projection.index("Direct 1") + len("Direct 1")
    forged_midrow = (
        direct_report.pieces[0].model_copy(update={"source_projection": direct_parent.source_projection[:cut]}),
        direct_report.pieces[1].model_copy(update={"source_projection": direct_parent.source_projection[cut:]}),
    )

    with pytest.raises(ValueError, match="cut only between complete outer rows"):
        validate_table_pieces(direct_parent, forged_midrow)
