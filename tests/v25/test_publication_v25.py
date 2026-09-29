from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest

from engine.epub.assembly import assemble_document
from engine.epub.preparation_v25 import PreparationConfig, prepare_book
from engine.epub.publication import publish_book, recover_publication, validate_assembled_document
from engine.epub.validation import EpubCheckResult, EpubValidationError
from engine.item.planner_v25 import plan_unit_v25
from engine.schemas.v25 import (
    BookPlan,
    GlossarySnapshot,
    ItemStatus,
    UnitRecord,
    canonical_hash,
)
from engine.services.store import Store
from engine.services.store_v25 import StoreV25
from tests.v23.book_factory import make_epub


class StubChecker:
    def check(self, path: Path) -> EpubCheckResult:
        assert path.is_file()
        return EpubCheckResult(("stub-epubcheck",), 0)


def _prepared(tmp_path: Path):
    source = make_epub(
        tmp_path / "source.epub",
        {"chapter.xhtml": "<h1>Reliable systems</h1><p>Keep <em>source data</em> safe.</p>"},
    )
    prepared = prepare_book(
        source,
        tmp_path / "work",
        PreparationConfig(run_id="publish-v25", translation_config={"target_language": "zh-Hans"}),
        StubChecker(),
    )
    store = StoreV25(prepared.work_dir)
    preparation = store.read_preparation()
    ordered_documents = (
        *preparation.reading_order,
        *(document_id for document_id in preparation.document_hashes if document_id not in preparation.reading_order),
    )
    documents = {document_id: store.read_document(document_id) for document_id in ordered_documents}
    unit_ids = tuple(unit.unit_id for document in documents.values() for unit in document.units)
    plan = BookPlan(
        source_hash=preparation.source_hash,
        run_id=preparation.run_id,
        preparation_hash=prepared.preparation_hash,
        glossary_file_sha256="glossary-sha",
        freeze_file_sha256="freeze-sha",
        freeze_id="freeze-1",
        document_hashes=preparation.document_hashes,
        unit_ids=unit_ids,
        unit_documents=preparation.unit_documents,
        required_unit_count=len(unit_ids),
        initial_unit_plans={unit_id: None for unit_id in unit_ids},
        translation_config=preparation.translation_config,
        output_policy_hash=canonical_hash({"target_language": "zh-Hans"}),
    )
    return source, store, plan, documents


def _accepted_records(documents, *, translated_heading: str | None = None):
    records = {}
    translated = False
    for document in documents.values():
        for unit in document.units:
            target = unit.source_projection
            if translated_heading is not None and not translated and unit.source_projection == "Reliable systems":
                target = translated_heading
                translated = True
            target_hash = canonical_hash(target)
            records[unit.unit_id] = UnitRecord(
                unit_id=unit.unit_id,
                document_id=document.document_id,
                source_hash=document.source_hash,
                revision=1,
                candidate=target,
                accepted_revision=1,
                accepted_target_hash=target_hash,
                local_checks={"passed": True, "target_hash": target_hash},
                review={"passed": True, "revision": 1, "input_hash": None, "target_hash": target_hash},
            )
    return records


def _planned_record(document, unit, target: str):
    glossary = GlossarySnapshot(
        source_hash=document.source_hash,
        freeze_id="freeze-1",
        extraction_config_hash="extract-config",
        user_terms_hash=canonical_hash(()),
        extraction_status="not_required",
        warnings=("No terminology required.",),
    )
    initialized = plan_unit_v25(
        unit,
        document,
        glossary,
        {"target_language": "zh-Hans", "context_tokens": 8192, "max_output_tokens": 2048},
    )
    assert len(initialized.items) == 1
    target_hash = canonical_hash(target)
    items = {
        item_id: item.model_copy(
            update={
                "status": ItemStatus.REVIEWED,
                "target_projection": target,
                "target_hash": target_hash,
            }
        )
        for item_id, item in initialized.items.items()
    }
    return UnitRecord(
        unit_id=unit.unit_id,
        document_id=document.document_id,
        source_hash=document.source_hash,
        logical_hash=initialized.logical_hash,
        input_hash=initialized.input_hash,
        revision=1,
        cut_plan=initialized.cut_plan,
        items=items,
        candidate=target,
        accepted_revision=1,
        accepted_target_hash=target_hash,
        local_checks={"passed": True, "target_hash": target_hash},
        review={
            "passed": True,
            "revision": 1,
            "input_hash": initialized.input_hash,
            "target_hash": target_hash,
        },
    )


