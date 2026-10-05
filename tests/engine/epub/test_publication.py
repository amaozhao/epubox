from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest

from engine.epub.assembly import assemble_document, derive_navigation_projection
from engine.epub.preparation import PreparationConfig, prepare_book
from engine.epub.publication import publish_book, recover_publication, validate_assembled_document
from engine.epub.validation import EpubCheckResult, EpubValidationError
from engine.item.unit_planner import plan_unit
from engine.schemas.contracts import (
    Attempt,
    BookPlan,
    GlossarySnapshot,
    ItemStatus,
    RequestManifest,
    UnitRecord,
    canonical_hash,
)
from engine.services.coherence import pending_windows, prepare_document_check, save_window_result
from engine.services.store import RunStore
from tests.engine.epub.factory import make_epub


class StubChecker:
    def check(self, path: Path) -> EpubCheckResult:
        assert path.is_file()
        return EpubCheckResult(("stub-epubcheck",), 0)


def _prepared(
    tmp_path: Path,
    chapter: str = "<h1>Reliable systems</h1><p>Keep <em>source data</em> safe.</p>",
):
    source = make_epub(
        tmp_path / "source.epub",
        {"chapter.xhtml": chapter},
    )
    prepared = prepare_book(
        source,
        tmp_path / "work",
        PreparationConfig(run_id="publish-v25", translation_config={"target_language": "zh-Hans"}),
        StubChecker(),
    )
    store = RunStore(prepared.work_dir)
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
                review={
                    "protocol": "epubox-review-2",
                    "plan_epoch": 0,
                    "passed": True,
                    "revision": 1,
                    "input_hash": None,
                    "target_hash": target_hash,
                    "item_reviews": {
                        f"derived-{unit.unit_id}": {
                            "request_id": f"review-derived-{unit.unit_id}",
                            "target_hash": target_hash,
                        }
                    },
                },
            )
    return records


def _planned_record(document, unit, target: str, *, context_tokens: int = 8192, max_output_tokens: int = 2048):
    glossary = GlossarySnapshot(
        source_hash=document.source_hash,
        freeze_id="freeze-1",
        extraction_config_hash="extract-config",
        user_terms_hash=canonical_hash(()),
        extraction_status="not_required",
        warnings=("No terminology required.",),
    )
    initialized = plan_unit(
        unit,
        document,
        glossary,
        {
            "target_language": "zh-Hans",
            "context_tokens": context_tokens,
            "max_output_tokens": max_output_tokens,
            "review_output_tokens": max_output_tokens,
        },
    )
    target_hash = canonical_hash(target)
    items = {
        item_id: item.model_copy(
            update={
                "status": ItemStatus.REVIEWED,
                "target_projection": (
                    target
                    if len(initialized.items) == 1
                    else next(
                        segment.source_projection
                        for segment in initialized.cut_plan.segments
                        if segment.item_id == item_id
                    )
                ),
            }
        )
        for item_id, item in initialized.items.items()
    }
    items = {
        item_id: item.model_copy(update={"target_hash": canonical_hash(item.target_projection)})
        for item_id, item in items.items()
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
            "protocol": "epubox-review-2",
            "plan_epoch": initialized.cut_plan.plan_epoch,
            "passed": True,
            "revision": 1,
            "input_hash": initialized.input_hash,
            "target_hash": target_hash,
            "item_reviews": {
                item_id: {
                    "request_id": f"review-{item_id}",
                    "target_hash": item.target_hash,
                }
                for item_id, item in items.items()
            },
        },
    )


