from __future__ import annotations

import zipfile
from typing import Any, cast

import pytest

from engine.epub.publication import _accepted_target, publish_book
from engine.item.inline import plain_text
from engine.orchestrator import (
    TranslationEngine,
)
from engine.schemas.contracts import ItemStatus, Unit, UnitRecord, canonical_hash
from main import _progress_printer
from tests.engine.epub.preparation import StubChecker
from tests.engine.execution.support import (
    PartialBatchTransport,
    ScriptedTransport,
    ready_derived_store,
    ready_store,
)


@pytest.mark.asyncio
async def test_translation_review_manifest_and_resume_are_one_v25_path(tmp_path) -> None:
    store, unit, initial = ready_store(tmp_path)
    first_transport = ScriptedTransport(pause_review=True)
    reports: list[dict] = []
    first = await TranslationEngine(store, transport=first_transport, progress=reports.append).run()

    assert first.status == "paused"
    saved = store.read_unit(unit.unit_id)
    item = saved.items[next(iter(saved.items))]
    assert item.target_projection == "该进程使用内存。"
    assert item.status == ItemStatus.IN_FLIGHT
    assert [stage for stage, _ in first_transport.calls] == ["translate", "review"]
    assert reports[0]["execution_state"] == "running"
    assert reports[-1]["execution_state"] == "stopped"
    assert {"accepted_units", "required_units", "pending_items", "needs_attention_units", "http_attempts"}.issubset(
        reports[-1]
    )

    resumed_transport = ScriptedTransport()
    resumed = await TranslationEngine(store, transport=resumed_transport).run()
    accepted = store.read_unit(unit.unit_id)

    assert resumed.status == "translated"
    assert accepted.accepted_revision == accepted.revision
    assert accepted.review is not None and accepted.review["protocol"] == "epubox-review-2"
    assert _accepted_target(store, store.read_bookplan(), accepted, planned=True) == accepted.candidate
    assert [stage for stage, _ in resumed_transport.calls] == ["review"]
    assert resumed.predicted_http_requests == 2
    manifests = [store.read_request(path.stem) for path in sorted((store.root / "requests").glob("*.json"))]
    assert {manifest.stage for manifest in manifests} >= {"translate", "review"}
    assert all(
        manifest.freeze_id == store.read_bookplan().freeze_id
        for manifest in manifests
        if manifest.stage in {"translate", "review"}
    )
    assert all(
        manifest.terms_hashes and manifest.context_hashes
        for manifest in manifests
        if manifest.stage in {"translate", "review"}
    )
    assert accepted.record_version > initial.record_version

    no_calls = ScriptedTransport()
    again = await TranslationEngine(store, transport=no_calls).run()
    assert again.status == "translated" and no_calls.calls == []


def test_live_progress_separates_translated_items_from_waiting_navigation(tmp_path, capsys) -> None:
    store, unit, _ = ready_store(tmp_path)
    reports: list[dict] = []
    printer = _progress_printer()

    def progress(report: dict) -> None:
        reports.append(report)
        printer(report)

    engine = TranslationEngine(store, transport=ScriptedTransport(), progress=progress)
    record = engine.records[unit.unit_id]
    item_id, item = next(iter(record.items.items()))
    target = "该进程使用内存。"
    engine.records[unit.unit_id] = record.model_copy(
        update={
            "items": {
                item_id: item.model_copy(
                    update={
                        "status": ItemStatus.LOCAL_VALID,
                        "target_projection": target,
                        "target_hash": canonical_hash(target),
                    }
                )
            }
        }
    )
    engine.records["nav"] = UnitRecord(
        unit_id="nav",
        document_id="navigation",
        source_hash="source",
        derived={"state": "blocked_dependency", "source_unit_id": unit.unit_id},
    )
    engine.book = engine.book.model_copy(update={"required_unit_count": 2})

    engine._emit_progress("translation", "running")

    assert reports[0]["translated_items"] == 1
    assert reports[0]["reviewed_items"] == 0
    assert reports[0]["waiting_derived_units"] == 1
    assert reports[0]["needs_attention_units"] == 0
    assert "初译=1/1" in capsys.readouterr().out

    engine.records[unit.unit_id] = engine.records[unit.unit_id].model_copy(
        update={
            "items": {
                item_id: engine.records[unit.unit_id]
                .items[item_id]
                .model_copy(update={"status": ItemStatus.NEEDS_ATTENTION})
            }
        }
    )
    engine._emit_progress("translation", "stopped")
    assert reports[-1]["needs_attention_units"] == 1
    assert "局部问题=1" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_protocol_failure_retries_without_resetting_budget(tmp_path) -> None:
    store, unit, _ = ready_store(tmp_path)
    transport = ScriptedTransport(bad_translation_once=True)
    result = await TranslationEngine(store, transport=transport).run()
    record = store.read_unit(unit.unit_id)

    assert result.status == "translated"
    assert [stage for stage, _ in transport.calls] == ["translate", "translate", "review"]
    assert record.counters["http_attempts"] == 3
    assert result.http_attempts >= 3


