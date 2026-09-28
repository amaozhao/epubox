from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from engine.core.markup import parse_xml_safely
from engine.epub.preparation import PreparationConfig, prepare_book, repair_document, resume_preparation
from engine.epub.publication import (
    PackageVerification,
    publish_book,
    publish_verified,
    recover_publication,
    stage_epub,
    validate_assembled_document,
    verify_staged_epub,
)
from engine.epub.validation import (
    EpubChecker,
    EpubCheckResult,
    EpubCheckUnavailable,
    EpubValidationError,
    inspect_epub,
    validate_internal_references,
)
from engine.item.extractor import extract_document
from engine.item.planner import PlannerConfig, PlanningError, plan_unit
from engine.schemas.v23 import ItemStatus, RunConfig, canonical_hash
from engine.services.store import StaleWrite, Store
from tests.v23.book_factory import make_epub


class StubChecker:
    """A deterministic test double; these tests are not evidence that real EPUBCheck ran."""

    def __init__(self, *, warnings: tuple[str, ...] = ()) -> None:
        self.warnings = warnings
        self.paths: list[Path] = []

    def check(self, path: Path) -> EpubCheckResult:
        self.paths.append(path)
        return EpubCheckResult(("stub-epubcheck",), 0, warnings=self.warnings)


def test_preparation_uses_unambiguous_primary_title_and_passes_local_stylesheets(tmp_path: Path) -> None:
    source = make_epub(
        tmp_path / "metadata.epub",
        {
            "chapter.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter</title>'
            '<link rel="stylesheet" href="styles/book.css"/></head><body><p>Text.</p></body></html>'
        },
    )
    opf = (
        _read(source, "OEBPS/content.opf")
        .replace(
            b"</dc:title>",
            b"</dc:title><dc:title>Ambiguous alternate title</dc:title>",
            1,
        )
        .replace(
            b"</manifest>",
            b'<item id="css" href="styles/book.css" media-type="text/css"/></manifest>',
        )
    )
    _rewrite(source, {"OEBPS/content.opf": opf}, {"OEBPS/styles/book.css": b"em { font-style: italic; }"})
    seen_titles: list[str] = []
    seen_styles: list[dict[str, str]] = []

    def capture(markup: str, resource_path: str, source_hash: str, **kwargs):
        config = kwargs.get("config") or {}
        seen_titles.append(str(config.get("book_title", "missing")))
        seen_styles.append(dict(kwargs.get("styles") or {}))
        return extract_document(markup, resource_path, source_hash, **kwargs)

    run = RunConfig(
        model="test-model",
        provider="test-provider",
        prompt_version="test-prompt",
        extractor_version="epubox-extractor-1",
        run_http_limit=0,
    )
    prepare_book(
        source,
        tmp_path / "work",
        PreparationConfig(run, run_id="metadata-run"),
        StubChecker(),
        extract_document=capture,
        plan_unit=plan_unit,
        planner_config=PlannerConfig(context_tokens=8192, max_input_tokens=7000),
    )

    assert seen_titles and set(seen_titles) == {""}
    assert all(styles == {"OEBPS/styles/book.css": "em { font-style: italic; }"} for styles in seen_styles)