def _review_manifests(plan: BookPlan, records: dict[str, UnitRecord]) -> dict[str, RequestManifest]:
    manifests = {}
    for unit_id, record in records.items():
        if record.derived is not None:
            continue
        assert record.review is not None and isinstance(record.review["item_reviews"], dict)
        for item_id, item_review in record.review["item_reviews"].items():
            assert isinstance(item_review, dict)
            request_id = str(item_review["request_id"])
            item_target_hash = str(item_review["target_hash"])
            manifests[request_id] = RequestManifest(
                request_id=request_id,
                stage="review",
                owner_kind="translation_item",
                owner_id=item_id,
                item_ids=(item_id,),
                input_hashes={item_id: f"input-{item_id}"},
                wire_hash=f"wire-{item_id}",
                record_versions={unit_id: record.record_version},
                item_unit_ids={item_id: (unit_id,)},
                unit_document_ids={unit_id: record.document_id},
                plan_epochs={unit_id: record.plan_epoch},
                revisions={unit_id: record.revision},
                target_hashes={item_id: item_target_hash},
                glossary_file_sha256=plan.glossary_file_sha256,
                freeze_id=plan.freeze_id,
                term_ids_by_item={item_id: ()},
                terms_hashes={item_id: record.items[item_id].terms_hash if item_id in record.items else "terms"},
                context_hashes={item_id: record.items[item_id].context_hash if item_id in record.items else "context"},
                attempts=(
                    Attempt(
                        attempt_id=f"attempt-{item_id}",
                        affected_items=(item_id,),
                        state="succeeded",
                        created_at="2026-09-29T00:00:00Z",
                        finished_at="2026-09-29T00:00:01Z",
                    ),
                ),
            )
    return manifests


def _coherence_checks(store: RunStore, documents, records: dict[str, UnitRecord]):
    checks = {}
    for document in documents.values():
        check = prepare_document_check(store, document, records)
        for window in pending_windows(check):
            check = save_window_result(store, check, str(window["item_id"]), ())
        assert check["status"] == "valid"
        checks[document.document_id] = check
    return checks


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
    manifests = _review_manifests(plan, records)
    _coherence_checks(store, documents, records)
    monkeypatch.setattr(store, "read_bookplan", lambda: plan)
    monkeypatch.setattr(store, "read_unit", lambda unit_id: records[unit_id])
    monkeypatch.setattr(store, "read_request", lambda request_id: manifests[request_id])
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
    _coherence_checks(store, documents, records)
    rejected_id = plan.unit_ids[0]
    records[rejected_id] = records[rejected_id].model_copy(
        update={"accepted_revision": None, "accepted_target_hash": None}
    )
    manifests = _review_manifests(plan, records)
    monkeypatch.setattr(store, "read_bookplan", lambda: plan)
    monkeypatch.setattr(store, "read_unit", lambda unit_id: records[unit_id])
    monkeypatch.setattr(store, "read_request", lambda request_id: manifests[request_id])

    with pytest.raises(EpubValidationError, match="not currently accepted"):
        publish_book(store, tmp_path / "rejected.epub", StubChecker())
    restored = _accepted_records(documents)
    restored[rejected_id] = restored[rejected_id].model_copy(
        update={"review": restored[rejected_id].review | {"protocol": "epubox-review-1"}}
    )
    old_manifests = _review_manifests(plan, restored)
    monkeypatch.setattr(store, "read_unit", lambda unit_id: restored[unit_id])
    monkeypatch.setattr(store, "read_request", lambda request_id: old_manifests[request_id])
    with pytest.raises(EpubValidationError, match="not currently accepted"):
        publish_book(store, tmp_path / "review-1.epub", StubChecker())
    with pytest.raises(TypeError, match="RunStore"):
        publish_book(cast(Any, object()), tmp_path / "legacy.epub", StubChecker())


def test_publication_rejects_record_changed_during_package_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store, plan, documents = _prepared(tmp_path)
    records = _accepted_records(documents)
    manifests = _review_manifests(plan, records)
    _coherence_checks(store, documents, records)
    changed_id = plan.unit_ids[0]
    reads: dict[str, int] = {}

    def read_unit(unit_id: str):
        reads[unit_id] = reads.get(unit_id, 0) + 1
        if unit_id == changed_id and reads[unit_id] > 1:
            return records[unit_id].model_copy(update={"record_version": records[unit_id].record_version + 1})
        return records[unit_id]

    monkeypatch.setattr(store, "read_bookplan", lambda: plan)
    monkeypatch.setattr(store, "read_unit", read_unit)
    monkeypatch.setattr(store, "read_request", lambda request_id: manifests[request_id])

    with pytest.raises(EpubValidationError, match="changed while publication"):
        publish_book(store, tmp_path / "stale.epub", StubChecker())
    assert not (tmp_path / "stale.epub").exists()


