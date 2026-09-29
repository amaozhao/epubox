from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import engine.epub.preparation as preparation_module
from engine.core.config import settings
from engine.epub.preparation import PreparationConfig, prepare_book
from engine.epub.validation import EpubCheckResult
from engine.item.extractor import extract_document
from engine.services.store import RunStore
from engine.services.term_planning import TERM_PLANNER_VERSION, plan_term_extraction
from tests.v23.book_factory import make_epub


class StubChecker:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def check(self, path: Path) -> EpubCheckResult:
        self.paths.append(path)
        return EpubCheckResult(("stub-epubcheck",), 0)


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
            extraction_config={"prompt_version": "epubox-terms-1"},
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
        "prompt_version": "epubox-terms-1",
        "auto_extract": True,
        "strategy": TERM_PLANNER_VERSION,
        "provider": "agnes",
        "model": settings.AGNES_MODEL,
        "target_language": "zh-Hans",
    }
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