@pytest.mark.parametrize("tamper_document", [False, True])
def test_preparation_resumes_same_unready_run_and_revalidates_saved_document(
    tmp_path: Path, tamper_document: bool
) -> None:
    source = make_epub(tmp_path / "resume.epub")
    run = RunConfig(
        model="test-model",
        provider="test-provider",
        prompt_version="test-prompt",
        extractor_version="epubox-extractor-1",
        run_http_limit=0,
    )
    calls: list[str] = []

    def interrupted(markup: str, resource_path: str, source_hash: str, **kwargs):
        calls.append(resource_path)
        if len(calls) == 2:
            raise RuntimeError("injected extraction interruption")
        return extract_document(markup, resource_path, source_hash, **kwargs)

    with pytest.raises(RuntimeError, match="injected"):
        prepare_book(
            source,
            tmp_path / "work",
            PreparationConfig(run, run_id="resume-run"),
            StubChecker(),
            extract_document=interrupted,
            plan_unit=plan_unit,
            planner_config=PlannerConfig(context_tokens=8192, max_input_tokens=7000),
        )

    work_dir = tmp_path / "work" / _sha256(source) / "resume-run"
    partial = Store(work_dir).read_bookplan()
    assert partial.preparation_state == "building"
    assert len(partial.document_hashes) == 1
    if tamper_document:
        document_path = next((work_dir / "documents").glob("*.json"))
        payload = json.loads(document_path.read_text(encoding="utf-8"))
        payload["preparation_issues"] = [{"scope": "document", "code": "tampered", "message": "must not be trusted"}]
        document_path.write_text(json.dumps(payload), encoding="utf-8")
    source.unlink()
    resumed_calls: list[str] = []

    def resumed(markup: str, resource_path: str, source_hash: str, **kwargs):
        resumed_calls.append(resource_path)
        return extract_document(markup, resource_path, source_hash, **kwargs)

    prepared = resume_preparation(
        work_dir,
        StubChecker(),
        extract_document=resumed,
        plan_unit=plan_unit,
        planner_config=PlannerConfig(context_tokens=8192, max_input_tokens=7000),
    )
    assert prepared.bookplan.preparation_state == "ready"
    assert (calls[0] in resumed_calls) is tamper_document
    assert set(prepared.bookplan.unit_documents) == set(prepared.bookplan.unit_ids)

    with pytest.raises(StaleWrite, match="ready preparation"):
        resume_preparation(
            work_dir,
            StubChecker(),
            extract_document=extract_document,
            plan_unit=plan_unit,
            planner_config=PlannerConfig(context_tokens=8192, max_input_tokens=7000),
        )


def test_unplannable_unit_is_recorded_without_blocking_ready_inventory(tmp_path: Path) -> None:
    source = make_epub(
        tmp_path / "planning.epub",
        {"chapter.xhtml": "<p>Cannot plan this.</p><p>Independent unit continues.</p>"},
    )
    run = RunConfig(
        model="test-model",
        provider="test-provider",
        prompt_version="test-prompt",
        extractor_version="epubox-extractor-1",
        run_http_limit=0,
    )

    def selective_plan(unit, planner_config, epoch=0):
        if "Cannot plan" in unit.source_projection:
            raise PlanningError("injected unit planning failure")
        return plan_unit(unit, planner_config, epoch=epoch)

    prepared = prepare_book(
        source,
        tmp_path / "work",
        PreparationConfig(run, run_id="planning-run"),
        StubChecker(),
        extract_document=extract_document,
        plan_unit=selective_plan,
        planner_config=PlannerConfig(context_tokens=8192, max_input_tokens=7000),
    )
    store = Store(prepared.work_dir)
    records = [store.load_unit(unit_id) for unit_id in prepared.bookplan.unit_ids]
    unplanned = [record for record in records if record.cut_plan is None]
    assert len(unplanned) == 1
    assert unplanned[0].input_hash is None and not unplanned[0].items
    assert unplanned[0].unresolved_issues[0].code == "unit_planning_failed"
    assert any(record.cut_plan is not None for record in records)
    assert prepared.bookplan.required_unit_count == len(records)
    owner = prepared.bookplan.unit_documents[unplanned[0].unit_id]
    document = store.read_document(owner, expected_hash=prepared.bookplan.document_hashes[owner])
    assert any(issue.get("unit_id") == unplanned[0].unit_id for issue in document.preparation_issues)


