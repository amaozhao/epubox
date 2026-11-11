from __future__ import annotations

import asyncio
import codecs
import hashlib
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from lxml import etree  # type: ignore[attr-defined]

from engine import cli
from engine.epub.fill import fill_resource
from engine.item.atoms import extract_resource
from engine.item.attributes import attribute_boundaries
from engine.item.budget import request_source_tokens
from engine.item.members import materialize_members, merge_member_targets
from engine.schemas.budget import BudgetLimits
from engine.services import preflight, state, tables
from engine.services.journal import BodyJournal
from engine.services.preflight import preflight_atomic_resources, receipt_path
from engine.services.preparation import prepare_translation, resume_preparation
from engine.services.ready import ReadySession, limits_from_config
from engine.services.store import RunStore
from tests.engine.epub.test_factory import make_epub
from tests.engine.epub.test_preparation import StubChecker
from tests.engine.execution.test_atomic import answer
from tests.engine.item.test_members import record
from tests.engine.services.test_preparing import atomic_config, forbidden


@pytest.mark.parametrize("attribute", ["alt", "title", "aria-label", "aria-description", "content"])
@pytest.mark.parametrize(
    "encoding,bom",
    [
        ("utf-8", b""),
        ("utf-8", codecs.BOM_UTF8),
        ("utf-16-le", codecs.BOM_UTF16_LE),
        ("utf-16-be", codecs.BOM_UTF16_BE),
    ],
)
def test_long_attribute_splits_with_exact_source_coverage_and_escaped_writeback(attribute, encoding, bom):
    value = (
        "upper Q left-parenthesis &amp; e\u0301 👩\u200d🔬 &#x20; " * 25
        + "literal ⟦=x1⟧ "
        + "right-parenthesis upper M " * 25
    )
    node = (
        f'<meta name="description" content="{value}"/>'
        if attribute == "content"
        else f'<img src="formula.png" {attribute}="{value}"/>'
    )
    markup = (
        f'<?xml version="1.0" encoding="{encoding}"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
        + (node if attribute == "content" else "")
        + "</head><body><p>Body.</p>"
        + (node if attribute != "content" else "")
        + "</body></html>"
    )
    raw = bom + markup.encode(encoding)
    atoms = extract_resource(raw, "OPS/chapter.xhtml", "book")
    capacity = BudgetLimits(
        source_tokens=100,
        source_hard_limit=1500,
        input_tokens=50000,
        output_tokens=None,
        output_version=7,
        context_tokens=50256,
    )
    report = preflight_atomic_resources((atoms,), {"OPS/chapter.xhtml": raw}, capacity, "fake", workers=1)

    assert report.passed, [(item.status, item.failures) for item in report.diagnostics]
    members = materialize_members((atoms,), report)
    parent = next(item for item in atoms.items if item.kind == "attribute")
    children = tuple(member for member in members if member.parent_item_id == parent.item_id)
    assert len(children) > 1
    assert "".join(child.source_projection for child in children) == parent.source_projection
    assert all(piece.translate.source_tokens <= 100 for piece in report.pieces)
    assert all(piece.translate.fits and piece.review.fits for piece in report.pieces)
    targets = {
        child.item_id: record(child.item_id, child.source_projection.replace("upper", "大写")) for child in children
    }
    merged = merge_member_targets(parent, children, targets)
    rendered = fill_resource(
        raw, atoms, {item.item_id: merged if item == parent else item.source_projection for item in atoms.items}
    )
    tree = etree.fromstring(rendered)
    original = etree.fromstring(raw)
    expected = original.xpath("//*[@" + attribute + "]")[0].get(attribute).replace("upper", "大写")
    assert tree.xpath("//*[@" + attribute + "]")[0].get(attribute) == expected
    assert (
        fill_resource(raw, atoms, {item.item_id: item.source_projection for item in atoms.items}, identity=True) == raw
    )
    # Piece source text cannot be paired with forged byte seams.
    from engine.item.validation import validate_coverage

    pieces = [piece for piece in report.pieces if piece.item_id == parent.item_id]
    left, right = pieces[:2]
    seam = left.source_span.byte_end + (2 if encoding.startswith("utf-16") else 1)
    forged = [
        left.model_copy(update={"source_span": left.source_span.model_copy(update={"byte_end": seam})}),
        right.model_copy(update={"source_span": right.source_span.model_copy(update={"byte_start": seam})}),
        *pieces[2:],
    ]
    with pytest.raises(ValueError, match="attribute|boundary|source"):
        validate_coverage(parent, forged, inventory=atoms)


