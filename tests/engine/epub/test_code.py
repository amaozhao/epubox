"""End-to-end publication guarantees for source-exact code."""

from __future__ import annotations

import json
import zipfile
from functools import partial
from pathlib import Path

import pytest

from engine import cli
from engine.epub.preservation import CODE_POLICY, decode_code_maps, discover_code_plan, verify_code_plan
from engine.epub.publication import publish_book
from engine.epub.publish import publish_atomic, recover_atomic
from engine.epub.validation import EpubValidationError, inspect_epub
from engine.epub.verification import file_hash, stage_epub, verify_staged_epub
from engine.item.atoms import extract_resource
from engine.schemas.contracts import canonical_hash
from engine.services import state
from engine.services.atomic import IdentityMismatch
from engine.services.journal import BodyJournal
from tests.engine.epub.test_factory import make_epub
from tests.engine.epub.test_preparation import StubChecker
from tests.engine.epub.test_publication import (
    _accepted_records,
    _coherence_checks,
    _planned_record,
    _prepared,
    _review_manifests,
)
from tests.engine.epub.test_publish import _case as atomic_case

RESOURCE = "OEBPS/chapter.xhtml"
INLINE_SOURCE = "⟦+b1⟧Use ⟦-b1⟧⟦=x1⟧⟦+b2⟧ now.⟦-b2⟧"


def test_legacy_named_entities_keep_exact_code_characters_during_publication(tmp_path, monkeypatch):
    import tests.engine.agents.test_workflow as workflow_fixture
    from engine.epub.parsing import parse_resource

    raw = (
        b'<?xml version="1.0" encoding="utf-8"?>\r\n'
        b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
        b'"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\r\n'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>chapter</title></head><body>'
        b'<p>Normal prose.</p><pre title="copyright &copy;">\tprint("&copy; &amp;copy;");\r\n'
        b"&nbsp;&nbsp;# keep indent<![CDATA[literal &copy;]]><!-- keep &nbsp; --><?keep &copy;?></pre>"
        b"</body></html>"
    )

    def legacy_book(path, documents):
        return make_epub(path, {"chapter.xhtml": raw.decode()}, version="2.0")

    monkeypatch.setattr(workflow_fixture, "make_epub", legacy_book)
    case = atomic_case(tmp_path, "unused", ("Normal prose.",))
    targets = {
        item.item_id: item.source_projection.replace("Normal prose.", "正文说明。")
        for inventory in case.index.inventories
        for item in inventory.items
    }
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)
    output = tmp_path / "legacy-code-cn.epub"
    result = publish_atomic(case.session.store, output, StubChecker())
    proof = result["verification"]
    assert isinstance(proof, dict) and proof["code_policy"] == CODE_POLICY
    with zipfile.ZipFile(output) as archive:
        rendered = archive.read(RESOURCE)
    before = parse_resource(raw, "application/xhtml+xml").tree
    after = parse_resource(rendered, "application/xhtml+xml").tree
    assert before is not None and after is not None
    first = next(before.iter("{http://www.w3.org/1999/xhtml}pre"))
    second = next(after.iter("{http://www.w3.org/1999/xhtml}pre"))
    assert "".join(first.itertext()) == "".join(second.itertext())
    assert first.attrib == second.attrib
    assert b"&amp;copy;" in rendered and b"<![CDATA[literal &copy;]]>" in rendered
    assert b"<!-- keep &nbsp; --><?keep &copy;?>" in rendered
    assert "正文说明。" in rendered.decode()


def _atomic_case(tmp_path: Path):
    body = (
        "<style>.listing{white-space:pre-wrap;font-family:Menlo,monospace}</style>"
        "<p>Explanation.</p><span></span><pre>  let semantic = 1;\n</pre>"
        '<p class="listing">  let styled = 2;\n</p>'
        "<p>Use <code>OutPoint</code> now.</p>"
    )
    case = atomic_case(tmp_path, body, ("Explanation.", INLINE_SOURCE))
    targets = {
        item.item_id: item.source_projection.replace("Explanation.", "中文说明。")
        .replace("Use ", "使用 ")
        .replace(" now.", "。")
        for inventory in case.index.inventories
        for item in inventory.items
    }
    return case, targets


def _resource(path: Path) -> bytes:
    with zipfile.ZipFile(path) as archive:
        return archive.read(RESOURCE)