def test_prepare_commits_snapshot_documents_units_then_ready_bookplan(tmp_path: Path) -> None:
    source = make_epub(
        tmp_path / "book.epub",
        {
            "chapter1.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en">'
            '<head><title>Chapter 1</title></head><body><h1 id="title">Reliable systems</h1>'
            '<p>Translate this.</p><p id="keep" translate="no">Keep this English.</p></body></html>',
            "chapter2.xhtml": "<h1>Recovery</h1><p>Continue independent work.</p>",
        },
    )
    run = RunConfig(
        model="test-model",
        provider="test-provider",
        prompt_version="test-prompt",
        extractor_version="epubox-extractor-1",
        run_http_limit=100,
    )
    prepared = prepare_book(
        source,
        tmp_path / "work",
        PreparationConfig(run, run_id="run-1"),
        StubChecker(warnings=("WARNING synthetic source warning",)),
        extract_document=extract_document,
        plan_unit=plan_unit,
        planner_config=PlannerConfig(context_tokens=8192, max_input_tokens=7000),
    )

    store = Store(prepared.work_dir)
    loaded = store.read_bookplan(ready=True)
    assert loaded.source_hash == _sha256(prepared.source_snapshot)
    assert loaded.required_unit_count == len(loaded.unit_ids) > 0
    assert set(loaded.unit_documents) == set(loaded.unit_ids)
    assert set(loaded.initial_coherence_limits) == set(loaded.document_hashes)
    assert set(loaded.initial_coherence_windows) == set(loaded.document_hashes)
    assert loaded.preparation_issues[0]["severity"] == "warning"
    run_http_limit = loaded.frozen_config["run_http_limit"]
    assert isinstance(run_http_limit, int) and run_http_limit > 0
    assert all(store.load_unit(unit_id).record_version == 0 for unit_id in loaded.unit_ids)
    assert prepared.source_snapshot.read_bytes() == source.read_bytes()

    output = tmp_path / "identity.epub"
    published = publish_book(store, {}, output, StubChecker(), identity=True)
    assert published["path"] == str(output)
    assert b"zh-Hans" not in _read(output, "OEBPS/chapter1.xhtml")
    assert b"Reliable systems" in _read(output, "OEBPS/content.opf")

    documents = [
        store.read_document(document_id, expected_hash=document_hash)
        for document_id, document_hash in loaded.document_hashes.items()
    ]
    derived_sources = {
        str(binding["unit_id"]): str(binding["source_unit_id"])
        for document in documents
        for binding in document.derived_bindings
        if binding.get("kind") == "derived_navigation"
    }
    units = {unit.unit_id: unit for document in documents for unit in document.units}
    assert derived_sources
    targets: dict[str, str] = {}
    for document in documents:
        for unit in document.units:
            if unit.unit_id in derived_sources:
                continue
            target = unit.source_projection
            target_hash = canonical_hash(target)
            record = store.load_unit(unit.unit_id)
            assert record.cut_plan is not None
            items = {
                segment.item_id: record.items[segment.item_id].model_copy(
                    update={
                        "status": ItemStatus.REVIEWED,
                        "target_projection": segment.source_projection,
                        "target_hash": canonical_hash(segment.source_projection),
                    }
                )
                for segment in record.cut_plan.segments
            }
            accepted = record.model_copy(
                update={
                    "items": items,
                    "candidate": target,
                    "target_hash": target_hash,
                    "accepted_revision": record.revision,
                    "accepted_target_hash": target_hash,
                    "local_checks": {"passed": True, "target_hash": target_hash},
                    "review": {
                        "passed": True,
                        "revision": record.revision,
                        "input_hash": record.input_hash,
                        "target_hash": target_hash,
                    },
                }
            )
            store.save_unit(accepted)
            targets[unit.unit_id] = target

    for unit_id, source_id in derived_sources.items():
        source_record = store.load_unit(source_id)
        target = units[unit_id].source_projection
        record = store.load_unit(unit_id)
        derived = record.model_copy(
            update={
                "derived": {
                    "state": "valid",
                    "source_unit_id": source_id,
                    "source_revision": source_record.revision,
                    "source_target_hash": source_record.target_hash,
                    "target": target,
                    "target_hash": canonical_hash(target),
                }
            }
        )
        store.save_unit(derived)
        targets[unit_id] = target

    translated = tmp_path / "translated.epub"
    publish_book(store, targets, translated, StubChecker())
    chapter = parse_xml_safely(_read(translated, "OEBPS/chapter1.xhtml").decode("utf-8"))
    root = chapter.getroot()
    kept = next(element for element in root.iter() if element.get("id") == "keep")
    translated_paragraph = next(element for element in root.iter() if (element.text or "") == "Translate this.")
    assert root.get("lang") == "zh-Hans"
    assert translated_paragraph.get("lang") == "zh-Hans"
    assert kept.get("lang") == "en"
    assert b"dcterms:modified" in _read(translated, "OEBPS/content.opf")

    source_id = next(iter(derived_sources.values()))
    source_record = store.load_unit(source_id)
    store.save_unit(
        source_record.model_copy(
            update={"revision": source_record.revision + 1, "accepted_revision": None, "accepted_target_hash": None}
        )
    )
    with pytest.raises(EpubValidationError) as error:
        publish_book(store, targets, tmp_path / "stale-derived.epub", StubChecker())
    assert error.value.code == "unit_not_accepted"

    repair_target = documents[0]
    document_path = prepared.work_dir / "documents" / f"{repair_target.document_id}.json"
    unit_bytes = {path.name: path.read_bytes() for path in (prepared.work_dir / "units").glob("*.json")}
    document_path.write_text("corrupt", encoding="utf-8")
    restored_hash = repair_document(
        store,
        repair_target.document_id,
        StubChecker(),
        extract_document=extract_document,
        plan_unit=plan_unit,
        planner_config=PlannerConfig(context_tokens=8192, max_input_tokens=7000),
    )
    assert restored_hash == loaded.document_hashes[repair_target.document_id]
    assert store.read_document(repair_target.document_id) == repair_target
    assert unit_bytes == {path.name: path.read_bytes() for path in (prepared.work_dir / "units").glob("*.json")}

    def changed_extractor(*args, **kwargs):
        document = extract_document(*args, **kwargs)
        if document.document_id == repair_target.document_id:
            return document.model_copy(
                update={"preparation_issues": (*document.preparation_issues, {"code": "changed"})}
            )
        return document

    document_path.write_text("corrupt again", encoding="utf-8")
    with pytest.raises(ValueError, match="start a new run"):
        repair_document(
            store,
            repair_target.document_id,
            StubChecker(),
            extract_document=changed_extractor,
            plan_unit=plan_unit,
            planner_config=PlannerConfig(context_tokens=8192, max_input_tokens=7000),
        )
    assert document_path.read_text(encoding="utf-8") == "corrupt again"