def test_v25_assembly_identity_and_accepted_projection_use_the_same_source_template(tmp_path: Path) -> None:
    _, _, _, documents = _prepared(tmp_path)
    document = next(document for document in documents.values() if document.resource.path.endswith("chapter.xhtml"))
    heading = next(unit for unit in document.units if unit.source_projection == "Reliable systems")

    identity = assemble_document(document, {}, identity=True)
    targets = {unit.unit_id: unit.source_projection for unit in document.units}
    targets[heading.unit_id] = "可靠系统"
    translated = assemble_document(document, targets)

    assert "Reliable systems" in identity.markup
    assert "可靠系统" in translated.markup
    assert "source data" in translated.markup
    validate_assembled_document(
        document,
        targets,
        translated.markup,
        source_to_target=translated.source_to_target,
    )


def test_publish_book_uses_only_v25_bookplan_documents_and_current_accepted_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store, plan, documents = _prepared(tmp_path)
    records = _accepted_records(documents, translated_heading="可靠系统")
    heading_document = next(
        document
        for document in documents.values()
        if any(unit.source_projection == "Reliable systems" for unit in document.units)
    )
    heading = next(unit for unit in heading_document.units if unit.source_projection == "Reliable systems")
    records[heading.unit_id] = _planned_record(heading_document, heading, "可靠系统")
    initial_plans = dict(plan.initial_unit_plans)
    assert records[heading.unit_id].cut_plan is not None
    initial_plans[heading.unit_id] = records[heading.unit_id].cut_plan.plan_hash
    plan = plan.model_copy(update={"initial_unit_plans": initial_plans})
    monkeypatch.setattr(store, "read_bookplan", lambda: plan)
    monkeypatch.setattr(store, "read_unit", lambda unit_id: records[unit_id])
    output = tmp_path / "translated.epub"

    result = publish_book(store, output, StubChecker())

    assert result["path"] == str(output)
    publish = result["publish"]
    assert isinstance(publish, dict) and publish["state"] == "completed"
    with zipfile.ZipFile(output) as archive:
        chapter = archive.read("OEBPS/chapter.xhtml").decode()
        opf = archive.read("OEBPS/content.opf").decode()
    assert "可靠系统" in chapter
    assert "zh-Hans" in opf
    recovered = recover_publication(
        store.root / "publish.json",
        plan_fingerprint=canonical_hash(plan),
        version_vector={unit_id: 1 for unit_id in plan.unit_ids},
    )
    assert recovered is not None and recovered["state"] == "completed"


def test_identity_publish_needs_no_translation_records_but_still_verifies_the_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, store, plan, _ = _prepared(tmp_path)
    monkeypatch.setattr(store, "read_bookplan", lambda: plan)
    monkeypatch.setattr(store, "read_unit", lambda _unit_id: pytest.fail("identity must not read UnitRecord"))
    output = tmp_path / "identity.epub"

    result = publish_book(store, output, StubChecker(), identity=True)

    publish = result["publish"]
    assert output.is_file() and isinstance(publish, dict) and publish["state"] == "completed"
    assert source.read_bytes() != b""
    intent = json.loads((store.root / "publish.json").read_text())
    assert intent["version_vector"] == {unit_id: 0 for unit_id in sorted(plan.unit_ids)}


def test_publication_rejects_unaccepted_v25_record_and_legacy_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store, plan, documents = _prepared(tmp_path)
    records = _accepted_records(documents)
    rejected_id = plan.unit_ids[0]
    records[rejected_id] = records[rejected_id].model_copy(
        update={"accepted_revision": None, "accepted_target_hash": None}
    )
    monkeypatch.setattr(store, "read_bookplan", lambda: plan)
    monkeypatch.setattr(store, "read_unit", lambda unit_id: records[unit_id])

    with pytest.raises(EpubValidationError, match="not currently accepted"):
        publish_book(store, tmp_path / "rejected.epub", StubChecker())
    with pytest.raises(TypeError, match="StoreV25"):
        publish_book(cast(Any, Store(tmp_path / "legacy")), tmp_path / "legacy.epub", StubChecker())


def test_publication_rejects_record_changed_during_package_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store, plan, documents = _prepared(tmp_path)
    records = _accepted_records(documents)
    changed_id = plan.unit_ids[0]
    reads: dict[str, int] = {}

    def read_unit(unit_id: str):
        reads[unit_id] = reads.get(unit_id, 0) + 1
        if unit_id == changed_id and reads[unit_id] > 1:
            return records[unit_id].model_copy(update={"record_version": records[unit_id].record_version + 1})
        return records[unit_id]

    monkeypatch.setattr(store, "read_bookplan", lambda: plan)
    monkeypatch.setattr(store, "read_unit", read_unit)

    with pytest.raises(EpubValidationError, match="changed while publication"):
        publish_book(store, tmp_path / "stale.epub", StubChecker())
    assert not (tmp_path / "stale.epub").exists()