def test_atomic_publication_automatically_preserves_all_code_without_manual_plan(tmp_path: Path, monkeypatch) -> None:
    case, targets = _atomic_case(tmp_path)
    store = case.session.store
    before = {path: state.read(path) for path in state.glob(store.root / "results", "*.json")}
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)
    output = tmp_path / "reader.epub"

    result = publish_atomic(store, output, StubChecker())

    proof = result["verification"]
    assert isinstance(proof, dict) and proof["code_policy"] == CODE_POLICY
    assert proof["code_node_maps"]
    plan = discover_code_plan(tmp_path / "source.epub")
    assert plan is not None and proof["code_plan_hash"] == canonical_hash(plan)
    assert (
        verify_code_plan(
            tmp_path / "source.epub",
            output,
            plan,
            node_maps=decode_code_maps(proof["code_node_maps"]),
        )
        == proof["code_stats"]
    )
    rendered = _resource(output)
    assert b"  let semantic = 1;\n" in rendered and b"  let styled = 2;\n" in rendered
    assert b"OutPoint" in rendered and "中文说明。" in rendered.decode()
    assert "使用 ".encode() in rendered
    assert b"font-family:Menlo,monospace" in rendered
    assert not state.is_file(store.root / "checks" / "code.json")
    assert {path: state.read(path) for path in before} == before
    recovered = recover_atomic(store, output, StubChecker())
    assert recovered is not None and recovered["sha256"] == result["sha256"]


def test_no_code_book_still_records_current_code_policy_and_recovers_without_model(
    tmp_path: Path, monkeypatch
) -> None:
    case = atomic_case(tmp_path, "<p>Explanation.</p>", ("Explanation.",))
    targets = {item.item_id: "中文说明。" for inventory in case.index.inventories for item in inventory.items}
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)
    output = tmp_path / "reader.epub"

    first = publish_atomic(case.session.store, output, StubChecker())
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: pytest.fail("model constructed"))
    second = recover_atomic(case.session.store, output, StubChecker())

    proof = first["verification"]
    assert isinstance(proof, dict) and proof["code_policy"] == CODE_POLICY
    assert proof["code_stats"] == {"checked_resources": 0, "selected_bytes": 0, "selected_regions": 0}
    assert second is not None and second["sha256"] == first["sha256"]


def test_old_atomic_five_accepted_code_translation_is_restored_during_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import engine.epub.preparation as epub_preparation
    import tests.engine.agents.test_workflow as workflow_fixture
    from engine.services import preparation

    version = "epubox-atomic-4"
    monkeypatch.setattr(preparation, "ATOMIC_EXTRACTOR_VERSION", version)
    monkeypatch.setattr(epub_preparation, "ATOMIC_EXTRACTOR_VERSION", version)
    monkeypatch.setattr(
        epub_preparation,
        "extract_resource",
        partial(epub_preparation.extract_resource, extractor_version=version),
    )
    monkeypatch.setattr(workflow_fixture, "EXTRACTOR_VERSION", version)
    style = "white-space:pre-wrap;font-family:Menlo,Consolas,monospace"
    case = atomic_case(
        tmp_path,
        f'<p>Explanation.</p><p style="{style}"><span>let</span> amount = 1;</p>',
        ("Explanation.", "⟦+g1⟧let⟦-g1⟧ amount = 1;"),
    )
    targets = {
        item.item_id: item.source_projection.replace("Explanation.", "中文说明。").replace("amount", "数量")
        for inventory in case.index.inventories
        for item in inventory.items
    }
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)
    output = tmp_path / "reader.epub"

    result = publish_atomic(case.session.store, output, StubChecker())

    assert b"let</span> amount = 1;" in _resource(output)
    assert "数量" not in _resource(output).decode()
    proof = result["verification"]
    assert isinstance(proof, dict) and proof["code_policy"] == CODE_POLICY


def test_legacy_publication_restores_mistranslated_styled_code(tmp_path: Path, monkeypatch) -> None:
    style = "white-space:pre-wrap;font-family:Menlo,Consolas,monospace"
    source, store, plan, documents = _prepared(
        tmp_path,
        f'<p>Explanation.</p><p style="{style}">let amount = 1;</p>',
    )
    records = _accepted_records(documents)
    code = next(
        unit for document in documents.values() for unit in document.units if "amount" in unit.source_projection
    )
    document = next(value for value in documents.values() if value.document_id == code.document_id)
    records[code.unit_id] = _planned_record(document, code, code.source_projection.replace("amount", "数量"))
    initial_plans = dict(plan.initial_unit_plans)
    assert records[code.unit_id].cut_plan is not None
    initial_plans[code.unit_id] = records[code.unit_id].cut_plan.plan_hash
    plan = plan.model_copy(update={"initial_unit_plans": initial_plans})
    manifests = _review_manifests(plan, records)
    _coherence_checks(store, documents, records)
    monkeypatch.setattr(store, "read_bookplan", lambda: plan)
    monkeypatch.setattr(store, "read_unit", lambda unit_id: records[unit_id])
    monkeypatch.setattr(store, "read_request", lambda request_id: manifests[request_id])
    output = tmp_path / "legacy.epub"

    result = publish_book(store, output, StubChecker())

    assert source.is_file() and b"let amount = 1;" in _resource(output)
    assert "数量" not in _resource(output).decode()
    proof = result["verification"]
    assert isinstance(proof, dict) and proof["code_policy"] == CODE_POLICY