def blocked_checkpoint(tmp_path, monkeypatch):
    value = "StartLayout " + "upper Q left-parenthesis upper M right-parenthesis " * 400 + "EndLayout"
    source = make_epub(
        tmp_path / "book.epub", {"chapter.xhtml": f'<p>Body.</p><img src="formula.png" alt="{value}"/>'}
    )
    root = source.with_suffix("")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    state.initialize(root, source, digest, "atomic-run")
    cli._write_source_hint(root, source, digest, "atomic-run")
    with monkeypatch.context() as patch:
        patch.setattr(preflight, "_split_virtual", lambda *_args: ())
        result = asyncio.run(
            prepare_translation(
                source,
                root,
                atomic_config(
                    max_source_tokens=1500, source_hard_limit=1500, output_budget_version=7, context_unlimited=True
                ),
                StubChecker(),
                term_transport=forbidden,
            )
        )
    assert result.status == "needs_attention" and result.phase == "preflight"
    return source, result.work_dir


def test_blocked_attribute_resumes_preserving_frozen_records_and_rejecting_dispatched_work(tmp_path, monkeypatch):
    _source, root = blocked_checkpoint(tmp_path, monkeypatch)
    frozen = {name: state.read(root / name) for name in ("checks/preflight.json", "preparation.json")}
    store = RunStore(root)
    preparation = store.read_preparation()
    state.write(root / "requests/saved.json", b"{}")
    from engine.services.atomic import IdentityMismatch

    with pytest.raises(IdentityMismatch, match="no dispatched work"):
        preflight.prepare_preflight(
            store, limits_from_config(preparation.translation_config, output_unlimited=True), "fake"
        )
    state.unlink(root / "requests/saved.json")
    result = asyncio.run(resume_preparation(root, StubChecker(), term_transport=forbidden))
    assert result.status == "ready", result.reason
    assert receipt_path(root) == root / "checks/attributes.json"
    assert {name: state.read(root / name) for name in frozen} == frozen
    session = ReadySession(store)
    children = [member for member in session.index.members if member.kind == "attribute"]
    assert len(children) > 1
    assert all(session.index.source_tokens[member.item_id] <= 1500 for member in children)
    assert asyncio.run(resume_preparation(root, StubChecker(), term_transport=forbidden)).status == "ready"


def test_attribute_translation_review_publish_and_resume(tmp_path, monkeypatch):
    source, root = blocked_checkpoint(tmp_path, monkeypatch)
    original = source.read_bytes()
    calls = []
    runtime = BodyJournal.runtime

    async def transport(stage, payload):
        calls.append(stage)
        assert request_source_tokens(payload, "fake") <= 1500
        return answer(stage, payload)

    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: SimpleNamespace(id="fake"))
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: StubChecker())
    monkeypatch.setattr(
        BodyJournal,
        "runtime",
        lambda self, model=None, **kwargs: runtime(
            self, model=model, transport=transport, progress=kwargs.get("progress")
        ),
    )
    output = source.with_name("book-cn.epub")
    result = cli.resume_book(root, output=output)
    assert result.status == "completed", result.reason
    assert set(calls) == {"translate", "review"}
    with ZipFile(output) as archive:
        tree = etree.fromstring(archive.read("OEBPS/chapter.xhtml"))
        image = tree.xpath('//*[local-name()="img"]')[0]
        assert image.get("src") == "formula.png"
        assert "译文" in image.get("alt") and "upper" not in image.get("alt")
    before = len(calls)
    assert cli.resume_book(root, output=output).status == "completed"
    assert len(calls) == before
    assert source.read_bytes() == original


