import pytest
from lxml import etree  # type: ignore[attr-defined]

from engine.epub.fill import fill_resource
from engine.item.atoms import extract_resource
from engine.item.members import materialize_members, merge_member_targets
from engine.schemas.budget import BudgetLimits
from engine.schemas.contracts import ItemRecord, canonical_hash
from engine.services.preflight import preflight_atomic_resources

XHTML = "http://www.w3.org/1999/xhtml"


@pytest.mark.parametrize("tag", ["ul", "ol"])
def test_oversized_list_splits_only_between_complete_li_and_roundtrips(tag: str) -> None:
    long = "word " * 700
    body = (
        f"<{tag}><li><p>{long}</p><ol><li>Nested <em>detail</em>.</li></ol></li>"
        f"<li>{long}</li><li>{long}<pre><code>def f():\n return 1</code></pre></li>"
        f"<li>{long}</li><li>{long}</li><li>{long}</li></{tag}>"
    )
    raw = f'<html xmlns="{XHTML}"><head/><body>{body}</body></html>'.encode()
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    limits = BudgetLimits(
        source_tokens=2000,
        source_tolerance_tokens=1000,
        input_tokens=50_000,
        output_tokens=8192,
        context_tokens=60_000,
        context_unlimited=True,
        output_version=6,
    )

    report = preflight_atomic_resources((inventory,), {"OPS/chapter.xhtml": raw}, limits, "gpt-3.5-turbo")
    parent = inventory.items[0]
    members = materialize_members((inventory,), report)

    assert report.check is not None and report.diagnostics[0].status == "split"
    assert parent.atomic_tag == tag and len(members) > 1
    assert all(member.atomic_tag == tag and member.piece_count == len(members) for member in members)
    assert all(member.source_projection.count("⟦+g") == member.source_projection.count("⟦-g") for member in members)
    records = {
        member.item_id: ItemRecord(
            item_id=member.item_id,
            segment_id=member.item_id,
            terms_hash="terms",
            context_hash="context",
            target_projection=(target := member.source_projection.replace("word", "文字")),
            target_hash=canonical_hash(target),
        )
        for member in members
    }
    merged = merge_member_targets(parent, members, records)
    rendered = fill_resource(raw, inventory, {parent.item_id: merged})
    root = etree.fromstring(rendered)

    assert len(root.xpath(f"//x:body/x:{tag}/x:li", namespaces={"x": XHTML})) == 6
    assert len(root.xpath(f"//x:body/x:{tag}/x:li/x:ol/x:li", namespaces={"x": XHTML})) == 1
    assert b"<pre><code>def f():\n return 1</code></pre>" in rendered
    assert b"word" not in rendered and "文字".encode() in rendered


def test_oversized_paragraph_remains_blocked() -> None:
    raw = f'<html xmlns="{XHTML}"><head/><body><p>{"word " * 3000}</p></body></html>'.encode()
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    limits = BudgetLimits(source_tokens=2000, input_tokens=50_000, output_tokens=10_000, context_tokens=60_000)

    report = preflight_atomic_resources((inventory,), {"OPS/chapter.xhtml": raw}, limits, "gpt-3.5-turbo")

    assert report.check is None
    assert report.diagnostics[0].atomic_tag == "p" and report.diagnostics[0].status == "blocked"
    assert not report.pieces


def test_one_oversized_li_blocks_the_whole_list() -> None:
    raw = (
        f'<html xmlns="{XHTML}"><head/><body><ul><li>{"word " * 3000}</li><li>Short.</li></ul></body></html>'.encode()
    )
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    limits = BudgetLimits(source_tokens=2000, input_tokens=50_000, output_tokens=10_000, context_tokens=60_000)

    report = preflight_atomic_resources((inventory,), {"OPS/chapter.xhtml": raw}, limits, "gpt-3.5-turbo")

    assert report.check is None
    assert report.diagnostics[0].atomic_tag == "ul" and report.diagnostics[0].status == "blocked"
    assert not report.pieces
