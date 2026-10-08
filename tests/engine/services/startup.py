from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace

import pytest

import engine.item.members as members_module
import engine.services.ready as ready_module
from engine.execution.repair import run_translation
from engine.schemas.contracts import (
    ExtractionItem,
    TermExtractionPlan,
    TermExtractionRecord,
    canonical_json_bytes,
    term_plan_hash,
)
from engine.services import state
from engine.services.journal import BodyJournal
from engine.services.preparation import _initialize_extraction_records, prepare_translation, resume_preparation
from engine.services.store import RunStore
from engine.services.terms.runner import TermRunner, TermRunResult
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker
from tests.engine.services.preparing import atomic_config


class FirstProviderCall(RuntimeError):
    pass


def test_resume_reports_slow_checks_and_reuses_session_until_provider(tmp_path, monkeypatch) -> None:
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Translate this text.</p>"})
    prepared = asyncio.run(prepare_translation(source, tmp_path / "work", atomic_config(), StubChecker()))
    timeline: list[str] = []
    reads = 0
    read_ready = ready_module._read_ready
    load_preflight = ready_module.load_preflight
    pack_members = members_module.pack_members

    def counted_read(*args, **kwargs):
        nonlocal reads
        reads += 1
        return read_ready(*args, **kwargs)

    def counted_preflight(*args, **kwargs):
        timeline.append("preflight")
        return load_preflight(*args, **kwargs)

    def counted_pack(*args, **kwargs):
        timeline.append("pack")
        return pack_members(*args, **kwargs)

    def progress(event) -> None:
        notice = getattr(event, "notice", None) or (event.get("notice") if isinstance(event, dict) else None)
        if notice:
            timeline.append(notice)

    monkeypatch.setattr(ready_module, "_read_ready", counted_read)
    monkeypatch.setattr(ready_module, "load_preflight", counted_preflight)
    monkeypatch.setattr(members_module, "pack_members", counted_pack)
    resumed = asyncio.run(resume_preparation(prepared.work_dir, StubChecker(), progress=progress))

    assert resumed.ready_session is not None and reads == 1
    assert timeline.index("准备：验证原文快照和预检记录。") < timeline.index("preflight")
    assert any(item.startswith("准备：源映射已核对，加载 ") for item in timeline)
    assert "pack" not in timeline

    async def first_provider(_stage, _payload):
        raise FirstProviderCall

    with pytest.raises(FirstProviderCall):
        asyncio.run(
            run_translation(
                prepared.work_dir,
                transport=first_provider,
                ready_session=resumed.ready_session,
                progress=progress,
            )
        )
    assert reads == 1


def test_fresh_prepare_and_body_journal_share_one_full_verification(tmp_path, monkeypatch) -> None:
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Translate this text.</p>"})
    verifications = 0
    verify = ready_module._verify

    def counted(*args, **kwargs):
        nonlocal verifications
        verifications += 1
        return verify(*args, **kwargs)

    monkeypatch.setattr(ready_module, "_verify", counted)
    prepared = asyncio.run(prepare_translation(source, tmp_path / "work", atomic_config(), StubChecker()))

    assert prepared.ready_session is not None and verifications == 1
    BodyJournal(RunStore(prepared.work_dir), prepared.ready_session)
    assert verifications == 1


def checkpoint(tmp_path):
    source = tmp_path / "book.epub"
    source.write_bytes(b"epub")
    root = tmp_path / "book"
    state.initialize(root, source, hashlib.sha256(b"epub").hexdigest(), "run")
    items = tuple(
        ExtractionItem(
            item_id=f"te-{number}",
            document_id="document",
            view_ids=(f"view-{number}",),
            primary_ranges=({"view_id": f"view-{number}", "start": 0, "end": 1},),
            extraction_input_hash=f"input-{number}",
        )
        for number in range(20)
    )
    data = {
        "source_hash": "source",
        "preparation_hash": "preparation",
        "auto_extract": True,
        "extraction_http_limit": 120,
        "resolution_group_limit": 0,
        "items": items,
    }
    plan = TermExtractionPlan(**data, plan_hash=term_plan_hash(data))
    state.write(root / "glossary" / "plan.json", canonical_json_bytes(plan))
    return RunStore(root), plan


def test_initialization_commits_once_and_completed_initialization_does_not_write(tmp_path, monkeypatch):
    store, plan = checkpoint(tmp_path)
    existing = store.save_extraction(
        TermExtractionRecord(
            item_id="te-0",
            document_id="document",
            view_ids=("view-0",),
            extraction_input_hash="input-0",
            status="succeeded",
            counters={"http": 3},
        )
    )
    committed = []
    original = state._commit

    def counted(root, value):
        committed.append(root)
        return original(root, value)

    monkeypatch.setattr(state, "_commit", counted)
    _initialize_extraction_records(store, plan)
    assert len(committed) == 1
    assert store.read_extraction("te-0") == existing
    assert len(list(state.glob(store.root / "glossary/extraction", "*.json"))) == 20
    saved = state.read(store.root / "state.json")
    _initialize_extraction_records(store, plan)
    assert len(committed) == 1
    assert state.read(store.root / "state.json") == saved


def test_initialization_rolls_back_all_new_records_on_failure(tmp_path, monkeypatch):
    store, plan = checkpoint(tmp_path)
    original = store.save_extraction

    def fail(record, **kwargs):
        if record.item_id == "te-3":
            raise RuntimeError("interrupted initialization")
        return original(record, **kwargs)

    monkeypatch.setattr(store, "save_extraction", fail)
    with pytest.raises(RuntimeError, match="interrupted initialization"):
        _initialize_extraction_records(store, plan)
    assert not list(state.glob(store.root / "glossary/extraction", "*.json"))


def test_resume_before_ready_reuses_preflight_until_term_execution(tmp_path, monkeypatch):
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Translate this text.</p>"})

    async def paused(self):
        return TermRunResult("paused", 0, 0, len(self.plan.items), 0)

    monkeypatch.setattr(TermRunner, "run", paused)
    first = asyncio.run(
        prepare_translation(source, tmp_path / "work", replace(atomic_config(), auto_extract=True), StubChecker())
    )
    assert first.phase == "terms" and first.status == "paused"
    assert not state.exists(first.work_dir / "prepared.json")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("saved passing preflight must not be rebuilt")

    monkeypatch.setattr("engine.services.preparation.prepare_preflight", forbidden)
    monkeypatch.setattr("engine.services.preflight.extract_resource", forbidden)
    monkeypatch.setattr("engine.services.terms.storage.canonical_documents", forbidden)
    resumed = asyncio.run(resume_preparation(first.work_dir, StubChecker()))
    assert resumed.phase == "terms" and resumed.status == "paused"