def test_inventory_covers_epub3_documents_and_records_warnings(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "book.epub")
    checker = StubChecker(warnings=("WARNING test warning",))

    inventory = inspect_epub(source, _sha256(source), checker=checker)

    assert inventory.epub_version == "3.0"
    assert inventory.spine == ("c0", "c1")
    assert set(inventory.documents) == {
        "OEBPS/chapter1.xhtml",
        "OEBPS/chapter2.xhtml",
        "OEBPS/nav.xhtml",
    }
    assert inventory.ncx_path == "OEBPS/toc.ncx"
    assert inventory.nav_path == "OEBPS/nav.xhtml"
    assert inventory.warnings[0].severity == "warning"


def test_source_error_blocks_and_zip_traversal_is_rejected(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "book.epub")

    class ErrorChecker(StubChecker):
        def check(self, path: Path) -> EpubCheckResult:
            return EpubCheckResult(("stub-epubcheck",), 1, errors=("ERROR broken package",))

    with pytest.raises(EpubValidationError, match="ERROR/FATAL"):
        inspect_epub(source, _sha256(source), checker=ErrorChecker())

    unsafe = tmp_path / "unsafe.epub"
    unsafe.write_bytes(source.read_bytes())
    with zipfile.ZipFile(unsafe, "a") as archive:
        archive.writestr("../escape.xhtml", "bad")
    with pytest.raises(EpubValidationError) as error:
        inspect_epub(unsafe, _sha256(unsafe), checker=StubChecker())
    assert error.value.code == "unsafe_path"

    with pytest.raises(EpubCheckUnavailable) as unavailable:
        EpubChecker((str(tmp_path / "missing-epubcheck"),)).check(source)
    assert unavailable.value.code == "epubcheck_unavailable"


def test_unsupported_reading_content_is_blocked(tmp_path: Path) -> None:
    source = make_epub(
        tmp_path / "scripted.epub",
        {"chapter.xhtml": "<script>document.write('reading text')</script><p>Fallback</p>"},
    )
    with pytest.raises(EpubValidationError) as error:
        inspect_epub(source, _sha256(source), checker=StubChecker())
    assert any(issue.code == "scripted_content" for issue in error.value.issues)

    fixed = make_epub(tmp_path / "fixed.epub")
    opf = _read(fixed, "OEBPS/content.opf").replace(
        b"<metadata ",
        b'<metadata><meta property="rendition:layout">pre-paginated</meta></metadata><metadata ',
    )
    _rewrite(fixed, {"OEBPS/content.opf": opf})
    with pytest.raises(EpubValidationError) as error:
        inspect_epub(fixed, _sha256(fixed), checker=StubChecker())
    assert any(issue.code == "fixed_layout" for issue in error.value.issues)