@pytest.mark.asyncio
async def test_one_review_replacement_requires_a_complete_second_review(tmp_path) -> None:
    store, unit, _ = ready_store(tmp_path)
    transport = ScriptedTransport(replace_review_times=1)
    result = await TranslationEngine(store, transport=transport).run()
    record = store.read_unit(unit.unit_id)

    assert result.status == "translated"
    assert record.revision == record.accepted_revision == 1
    assert record.candidate == "进程会使用内存。"
    assert [stage for stage, _ in transport.calls] == ["translate", "review", "review"]
    assert record.counters["replacement_cycle"] == 0


@pytest.mark.asyncio
async def test_second_replacement_in_the_same_review_cycle_needs_attention(tmp_path) -> None:
    store, unit, _ = ready_store(tmp_path)
    transport = ScriptedTransport(replace_review_times=2)
    result = await TranslationEngine(store, transport=transport).run()
    record = store.read_unit(unit.unit_id)
    item = next(iter(record.items.values()))

    assert result.status == "needs_attention"
    assert record.revision == 2
    assert record.accepted_revision is None
    assert record.counters["replacement_cycle"] == 0
    assert record.counters[f"automatic_recovery_review:{item.item_id}"] == 1
    assert item.status == ItemStatus.NEEDS_ATTENTION
    assert item.failure == {
        "stage": "review",
        "code": "review_replacement_required",
        "message": "automatic review recovery requires a replacement target",
    }
    assert [stage for stage, _ in transport.calls] == ["translate", "review", "review", "review"]


@pytest.mark.asyncio
async def test_term_feedback_requires_source_supported_frozen_evidence(tmp_path) -> None:
    store, unit, _ = ready_store(tmp_path)
    document = store.read_document(unit.document_id)
    view = document.source_views[unit.source_view_ids[0]]
    invalid = {
        "source": "memory",
        "target": "内存",
        "category": "term",
        "evidence": [{"view_id": view.view_id, "source_quote": "RAM"}],
    }
    transport = ScriptedTransport(
        review_decision="needs_attention",
        review_issues=[{"code": "wrong_sense", "severity": "major", "message": "Wrong sense"}],
        term_suggestions=[invalid],
    )

    result = await TranslationEngine(store, transport=transport).run()
    record = store.read_unit(unit.unit_id)

    assert result.status == "needs_attention"
    assert next(iter(record.items.values())).status == ItemStatus.NEEDS_ATTENTION
    assert len(record.term_feedback) == 1
    assert record.term_feedback[0]["kind"] == "rejected_term_suggestion"


@pytest.mark.asyncio
async def test_term_feedback_persists_frozen_view_identity_and_source_refs(tmp_path) -> None:
    store, unit, _ = ready_store(tmp_path)
    document = store.read_document(unit.document_id)
    view = document.source_views[unit.source_view_ids[0]]
    suggestion = {
        "source": "RAM",
        "target": "内存",
        "category": "term",
        "evidence": [{"view_id": view.view_id, "source_quote": "RAM"}],
    }

    result = await TranslationEngine(store, transport=ScriptedTransport(term_suggestions=[suggestion])).run()
    record = store.read_unit(unit.unit_id)
    saved = record.term_feedback[0].get("suggestion")
    assert isinstance(saved, dict)
    citations = saved.get("evidence")
    assert isinstance(citations, list) and isinstance(citations[0], dict)
    evidence = citations[0]

    assert result.status == "translated"
    assert evidence["view_hash"] == view.view_hash
    assert evidence["source_refs"] == [{"slot_id": "s1", "start": 17, "end": 20}]


