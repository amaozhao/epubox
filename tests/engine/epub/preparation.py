from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import engine.epub.preparation as preparation_module
import engine.services.preparation as pipeline_module
from engine.agents.runtime import PROMPT_VERSION, RESOLUTION_PROTOCOL_VERSION, TERM_PROMPT_VERSION
from engine.core.config import settings
from engine.epub.preparation import PreparationConfig, prepare_book
from engine.epub.validation import EpubCheckResult, EpubValidationError
from engine.item.atoms import ADAPTER_VERSION as ATOMIC_ADAPTER_VERSION
from engine.item.atoms import EXTRACTOR_VERSION as ATOMIC_EXTRACTOR_VERSION
from engine.item.extractor import extract_document
from engine.services.atomic import IdentityMismatch
from engine.services.store import RunStore
from engine.services.terms.planning import ATOMIC_TERM_PLANNER_VERSION, TERM_PLANNER_VERSION, plan_term_extraction
from tests.engine.epub.factory import make_epub


class StubChecker:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def check(self, path: Path) -> EpubCheckResult:
        self.paths.append(path)
        return EpubCheckResult(("stub-epubcheck",), 0)


def test_snapshot_rejects_source_that_changes_during_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"first")
    work = tmp_path / "work"
    work.mkdir()
    copy = preparation_module.shutil.copyfileobj

    def changing_copy(input_file, output_file):
        copy(input_file, output_file)
        source.write_bytes(source.read_bytes() + b"x")

    monkeypatch.setattr(preparation_module.shutil, "copyfileobj", changing_copy)
    with pytest.raises(OSError, match="kept changing"):
        preparation_module._stable_snapshot(source, work)
    assert not list(work.glob(".source-*.tmp"))


@pytest.mark.parametrize("contents", [b"not a zip", None])
def test_p1_rejects_invalid_epub_before_commit(tmp_path: Path, contents: bytes | None) -> None:
    source = tmp_path / "bad.epub"
    if contents is None:
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
    else:
        source.write_bytes(contents)

    with pytest.raises((zipfile.BadZipFile, EpubValidationError)):
        prepare_book(source, tmp_path / "work", PreparationConfig(run_id="bad"), StubChecker())
    assert not list((tmp_path / "work").glob("*/bad/preparation.json"))


def test_p1_snapshots_complete_source_inventory_and_commits_parsed_ready_last(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "book.epub")
    terms = tmp_path / "terms.json"
    terms.write_text(json.dumps({"RAM": "内存"}))
    checker = StubChecker()
    prepared = prepare_book(
        source,
        tmp_path / "work",
        PreparationConfig(
            run_id="run-1",
            user_terms_path=terms,
            extraction_config={"prompt_version": TERM_PROMPT_VERSION},
            translation_config={"target_language": "zh-Hans"},
        ),
        checker,
    )

    store = RunStore(prepared.work_dir)
    loaded = store.read_preparation()
    expected_paths = set(prepared.inventory.documents) | {
        prepared.inventory.opf_path,
        prepared.inventory.ncx_path,
    }
    expected_paths.discard(None)
    documents = [store.read_document(document_id) for document_id in loaded.document_hashes]

    assert loaded.state == "parsed_ready"
    assert loaded == prepared.preparation
    assert (
        prepared.preparation_hash == hashlib.sha256((prepared.work_dir / "preparation.json").read_bytes()).hexdigest()
    )
    assert prepared.source_snapshot.read_bytes() == source.read_bytes()
    assert not prepared.source_snapshot.stat().st_mode & 0o222
    assert {document.resource.path for document in documents} == expected_paths
    assert set(loaded.unit_documents) == {unit.unit_id for document in documents for unit in document.units}
    assert tuple(store.read_user_terms().terms) == loaded.user_terms
    assert loaded.user_terms[0].mode == "preferred"
    assert loaded.extraction_config == {
        "prompt_version": TERM_PROMPT_VERSION,
        "resolution_protocol_version": RESOLUTION_PROTOCOL_VERSION,
        "auto_extract": True,
        "strategy": TERM_PLANNER_VERSION,
        "provider": "agnes",
        "model": settings.AGNES_MODEL,
        "target_language": "zh-Hans",
    }
    assert loaded.translation_config["prompt_version"] == PROMPT_VERSION
    assert checker.paths == [prepared.source_snapshot]
    assert not (prepared.work_dir / "bookplan.json").exists()
    assert not list((prepared.work_dir / "units").glob("*.json"))
    assert not list((prepared.work_dir / "requests").glob("*.json"))

    ordered_ids = (
        *loaded.reading_order,
        *(item for item in loaded.document_hashes if item not in loaded.reading_order),
    )
    term_plan = plan_term_extraction(
        tuple(store.read_document(document_id) for document_id in ordered_ids),
        loaded.user_terms,
        source_hash=loaded.source_hash,
        preparation_hash=prepared.preparation_hash,
        reading_edges=tuple(zip(loaded.reading_order, loaded.reading_order[1:], strict=False)),
        extraction_identity=loaded.extraction_config,
    ).plan
    assert store.write_term_plan(term_plan)