def test_long_unit_requires_every_segment_review_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    long_text = "Long sentence for segmented review. " * 100
    _, store, plan, documents = _prepared(tmp_path, chapter=f"<p>{long_text}</p>")
    records = _accepted_records(documents)
    document = next(document for document in documents.values() if document.resource.path.endswith("chapter.xhtml"))
    unit = next(unit for unit in document.units if "Long sentence" in unit.source_projection)
    records[unit.unit_id] = _planned_record(
        document,
        unit,
        unit.source_projection,
        context_tokens=4096,
        max_output_tokens=256,
    )
    assert len(records[unit.unit_id].items) > 1
    initial_plans = dict(plan.initial_unit_plans)
    assert records[unit.unit_id].cut_plan is not None
    initial_plans[unit.unit_id] = records[unit.unit_id].cut_plan.plan_hash
    plan = plan.model_copy(update={"initial_unit_plans": initial_plans})
    manifests = _review_manifests(plan, records)
    _coherence_checks(store, documents, records)
    loaded_requests: list[str] = []

    def read_request(request_id: str):
        loaded_requests.append(request_id)
        return manifests[request_id]

    monkeypatch.setattr(store, "read_bookplan", lambda: plan)
    monkeypatch.setattr(store, "read_unit", lambda unit_id: records[unit_id])
    monkeypatch.setattr(store, "read_request", read_request)

    publish_book(store, tmp_path / "long.epub", StubChecker())

    review = records[unit.unit_id].review
    assert review is not None and isinstance(review["item_reviews"], dict)
    expected_requests = {
        item_review["request_id"] for item_review in review["item_reviews"].values() if isinstance(item_review, dict)
    }
    assert len(expected_requests) == len(records[unit.unit_id].items)
    assert expected_requests.issubset(loaded_requests)


def test_formal_publication_requires_current_complete_nonblocking_coherence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store, plan, documents = _prepared(tmp_path)
    records = _accepted_records(documents)
    manifests = _review_manifests(plan, records)
    monkeypatch.setattr(store, "read_bookplan", lambda: plan)
    monkeypatch.setattr(store, "read_unit", lambda unit_id: records[unit_id])
    monkeypatch.setattr(store, "read_request", lambda request_id: manifests[request_id])

    with pytest.raises(EpubValidationError, match="Missing coherence"):
        publish_book(store, tmp_path / "missing-check.epub", StubChecker())

    checks = _coherence_checks(store, documents, records)
    document_id = next(document_id for document_id, check in checks.items() if check["windows"])
    path = store.root / "checks" / f"{document_id}.json"

    def write_check(check: dict[str, Any]) -> None:
        value = dict(check)
        value["record_hash"] = canonical_hash({key: item for key, item in value.items() if key != "record_hash"})
        store._atomic_write(path, value)

    write_check(dict(checks[document_id]) | {"format": "epubox-check-2"})
    with pytest.raises(EpubValidationError, match="Invalid coherence"):
        publish_book(store, tmp_path / "old-check.epub", StubChecker())

    stale = dict(checks[document_id])
    assert isinstance(stale["candidate_versions"], dict)
    stale["candidate_versions"] = {unit_id: revision + 1 for unit_id, revision in stale["candidate_versions"].items()}
    write_check(stale)
    with pytest.raises(EpubValidationError, match="versions are stale"):
        publish_book(store, tmp_path / "stale-check.epub", StubChecker())

    incomplete = dict(checks[document_id])
    assert isinstance(incomplete["checks"], dict)
    incomplete["checks"] = dict(incomplete["checks"])
    incomplete["checks"].pop(next(iter(incomplete["checks"])))
    write_check(incomplete)
    with pytest.raises(EpubValidationError, match="incomplete"):
        publish_book(store, tmp_path / "incomplete-check.epub", StubChecker())

    blocking = dict(checks[document_id])
    assert isinstance(blocking["checks"], dict)
    blocking["checks"] = dict(blocking["checks"])
    first_window = next(iter(blocking["checks"]))
    issue = {"code": "continuity", "severity": "major", "message": "broken"}
    blocking["checks"][first_window] = {"issues": [issue]}
    blocking["issues"] = [issue]
    write_check(blocking)
    with pytest.raises(EpubValidationError, match="blocking issues"):
        publish_book(store, tmp_path / "blocking-check.epub", StubChecker())


