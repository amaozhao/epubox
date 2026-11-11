import pytest
from lxml import etree  # type: ignore[attr-defined]

from engine.epub.fill import fill_resource
from engine.item.atoms import extract_resource
from engine.item.inline import validate_projection
from engine.item.members import materialize_members, merge_member_targets
from engine.schemas.budget import BudgetLimits
from engine.schemas.contracts import ItemRecord, canonical_hash
from engine.services.lists import validate_list_pieces
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


def test_nested_list_after_inline_code_splits_inside_protected_text_domain() -> None:
    body = (
        '<ol start="3"><li>Start <code>fixed_code</code> details<ol>'
        + "".join(f'<li id="step-{number}">{"word " * 650}</li>' for number in range(3))
        + "</ol></li></ol>"
    )
    raw = f'<html xmlns="{XHTML}"><head/><body>{body}</body></html>'.encode()
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    limits = BudgetLimits(
        source_tokens=1500,
        source_hard_limit=1500,
        input_tokens=50_000,
        output_tokens=None,
        context_tokens=60_000,
        output_version=7,
    )
    report = preflight_atomic_resources((inventory,), {"OPS/chapter.xhtml": raw}, limits, "gpt-3.5-turbo")

    assert report.passed
    assert report.diagnostics[0].status == "split"
    assert all(piece.translate.fits and piece.review.fits for piece in report.pieces)
    assert all(piece.translate.source_tokens <= 1500 for piece in report.pieces)
    members = materialize_members((inventory,), report)
    assert len(members) > 1
    assert any("⟦+b" in member.source_prefix for member in members)
    parent = inventory.items[0]
    identity_records = {
        member.item_id: ItemRecord(
            item_id=member.item_id,
            segment_id=member.item_id,
            terms_hash="terms",
            context_hash="context",
            target_projection=member.source_projection,
            target_hash=canonical_hash(member.source_projection),
        )
        for member in members
    }
    merged_source = merge_member_targets(parent, members, identity_records)
    assert merged_source == parent.source_projection
    assert fill_resource(raw, inventory, {parent.item_id: merged_source}, identity=True) == raw
    records = {
        member.item_id: identity_records[member.item_id].model_copy(
            update={
                "target_projection": (target := member.source_projection.replace("word", "文字")),
                "target_hash": canonical_hash(target),
            }
        )
        for member in members
    }
    merged = merge_member_targets(parent, members, records)
    result = fill_resource(raw, inventory, {parent.item_id: merged})
    assert b"<code>fixed_code</code>" in result
    assert b' start="3"' in result
    assert [node.get("id") for node in etree.fromstring(result).findall(".//{*}li[@id]")] == [
        f"step-{number}" for number in range(3)
    ]
    assert b"word" not in result and "文字".encode() in result


def test_successful_legacy_nested_pieces_keep_their_identities(monkeypatch) -> None:
    from engine.services import lists

    body = (
        "<ol><li>Part<ol>"
        + f"<li>{'word ' * 600}</li>" * 2
        + "<li>Start <code>fixed_code</code> Details<ol>"
        + f"<li>{'word ' * 200}</li>" * 3
        + "</ol></li></ol></li></ol>"
    )
    raw = f'<html xmlns="{XHTML}"><head/><body>{body}</body></html>'.encode()
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    limits = BudgetLimits(
        source_tokens=1500,
        source_hard_limit=1500,
        input_tokens=50_000,
        output_tokens=None,
        context_tokens=60_000,
        output_version=7,
    )
    layout = lists._nested_layout
    with monkeypatch.context() as legacy:
        legacy.setattr(lists, "_nested_layout", lambda parent, **kwargs: layout(parent, protected_domains=False))
        previous = preflight_atomic_resources((inventory,), {"OPS/chapter.xhtml": raw}, limits, "gpt-3.5-turbo")
    assert previous.passed and previous.diagnostics[0].status == "split"
    current = preflight_atomic_resources((inventory,), {"OPS/chapter.xhtml": raw}, limits, "gpt-3.5-turbo")
    assert current == previous
    materialize_members((inventory,), current)


def nested_directory(tag):
    entries = "".join(
        f'<ol><li><a href="chapter.xhtml#s{number}">Section {number} {"word " * 450}</a>'
        f'<span class="page">{number + 1}</span></li></ol>'
        for number in range(6)
    )
    body = f'<{tag} start="3"><li><span>Part One</span>{entries}</li></{tag}>'
    raw = f'<html xmlns="{XHTML}"><head/><body>{body}</body></html>'.encode()
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    limits = BudgetLimits(
        source_tokens=1500,
        source_hard_limit=1500,
        input_tokens=50_000,
        output_tokens=None,
        context_tokens=60_000,
        output_version=7,
    )
    report = preflight_atomic_resources((inventory,), {"OPS/chapter.xhtml": raw}, limits, "gpt-3.5-turbo")
    assert report.passed
    return raw, inventory, report


@pytest.mark.parametrize("tag", ["ol", "ul"])
def test_nested_directory_under_one_outer_item_splits_and_preserves_links(tag):
    raw, inventory, report = nested_directory(tag)
    members = materialize_members((inventory,), report)
    assert len(members) > 1
    assert all(piece.translate.fits and piece.review.fits for piece in report.pieces)
    records = {}
    for member in members:
        target = member.source_projection.replace("word", "文字").replace("Part One", "第一部分")
        validate_projection(member, target)
        records[member.item_id] = ItemRecord(
            item_id=member.item_id,
            segment_id=member.item_id,
            terms_hash="terms",
            context_hash="context",
            target_projection=target,
            target_hash=canonical_hash(target),
        )
    parent = inventory.items[0]
    merged = merge_member_targets(parent, members, records)
    result = fill_resource(raw, inventory, {parent.item_id: merged})
    root = etree.fromstring(result)
    links = root.findall(".//{*}a")
    assert [link.get("href") for link in links] == [f"chapter.xhtml#s{number}" for number in range(6)]
    assert [span.text for span in root.findall('.//{*}span[@class="page"]')] == [str(n + 1) for n in range(6)]
    assert root.find(f".//{{{XHTML}}}{tag}").get("start") == "3"
    assert len(root.findall(f".//{{{XHTML}}}{tag}")) == (7 if tag == "ol" else 1)
    assert b"word" not in result and "第一部分".encode() in result


@pytest.mark.parametrize("damage", ["prefix", "suffix", "bytes", "missing", "duplicate"])
def test_nested_list_rejects_changed_wrappers_bytes_and_coverage(damage):
    _raw, inventory, report = nested_directory("ol")
    pieces = list(report.pieces)
    if damage == "prefix":
        pieces[1] = pieces[1].model_copy(update={"source_prefix": ""})
    elif damage == "suffix":
        pieces[0] = pieces[0].model_copy(update={"source_suffix": ""})
    elif damage == "bytes":
        span = pieces[0].source_span
        pieces[0] = pieces[0].model_copy(
            update={"source_span": span.model_copy(update={"byte_end": span.byte_end + 1})}
        )
    elif damage == "missing":
        pieces.pop()
    else:
        pieces.insert(1, pieces[0])
    with pytest.raises(ValueError):
        validate_list_pieces(inventory.items[0], pieces, node_spans=inventory.source_map.node_spans)