def test_p1_interruption_leaves_no_ready_marker_and_replays_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_epub(tmp_path / "book.epub")
    work_root = tmp_path / "work"
    config = PreparationConfig(run_id="resume-run")
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected extraction interruption")
        return extract_document(*args, **kwargs)

    monkeypatch.setattr(preparation_module, "extract_document", interrupted)
    with pytest.raises(RuntimeError, match="injected"):
        prepare_book(source, work_root, config, StubChecker())

    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    work_dir = work_root / source_hash / "resume-run"
    assert not (work_dir / "preparation.json").exists()
    assert not list((work_dir / "documents").glob("*.json"))
    assert not (work_dir / "bookplan.json").exists()

    monkeypatch.setattr(preparation_module, "extract_document", extract_document)
    prepared = prepare_book(source, work_root, config, StubChecker())
    assert prepared.preparation.state == "parsed_ready"
    assert prepared.source_snapshot.read_bytes() == source.read_bytes()
    assert not (work_dir / "bookplan.json").exists()


def test_bad_user_scope_never_commits_parsed_ready(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "book.epub")
    terms = tmp_path / "terms.json"
    terms.write_text(
        json.dumps(
            [
                {
                    "source": "RAM",
                    "target": "内存",
                    "scope": {"kind": "units", "unit_ids": ["ghost-unit"]},
                }
            ]
        )
    )
    work_root = tmp_path / "work"

    with pytest.raises(ValueError, match="unknown Unit"):
        prepare_book(
            source,
            work_root,
            PreparationConfig(run_id="bad-terms", user_terms_path=terms),
            StubChecker(),
        )

    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    work_dir = work_root / source_hash / "bad-terms"
    assert not (work_dir / "preparation.json").exists()
    assert not list((work_dir / "requests").glob("*.json"))
    assert not (work_dir / "bookplan.json").exists()


def test_p1_freezes_the_provider_model_id_without_credentials(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "book.epub")
    prepared = prepare_book(
        source,
        tmp_path / "work",
        PreparationConfig(run_id="provider-model", translation_config={"provider": "cr_proxy"}),
        StubChecker(),
    )

    config = prepared.preparation.extraction_config
    assert config["provider"] == "cr_proxy"
    assert config["model"] == settings.CR_PROXY_MODEL
    assert not any("key" in name.casefold() for name in config)


def test_atomic_p1_uses_raw_resources_and_freezes_member_budget(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "book.epub")
    terms = tmp_path / "terms.json"
    terms.write_text(
        json.dumps([{"source": "resource", "target": "资源", "note": " preserve spacing "}]),
        encoding="utf-8",
    )
    prepared = prepare_book(
        source,
        tmp_path / "work",
        PreparationConfig(
            run_id="atomic",
            user_terms_path=terms,
            adapter_version=ATOMIC_ADAPTER_VERSION,
            extractor_version=ATOMIC_EXTRACTOR_VERSION,
            translation_config={"model": "fake"},
        ),
        StubChecker(),
    )
    store = RunStore(prepared.work_dir)
    documents = [store.read_document(document_id) for document_id in prepared.preparation.document_hashes]

    assert {document.extractor_version for document in documents} == {ATOMIC_EXTRACTOR_VERSION}
    assert prepared.preparation.extraction_config["strategy"] == ATOMIC_TERM_PLANNER_VERSION
    assert prepared.preparation.user_terms[0].note == " preserve spacing "
    assert prepared.preparation.translation_config == {
        "model": "fake",
        "provider": "agnes",
        "target_language": "zh-Hans",
        "max_source_tokens": settings.EPUB_CHUNK_MAX_TOKENS,
        "context_tokens": 32768,
        "max_input_tokens": 32768,
        "max_output_tokens": 4096,
        "prompt_version": "epubox-members-1",
        "planner_version": "epubox-member-planner-1",
        "input_budget_version": 2,
    }
    assert not any(
        binding.get("kind") == "derived_navigation" for document in documents for binding in document.derived_bindings
    )


def test_p1_reuse_rejects_changed_user_terms(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "book.epub")
    terms = tmp_path / "terms.json"
    terms.write_text('{"RAM":"内存"}')
    config = PreparationConfig(run_id="same-run", user_terms_path=terms)
    prepare_book(source, tmp_path / "work", config, StubChecker())
    terms.write_text('{"RAM":"随机存取存储器"}')

    with pytest.raises(IdentityMismatch, match="resume configuration differs"):
        pipeline_module._p1(source, tmp_path / "work", config, StubChecker())


def test_p1_resolves_derived_navigation_before_immutable_document_write(tmp_path: Path) -> None:
    source = make_epub(
        tmp_path / "book.epub",
        {
            "chapter.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter 1</title></head>'
            '<body><h1 id="chapter">Chapter 1</h1><p>Body.</p></body></html>'
        },
    )
    config = PreparationConfig(run_id="derived-navigation")
    first = prepare_book(source, tmp_path / "work", config, StubChecker())
    store = RunStore(first.work_dir)
    documents = [store.read_document(document_id) for document_id in first.preparation.document_hashes]
    derived = [
        binding
        for document in documents
        for binding in document.derived_bindings
        if binding.get("kind") == "derived_navigation"
    ]

    assert derived
    original_hashes = first.preparation.document_hashes
    resumed = prepare_book(source, tmp_path / "work", config, StubChecker())
    assert resumed.preparation.document_hashes == original_hashes