def test_font_obfuscation_is_preserved_but_encrypted_body_is_blocked(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "font.epub")
    opf = _read(source, "OEBPS/content.opf").replace(
        b"</manifest>",
        b'<item id="font" href="font.otf" media-type="font/otf"/></manifest>',
    )
    encryption = (
        b'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
        b'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
        b'<enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>'
        b'<enc:CipherData><enc:CipherReference URI="OEBPS/font.otf"/></enc:CipherData>'
        b"</enc:EncryptedData></encryption>"
    )
    _rewrite(
        source,
        {"OEBPS/content.opf": opf},
        {"OEBPS/font.otf": b"font-bytes", "META-INF/encryption.xml": encryption},
    )

    inventory = inspect_epub(source, _sha256(source), checker=StubChecker())
    assert inventory.obfuscated_fonts == ("OEBPS/font.otf",)

    encrypted_body = encryption.replace(
        b"http://www.idpf.org/2008/embedding",
        b"http://www.w3.org/2001/04/xmlenc#aes256-cbc",
    ).replace(b"OEBPS/font.otf", b"OEBPS/chapter1.xhtml")
    _rewrite(source, {}, {"META-INF/encryption.xml": encrypted_body})
    with pytest.raises(EpubValidationError) as error:
        inspect_epub(source, _sha256(source), checker=StubChecker())
    assert any(issue.code == "encrypted_content" for issue in error.value.issues)


def test_multirendition_smil_signature_and_external_entity_are_blocked(tmp_path: Path) -> None:
    multiple = make_epub(tmp_path / "multiple.epub")
    container = _read(multiple, "META-INF/container.xml").replace(
        b"</rootfiles>",
        b'<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>',
    )
    _rewrite(multiple, {"META-INF/container.xml": container})
    with pytest.raises(EpubValidationError) as error:
        inspect_epub(multiple, _sha256(multiple), checker=StubChecker())
    assert error.value.code == "multiple_renditions"

    smil = make_epub(tmp_path / "smil.epub")
    opf = (
        _read(smil, "OEBPS/content.opf")
        .replace(b'id="c0"', b'id="c0" media-overlay="overlay"')
        .replace(
            b"</manifest>",
            b'<item id="overlay" href="overlay.smil" media-type="application/smil+xml"/></manifest>',
        )
    )
    _rewrite(smil, {"OEBPS/content.opf": opf}, {"OEBPS/overlay.smil": b"<smil/>"})
    with pytest.raises(EpubValidationError) as error:
        inspect_epub(smil, _sha256(smil), checker=StubChecker())
    assert any(issue.code == "media_overlay" for issue in error.value.issues)

    signed = make_epub(tmp_path / "signed.epub")
    _rewrite(signed, {}, {"META-INF/signatures.xml": b"<signatures/>"})
    with pytest.raises(EpubValidationError) as error:
        inspect_epub(signed, _sha256(signed), checker=StubChecker())
    assert any(issue.code == "signature" for issue in error.value.issues)

    entity = make_epub(
        tmp_path / "entity.epub",
        {
            "chapter.xhtml": '<!DOCTYPE html [<!ENTITY leak SYSTEM "file:///etc/passwd">]>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter</title></head>'
            "<body><p>&leak;</p></body></html>"
        },
    )
    with pytest.raises(EpubValidationError) as error:
        inspect_epub(entity, _sha256(entity), checker=StubChecker())
    assert error.value.code == "invalid_xml"


