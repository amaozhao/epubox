from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import engine.services.ready as ready_module
from engine.epub.preparation import PreparationConfig
from engine.item.atoms import EXTRACTOR_VERSION as ATOMIC_EXTRACTOR_VERSION
from engine.services.atomic import IdentityMismatch
from engine.services.preparation import PreparationProgress, prepare_translation, resume_preparation
from engine.services.store import RunStore
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker


def atomic_config(**translation) -> PreparationConfig:
    return PreparationConfig(
        run_id="atomic-run",
        auto_extract=False,
        translation_config={"model": "fake", **translation},
    )


def forbidden(*_args, **_kwargs):
    raise AssertionError("model transport must not be called")


def test_atomic_default_commits_ready_last_and_resumes_without_http(tmp_path: Path) -> None:
    source = make_epub(
        tmp_path / "source.epub",
        {"chapter.xhtml": '<h1 id="chapter">Chapter 1</h1><p>Translate this text.</p>'},
    )
    progress: list[PreparationProgress] = []
    first = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            atomic_config(),
            StubChecker(),
            term_transport=forbidden,
            resolution_transport=forbidden,
            progress=progress.append,
        )
    )
    store = RunStore(first.work_dir)

    assert first.status == "ready" and first.prepared is not None and first.bookplan is None
    assert (first.work_dir / "prepared.json").is_file()
    assert (first.work_dir / "plans" / "book.json").is_file()
    assert not (first.work_dir / "bookplan.json").exists()
    assert not any(
        binding.get("kind") == "derived_navigation"
        for document_id in first.prepared.preparation.document_hashes
        for binding in store.read_document(document_id).derived_bindings
    )
    assert all(
        store.read_document(document_id).extractor_version == ATOMIC_EXTRACTOR_VERSION
        for document_id in first.prepared.preparation.document_hashes
    )
    assert any(event.phase == "p4" and event.planned == len(first.prepared.plan.member_hashes) for event in progress)

    resumed = asyncio.run(
        resume_preparation(
            first.work_dir,
            StubChecker(),
            term_transport=forbidden,
            resolution_transport=forbidden,
        )
    )
    assert resumed.prepared == first.prepared


def test_interruption_before_ready_marker_replays_local_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Translate this text.</p>"})
    original = ready_module.write_ready

    def interrupted(*_args, **_kwargs):
        raise OSError("injected ready interruption")

    monkeypatch.setattr(ready_module, "write_ready", interrupted)
    with pytest.raises(OSError, match="ready interruption"):
        asyncio.run(prepare_translation(source, tmp_path / "work", atomic_config(), StubChecker()))
    work_dir = next((tmp_path / "work").glob("*/atomic-run"))
    assert not (work_dir / "prepared.json").exists()

    monkeypatch.setattr(ready_module, "write_ready", original)
    resumed = asyncio.run(resume_preparation(work_dir, StubChecker(), term_transport=forbidden))
    assert resumed.status == "ready" and resumed.prepared is not None


def test_ready_resume_rejects_changed_dependency_before_http(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Translate this text.</p>"})
    result = asyncio.run(prepare_translation(source, tmp_path / "work", atomic_config(), StubChecker()))
    member = next((result.work_dir / "members").glob("*.json"))
    member.write_text(
        member.read_text(encoding="utf-8").replace('"source_projection":"', '"source_projection":"x', 1),
        encoding="utf-8",
    )

    with pytest.raises(IdentityMismatch, match="member changed|dependency"):
        asyncio.run(resume_preparation(result.work_dir, StubChecker(), term_transport=forbidden))


def test_virtual_preflight_pieces_are_packed_as_distinct_members(tmp_path: Path) -> None:
    text = " ".join(f"Sentence {index} stays concise." for index in range(50))
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": f"<div>{text}</div>"}, version="2.0")
    result = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            atomic_config(
                max_source_tokens=100,
                context_tokens=8192,
                max_input_tokens=8192,
                planner_version="epubox-member-planner-1",
                output_budget_version=4,
            ),
            StubChecker(),
        )
    )

    assert result.status == "ready" and result.prepared is not None, (
        result.phase,
        result.reason,
        [(item.resource_path, item.atomic_tag, item.source_tokens) for item in result.diagnostics],
    )
    assert any(len(member_ids) > 1 for member_ids in result.prepared.plan.unit_members.values())
    assert set(result.prepared.plan.member_hashes) == {
        path.stem for path in (result.work_dir / "members").glob("*.json")
    }


def test_blocked_hard_atom_stops_before_term_http_or_ready(tmp_path: Path) -> None:
    source = make_epub(
        tmp_path / "source.epub",
        {"chapter.xhtml": f"<p>{'word ' * 200}</p>"},
        version="2.0",
    )
    result = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            atomic_config(
                max_source_tokens=40,
                context_tokens=8192,
                max_input_tokens=8192,
                planner_version="epubox-member-planner-1",
                output_budget_version=4,
            ),
            StubChecker(),
            term_transport=forbidden,
        )
    )

    assert result.status == "needs_attention" and result.phase == "preflight"
    assert result.reason == "atomic preflight blocked 1 item(s)"
    assert len(result.diagnostics) == 1 and result.diagnostics[0].atomic_tag == "p"
    assert not (result.work_dir / "prepared.json").exists()
    assert not list((result.work_dir / "requests").glob("*.json"))