def test_final_verifier_rejects_code_tampering_even_when_epubcheck_passes(tmp_path: Path) -> None:
    source = tmp_path / "source.epub"
    from tests.engine.epub.test_factory import make_epub

    make_epub(source, {"chapter.xhtml": "<pre>let value = 1;</pre><p>Text.</p>"})
    plan = discover_code_plan(source)
    assert plan is not None
    inventory = inspect_epub(source, file_hash(source), checker=StubChecker())
    staged = tmp_path / "staged.epub"
    chapter = _resource(source).replace(b"let value = 1", b"let changed = 2")
    stage_epub(source, staged, {RESOURCE: chapter})

    with pytest.raises((EpubValidationError, ValueError), match="code|selected|source-exact"):
        verify_staged_epub(
            source,
            staged,
            inventory,
            {RESOURCE: chapter},
            accepted_targets={RESOURCE: {}},
            checker=StubChecker(),
            upgraded=True,
            code_plan=plan,
        )


def test_missing_normal_target_coverage_is_rejected_before_code_preservation(tmp_path: Path) -> None:
    case, _targets = _atomic_case(tmp_path)

    with pytest.raises((EpubValidationError, IdentityMismatch), match="cover|incomplete"):
        publish_atomic(case.session.store, tmp_path / "missing.epub", StubChecker())


def test_completed_run_upgrades_old_publication_proof_without_constructing_a_model(
    tmp_path: Path, monkeypatch
) -> None:
    case, targets = _atomic_case(tmp_path)
    store = case.session.store
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)
    output = tmp_path / "reader.epub"
    first = publish_atomic(store, output, StubChecker())
    intent_path = store.root / "publish.json"
    intent = json.loads(state.read(intent_path))
    for field in ("code_policy", "code_plan_hash", "code_stats", "code_node_maps"):
        intent["verification"].pop(field, None)
    intent["version_vector"].pop("@source-code", None)
    state.write(intent_path, json.dumps(intent).encode())
    old_hash = str(first["sha256"])
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: StubChecker())
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: pytest.fail("model constructed"))
    monkeypatch.setattr(cli, "_record", lambda outcome: outcome)

    outcome = cli._completed_run_outcome(store.root, output)

    assert outcome is not None and outcome.status == "completed"
    proof = json.loads(state.read(intent_path))["verification"]
    assert proof["code_policy"] == CODE_POLICY and proof["code_node_maps"]
    plan = discover_code_plan(tmp_path / "source.epub")
    assert plan is not None
    assert (
        outcome.output_sha256 != old_hash
        or verify_code_plan(
            tmp_path / "source.epub",
            output,
            plan,
            node_maps=decode_code_maps(proof["code_node_maps"]),
        )
        == proof["code_stats"]
    )


def test_code_with_chinese_string_and_ascii_spaces_is_source_exact_while_prose_spacing_is_cleaned(
    tmp_path: Path, monkeypatch
) -> None:
    body = '<p>Translate this.</p><pre>  let message = "你 好";\n</pre>'
    case = atomic_case(tmp_path, body, ("Translate this.",))
    targets = {
        item.item_id: item.source_projection.replace("Translate this.", "中 文需要清理。")
        for inventory in case.index.inventories
        for item in inventory.items
    }
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)
    output = tmp_path / "reader.epub"

    result = publish_atomic(case.session.store, output, StubChecker())

    rendered = _resource(output)
    assert '  let message = "你 好";\n'.encode() in rendered
    assert "中文需要清理。".encode() in rendered and "中 文需要清理。".encode() not in rendered
    proof = result["verification"]
    assert isinstance(proof, dict)
    assert proof["cleanup_stats"]["removed_spaces"] >= 1
    assert proof["code_policy"] == CODE_POLICY


def test_v6_code_classification_binds_linked_styles_before_any_model_transport() -> None:
    raw = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Code</title>'
        b'<link rel="stylesheet" href="code.css"/></head><body>'
        b'<p class="listing">let value = 1;</p><p>Translate prose.</p></body></html>'
    )
    proven = {"OEBPS/code.css": ".listing{white-space:pre-wrap;font-family:monospace}"}
    changed = {"OEBPS/code.css": ".listing{white-space:normal;font-family:serif}"}

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "engine.agents.runtime.ModelRuntime.__init__",
            lambda *_args, **_kwargs: pytest.fail("model constructed before stylesheet identity check"),
        )
        frozen = extract_resource(raw, RESOURCE, "source-hash", styles=proven)
        replayed = extract_resource(raw, RESOURCE, "source-hash", styles=changed)
        with pytest.raises(ValueError, match="missing stylesheet"):
            extract_resource(raw, RESOURCE, "source-hash", styles={})

    assert frozen.document != replayed.document
    assert all("value" not in item.source_projection for item in frozen.items)
    assert any("value" in item.source_projection for item in replayed.items)