def test_staging_preserves_resources_and_independently_verifies_document(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": '<p id="p">Hello</p>'})
    checker = StubChecker()
    inventory = inspect_epub(source, _sha256(source), checker=checker)
    original = _read(source, "OEBPS/chapter.xhtml")
    changed = original.replace(b">Hello<", ">你好<".encode())
    staged = tmp_path / "staging" / "candidate.epub"

    stage_hash = stage_epub(source, staged, {"OEBPS/chapter.xhtml": changed})
    verification = verify_staged_epub(
        source,
        staged,
        inventory,
        {"OEBPS/chapter.xhtml": changed},
        accepted_targets={"OEBPS/chapter.xhtml": {"u1": "你好"}},
        checker=checker,
    )

    assert verification.output_hash == stage_hash
    with zipfile.ZipFile(staged) as archive:
        assert archive.infolist()[0].filename == "mimetype"
        assert archive.infolist()[0].compress_type == zipfile.ZIP_STORED
        assert archive.read("OEBPS/toc.ncx") == _read(source, "OEBPS/toc.ncx")


def test_final_reference_check_includes_css_urls(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "css.epub")
    opf = _read(source, "OEBPS/content.opf").replace(
        b"</manifest>",
        b'<item id="css" href="styles/main.css" media-type="text/css"/></manifest>',
    )
    _rewrite(source, {"OEBPS/content.opf": opf}, {"OEBPS/styles/main.css": b"p{background:url(../missing.png)}"})
    inventory = inspect_epub(source, _sha256(source), checker=StubChecker())

    with zipfile.ZipFile(source) as archive:
        issues = validate_internal_references(archive, inventory)

    assert any(issue.code == "missing_reference" and issue.resource == "OEBPS/styles/main.css" for issue in issues)


def test_document_validation_reads_actual_slots_and_frozen_attributes() -> None:
    source = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter</title></head>'
        '<body><p id="p"><a href="note.xhtml#n">Hello</a></p></body></html>'
    )
    document = extract_document(source, "OEBPS/chapter.xhtml", "source-hash")
    paragraph = next(unit for unit in document.units if unit.kind == "paragraph")
    targets = {
        unit.unit_id: unit.source_projection.replace("Hello", "你好")
        if unit.unit_id == paragraph.unit_id
        else unit.source_projection
        for unit in document.units
    }
    actual = source.replace("Hello", "你好")

    validate_assembled_document(document, targets, actual)

    with pytest.raises(EpubValidationError) as error:
        validate_assembled_document(document, targets, actual.replace("note.xhtml#n", "wrong.xhtml"))
    assert error.value.code == "frozen_attribute_changed"
    with pytest.raises(EpubValidationError) as error:
        validate_assembled_document(document, targets, actual.replace("你好", ""))
    assert error.value.code == "target_text_mismatch"
    with pytest.raises(EpubValidationError) as error:
        validate_assembled_document(document, targets, actual.replace("你好", "你好你好"))
    assert error.value.code == "target_text_mismatch"


def test_publish_is_atomic_refuses_alias_and_recovers_completed_intent(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "source.epub")
    staged = tmp_path / "staged.epub"
    staged.write_bytes(source.read_bytes())
    digest = _sha256(staged)
    verification = PackageVerification(digest, EpubCheckResult(("stub",), 0), ())
    publish_json = tmp_path / "work" / "publish.json"
    output = tmp_path / "result.epub"

    intent = publish_verified(
        staged,
        output,
        publish_json,
        run_id="run-1",
        plan_fingerprint="plan-1",
        version_vector={"u1": 2},
        verification=verification,
        forbidden_paths=(source,),
    )
    assert intent["state"] == "completed"
    assert _sha256(output) == digest
    assert recover_publication(
        publish_json,
        plan_fingerprint="plan-1",
        version_vector={"u1": 2},
    ) == json.loads(publish_json.read_text(encoding="utf-8"))

    with pytest.raises(FileExistsError):
        publish_verified(
            staged,
            output,
            publish_json,
            run_id="run-1",
            plan_fingerprint="plan-1",
            version_vector={"u1": 2},
            verification=verification,
        )
    with pytest.raises(EpubValidationError) as error:
        publish_verified(
            staged,
            source,
            publish_json,
            run_id="run-1",
            plan_fingerprint="plan-1",
            version_vector={"u1": 2},
            verification=verification,
            forbidden_paths=(source,),
            overwrite=True,
        )
    assert error.value.code == "unsafe_output_path"


def _read(path: Path, resource: str) -> bytes:
    with zipfile.ZipFile(path) as archive:
        return archive.read(resource)


def _rewrite(path: Path, replacements: dict[str, bytes], additions: dict[str, bytes] | None = None) -> None:
    temporary = path.with_suffix(".tmp")
    additions = additions or {}
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(temporary, "w") as target:
        existing = set(source.namelist())
        for info in source.infolist():
            target.writestr(
                info, additions.get(info.filename, replacements.get(info.filename, source.read(info.filename)))
            )
        for name, value in additions.items():
            if name not in existing:
                target.writestr(name, value)
    temporary.replace(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