def _limits(source_tokens: int = 80) -> BudgetLimits:
    return BudgetLimits(
        source_tokens=source_tokens,
        source_hard_limit=1500,
        input_tokens=50000,
        output_tokens=None,
        output_version=7,
        context_tokens=50256,
    )


def test_attribute_boundaries_never_cut_after_trailing_whitespace() -> None:
    raw = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head></head><body>'
        b'<img src="f.png" alt="one two   "/></body></html>'
    )
    inventory = extract_resource(raw, "chapter.xhtml", "book")
    item = next(item for item in inventory.items if item.kind == "attribute")

    cuts = attribute_boundaries(inventory, item, raw)
    assert cuts
    assert all(offset < len(item.source_projection) for offset, _byte in cuts)
    assert all(byte < item.source_span.byte_end for _offset, byte in cuts)


def test_attribute_without_safe_whitespace_remains_blocked() -> None:
    value = "x" * 4000
    raw = (
        f'<html xmlns="http://www.w3.org/1999/xhtml"><head></head><body><img src="f.png" alt="{value}"/></body></html>'
    ).encode()
    inventory = extract_resource(raw, "chapter.xhtml", "book")
    report = preflight_atomic_resources(
        (inventory,), {"chapter.xhtml": raw}, _limits(source_tokens=80), "fake", workers=1
    )

    attribute_id = next(item.item_id for item in inventory.items if item.kind == "attribute")
    diagnostic = next(item for item in report.diagnostics if item.item_id == attribute_id)
    assert diagnostic.atomic_tag is None
    assert diagnostic.resource_path == "chapter.xhtml"
    assert diagnostic.source_label == "img@alt"
    assert diagnostic.status == "blocked"
    assert any("source budget" in failure for failure in diagnostic.failures)
    assert report.check is None


def test_attribute_and_table_upgrade_preserves_both_blocked_items(tmp_path, monkeypatch) -> None:
    table = "<table><tbody>" + "".join(f"<tr><td>{'word ' * 900}</td></tr>" for _ in range(2)) + "</tbody></table>"
    alt = "Start " + ("formula term " * 1200) + " end"
    source = make_epub(
        tmp_path / "book.epub",
        {"chapter.xhtml": f'<img src="f.png" alt="{alt}"/>{table}'},
    )
    root = source.with_suffix("")
    with monkeypatch.context() as patch:
        patch.setattr(preflight, "_split_virtual", lambda *_args: ())
        patch.setattr(tables, "split_table", lambda *_args: ())
        first = asyncio.run(
            prepare_translation(
                source,
                root,
                atomic_config(max_source_tokens=1500, source_hard_limit=1500, output_budget_version=7),
                StubChecker(),
                term_transport=forbidden,
            )
        )
    assert first.status == "needs_attention" and first.phase == "preflight"
    attribute_id = next(item.item_id for item in first.diagnostics if item.source_label == "img@alt")
    table_id = next(item.item_id for item in first.diagnostics if item.atomic_tag == "table")
    root = first.work_dir
    prior_path = receipt_path(root)
    prior = state.read(prior_path)

    resumed = asyncio.run(resume_preparation(root, StubChecker(), term_transport=forbidden))
    assert resumed.status == "ready", resumed.reason
    assert state.read(prior_path) == prior
    assert state.is_file(root / "checks/attributes.json")
    store = RunStore(root)
    report = preflight.read_preflight(
        store,
        limits_from_config(store.read_preparation().translation_config, output_unlimited=True),
        "fake",
    )
    diagnostics = {item.item_id: item for item in report.diagnostics}
    assert diagnostics[attribute_id].status == "split"
    assert diagnostics[table_id].status == "split"