def test_derived_navigation_tracks_the_current_accepted_title_without_model_items() -> None:
    source = UnitRecord(
        unit_id="title",
        document_id="chapter",
        source_hash="source",
        candidate="Translated title",
        accepted_revision=0,
        accepted_target_hash=canonical_hash("Translated title"),
    )
    derived = UnitRecord(
        unit_id="nav",
        document_id="navigation",
        source_hash="source",
        derived={"state": "blocked_dependency", "source_unit_id": "title"},
    )
    engine = object.__new__(TranslationEngine)
    engine.book = cast(Any, type("Book", (), {"unit_ids": ("title", "nav")})())
    engine.records = {"title": source, "nav": derived}
    engine.derived_bindings = {"nav": "title"}
    engine.units = {
        "nav": Unit(
            unit_id="nav",
            document_id="navigation",
            kind="navigation",
            source_projection="Source label",
            node_key="n1",
            slot_ids=("s1",),
        )
    }

    def save(record: UnitRecord, **updates) -> UnitRecord:
        saved = record.model_copy(update={"record_version": record.record_version + 1, **updates})
        engine.records[record.unit_id] = saved
        return saved

    engine._save = save  # type: ignore[method-assign]
    engine._advance_derived_navigation()
    first = engine.records["nav"]

    assert first.items == {}
    assert first.cut_plan is None
    assert first.candidate is None
    assert first.accepted_revision is None
    assert first.derived == {
        "state": "valid",
        "source_unit_id": "title",
        "source_revision": 0,
        "source_target_hash": canonical_hash("Translated title"),
        "target": "Translated title",
        "target_hash": canonical_hash("Translated title"),
    }

    engine.records["title"] = source.model_copy(
        update={
            "revision": 1,
            "candidate": "Revised title",
            "accepted_revision": 1,
            "accepted_target_hash": canonical_hash("Revised title"),
        }
    )
    engine._advance_derived_navigation()
    revised = engine.records["nav"]

    assert revised.revision == 1
    assert revised.accepted_revision is None
    assert revised.derived is not None and revised.derived["target"] == "Revised title"

    engine.records["title"] = engine.records["title"].model_copy(
        update={"revision": 2, "accepted_revision": None, "accepted_target_hash": None}
    )
    engine._advance_derived_navigation()
    blocked = engine.records["nav"]
    assert blocked.accepted_revision is None
    assert blocked.derived == {"state": "blocked_dependency", "source_unit_id": "title"}


@pytest.mark.asyncio
async def test_real_derived_navigation_uses_no_model_item_and_tracks_accepted_title(tmp_path) -> None:
    store, binding = await ready_derived_store(tmp_path)
    derived_id = str(binding["unit_id"])
    initial = store.read_unit(derived_id)
    assert initial.cut_plan is None and initial.items == {}
    assert initial.derived == {
        "state": "blocked_dependency",
        "source_unit_id": binding["source_unit_id"],
    }
    transport = PartialBatchTransport(title_target="第一章")
    transport.omitted = "disabled"

    result = await TranslationEngine(store, transport=transport).run()
    derived = store.read_unit(derived_id)

    assert result.status == "translated"
    assert derived.derived is not None and derived.derived["state"] == "valid"
    assert derived.candidate is None and derived.accepted_revision is None
    target = derived.derived["target"]
    assert isinstance(target, str) and plain_text(target) == "第一章"
    requested_ids = {item_id for _, item_ids in transport.calls for item_id in item_ids}
    assert not requested_ids & set(initial.items)

    output = tmp_path / "derived-output.epub"
    published = publish_book(store, output, StubChecker())
    assert published["path"] == str(output)
    with zipfile.ZipFile(output) as archive:
        assert "第一章" in archive.read("OEBPS/nav.xhtml").decode()


@pytest.mark.asyncio
async def test_failed_title_blocks_only_its_derived_navigation(tmp_path) -> None:
    store, binding = await ready_derived_store(tmp_path, "derived-blocked")
    source_id = str(binding["source_unit_id"])
    source = store.read_unit(source_id)
    items = {
        item_id: item.model_copy(
            update={
                "status": ItemStatus.NEEDS_ATTENTION,
                "failure": {"stage": "translation", "code": "bad", "message": "bad title"},
            }
        )
        for item_id, item in source.items.items()
    }
    store.save_unit(
        source.model_copy(update={"record_version": source.record_version + 1, "items": items}),
        expected_record_version=source.record_version,
    )
    transport = PartialBatchTransport()
    transport.omitted = "disabled"

    result = await TranslationEngine(store, transport=transport).run()
    derived = store.read_unit(str(binding["unit_id"]))
    unrelated = [
        store.read_unit(unit_id)
        for unit_id in store.read_bookplan().unit_ids
        if unit_id not in {source_id, str(binding["unit_id"])}
    ]

    assert result.status == "needs_attention"
    assert derived.derived == {"state": "blocked_dependency", "source_unit_id": source_id}
    assert any(record.accepted_revision == record.revision for record in unrelated)