def test_derived_navigation_tracks_current_accepted_title_and_rejects_stale_or_failed_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chapter = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter 1</title></head>'
        "<body><p>Chapter body.</p></body></html>"
    )
    _, store, plan, documents = _prepared(tmp_path, chapter=chapter)
    binding_document = next(
        document
        for document in documents.values()
        if document.resource.path.endswith("nav.xhtml")
        and any(binding.get("kind") == "derived_navigation" for binding in document.derived_bindings)
    )
    binding = next(
        binding for binding in binding_document.derived_bindings if binding.get("kind") == "derived_navigation"
    )
    target_unit = next(unit for unit in binding_document.units if unit.unit_id == binding["unit_id"])
    source_document = next(
        document
        for document in documents.values()
        if any(unit.unit_id == binding["source_unit_id"] for unit in document.units)
    )
    source_unit = next(unit for unit in source_document.units if unit.unit_id == binding["source_unit_id"])
    records = _accepted_records(documents)
    records[source_unit.unit_id] = _planned_record(source_document, source_unit, "第一章")
    derived_target = derive_navigation_projection(target_unit, "第一章")
    records[target_unit.unit_id] = UnitRecord(
        unit_id=target_unit.unit_id,
        document_id=binding_document.document_id,
        source_hash=binding_document.source_hash,
        derived={
            "state": "valid",
            "source_unit_id": source_unit.unit_id,
            "source_revision": records[source_unit.unit_id].revision,
            "source_target_hash": records[source_unit.unit_id].accepted_target_hash,
            "target": derived_target,
            "target_hash": canonical_hash(derived_target),
        },
    )
    initial_plans = dict(plan.initial_unit_plans)
    assert records[source_unit.unit_id].cut_plan is not None
    initial_plans[source_unit.unit_id] = records[source_unit.unit_id].cut_plan.plan_hash
    initial_plans[target_unit.unit_id] = None
    plan = plan.model_copy(update={"initial_unit_plans": initial_plans})
    manifests = _review_manifests(plan, records)
    _coherence_checks(store, documents, records)
    monkeypatch.setattr(store, "read_bookplan", lambda: plan)
    monkeypatch.setattr(store, "read_unit", lambda unit_id: records[unit_id])
    monkeypatch.setattr(store, "read_request", lambda request_id: manifests[request_id])

    output = tmp_path / "derived.epub"
    publish_book(store, output, StubChecker())
    with zipfile.ZipFile(output) as archive:
        assert "第一章" in archive.read("OEBPS/nav.xhtml").decode()

    replacement = _planned_record(source_document, source_unit, "第二章").model_copy(
        update={"revision": 2, "accepted_revision": 2}
    )
    assert replacement.review is not None
    replacement = replacement.model_copy(update={"review": replacement.review | {"revision": 2}})
    records[source_unit.unit_id] = replacement
    manifests = _review_manifests(plan, records)
    _coherence_checks(store, documents, records)
    with pytest.raises(EpubValidationError, match="not currently accepted"):
        publish_book(store, tmp_path / "stale-derived.epub", StubChecker())

    records[source_unit.unit_id] = replacement.model_copy(
        update={"accepted_revision": None, "accepted_target_hash": None}
    )
    with pytest.raises(EpubValidationError, match="not currently accepted"):
        publish_book(store, tmp_path / "failed-title.epub", StubChecker())
