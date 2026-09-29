from __future__ import annotations

import json
import zipfile
from typing import Any, cast

import pytest

import engine.orchestrator as orchestrator_module
from engine.agents.runtime import MAX_MODEL_INPUT_TOKENS, ProviderError
from engine.epub.preparation import PreparationConfig
from engine.epub.publication import _accepted_target, publish_book
from engine.item.inline import plain_text
from engine.orchestrator import (
    TranslationEngine,
    import_repair_file,
    retry_failed_units,
    validate_repair_file,
    validate_retry_failed_units,
)
from engine.schemas.contracts import ItemStatus, Unit, UnitRecord, canonical_hash
from engine.services.atomic_store import StoreError
from engine.services.preparation_pipeline import prepare_translation
from engine.services.store import RunStore
from engine.services.term_planning import TERM_PLANNER_VERSION
from main import _progress_printer
from tests.engine.epub.book_factory import make_epub
from tests.engine.epub.test_preparation import StubChecker
from tests.engine.services.test_store_contracts import _bookplan, _frozen_store, _unit_record


class ScriptedTransport:
    def __init__(
        self,
        *,
        pause_review: bool = False,
        bad_translation_once: bool = False,
        replace_review_times: int = 0,
        review_decision: str = "no_change",
        review_issues: list[dict] | None = None,
        term_suggestions: list[dict] | None = None,
    ):
        self.pause_review = pause_review
        self.bad_translation_once = bad_translation_once
        self.replace_review_times = replace_review_times
        self.review_decision = review_decision
        self.review_issues = review_issues or []
        self.term_suggestions = term_suggestions or []
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, stage: str, payload: dict):
        self.calls.append((stage, payload))
        item = payload["items"][0]
        if stage == "translate":
            if self.bad_translation_once:
                self.bad_translation_once = False
                return {
                    "raw": json.dumps({"protocol": "epubox-text-1", "request_id": payload["request_id"], "items": []})
                }
            return {
                "raw": json.dumps(
                    {
                        "protocol": "epubox-text-1",
                        "request_id": payload["request_id"],
                        "items": [{"item_id": item["item_id"], "target": "该进程使用内存。"}],
                    },
                    ensure_ascii=False,
                )
            }
        if stage == "coherence":
            return {
                "raw": json.dumps(
                    {
                        "protocol": "epubox-coherence-1",
                        "request_id": payload["request_id"],
                        "items": [{"item_id": item["item_id"], "unit_ids": [], "issues": []}],
                    }
                )
            }
        if self.pause_review:
            raise ProviderError("account paused", status_code=401)
        decision = self.review_decision
        replacement = {}
        if self.replace_review_times:
            self.replace_review_times -= 1
            decision = "replace"
            replacement = {"target": "进程会使用内存。"}
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-review-2",
                    "request_id": payload["request_id"],
                    "items": [
                        {
                            "item_id": item["item_id"],
                            "base_revision": item["base_revision"],
                            "decision": decision,
                            "checks": {
                                "accuracy": "pass",
                                "fluency": "pass",
                                "terminology": "not_applicable",
                                "bindings": "not_applicable",
                                "script": "pass",
                            },
                            "issues": self.review_issues,
                            "term_suggestions": self.term_suggestions,
                            **replacement,
                        }
                    ],
                },
                ensure_ascii=False,
            )
        }


class PartialBatchTransport:
    def __init__(
        self,
        *,
        coherence_major_once: bool = False,
        coherence_empty_once: bool = False,
        coherence_always_empty: bool = False,
        coherence_omit_once: bool = False,
        coherence_pause_once: bool = False,
        coherence_request_failures: int = 0,
        coherence_malformed_once: bool = False,
        coherence_length_once: bool = False,
        title_target: str | None = None,
    ):
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.omitted: str | None = None
        self.coherence_major_once = coherence_major_once
        self.coherence_empty_once = coherence_empty_once
        self.coherence_always_empty = coherence_always_empty
        self.coherence_omit_once = coherence_omit_once
        self.coherence_pause_once = coherence_pause_once
        self.coherence_request_failures = coherence_request_failures
        self.coherence_malformed_once = coherence_malformed_once
        self.coherence_length_once = coherence_length_once
        self.coherence_omitted: str | None = None
        self.title_target = title_target

    async def __call__(self, stage: str, payload: dict):
        ids = tuple(item["item_id"] for item in payload["items"])
        self.calls.append((stage, ids))
        if stage == "translate":
            included = list(payload["items"])
            if self.omitted is None and len(included) > 1:
                self.omitted = included[1]["item_id"]
                included.pop(1)
            return {
                "raw": json.dumps(
                    {
                        "protocol": "epubox-text-1",
                        "request_id": payload["request_id"],
                        "items": [
                            {
                                "item_id": item["item_id"],
                                "target": self.title_target
                                if self.title_target is not None and plain_text(item["source"]).strip() == "Chapter 1"
                                else item["source"],
                            }
                            for item in included
                        ],
                    },
                    ensure_ascii=False,
                )
            }
        if stage == "coherence":
            if self.coherence_malformed_once:
                self.coherence_malformed_once = False
                return {"raw": 1}
            if self.coherence_request_failures:
                self.coherence_request_failures -= 1
                raise ProviderError("invalid coherence request", status_code=400)
            if self.coherence_pause_once:
                self.coherence_pause_once = False
                raise ProviderError("account paused", status_code=401)
            included = list(payload["items"])
            if self.coherence_always_empty:
                included = []
            elif self.coherence_empty_once:
                self.coherence_empty_once = False
                included = []
            elif self.coherence_omit_once and len(included) > 1:
                self.coherence_omit_once = False
                self.coherence_omitted = included.pop(1)["item_id"]
            blocking = self.coherence_major_once
            self.coherence_major_once = False
            response = {
                "raw": json.dumps(
                    {
                        "protocol": "epubox-coherence-1",
                        "request_id": payload["request_id"],
                        "items": [
                            {
                                "item_id": item["item_id"],
                                "unit_ids": [item["unit_ids"][0]] if blocking else [],
                                "issues": [
                                    {
                                        "code": "continuity",
                                        "severity": "major",
                                        "message": "repair this transition",
                                    }
                                ]
                                if blocking
                                else [],
                            }
                            for item in included
                        ],
                    }
                )
            }
            if self.coherence_length_once:
                self.coherence_length_once = False
                response["finish_reason"] = "length"
            return response
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-review-2",
                    "request_id": payload["request_id"],
                    "items": [
                        {
                            "item_id": item["item_id"],
                            "base_revision": item["base_revision"],
                            "decision": "replace" if item.get("required_revision") else "no_change",
                            "checks": {
                                "accuracy": "pass",
                                "fluency": "pass",
                                "terminology": "pass" if item["applicability"]["terminology"] else "not_applicable",
                                "bindings": "pass" if item["applicability"]["bindings"] else "not_applicable",
                                "script": "pass",
                            },
                            "issues": [],
                            **({"target": item["target"]} if item.get("required_revision") else {}),
                        }
                        for item in payload["items"]
                    ],
                },
                ensure_ascii=False,
            )
        }


class TruncatedOnceTransport:
    def __init__(self, stage: str, *, input_tokens: int | None = None):
        self.stage = stage
        self.input_tokens = input_tokens
        self.truncated = False
        self.base = PartialBatchTransport()
        self.base.omitted = "disabled"

    @property
    def calls(self):
        return self.base.calls

    async def __call__(self, stage: str, payload: dict):
        response = await self.base(stage, payload)
        if self.input_tokens is not None:
            response = dict(response) | {"usage": {"input_tokens": self.input_tokens, "output_tokens": 1}}
            self.input_tokens = None
        if stage == self.stage and not self.truncated:
            self.truncated = True
            response = dict(response) | {"raw": "{", "finish_reason": "length"}
        return response


def ready_store(tmp_path):
    store, unit = _frozen_store(tmp_path)
    record = store.save_unit(_unit_record(store, unit))
    store.write_bookplan(_bookplan(store, record))
    return store, unit, record


async def ready_batch_store(
    tmp_path,
    run_id: str = "batch-run",
    documents: dict[str, str] | None = None,
    tpm: int | None = None,
) -> RunStore:
    source = make_epub(
        tmp_path / f"{run_id}.epub",
        documents
        or {
            "chapter.xhtml": (
                "<div><p>First item.</p></div><div><p>Second item.</p></div><div><p>Third item.</p></div>"
            )
        },
    )
    prepared = await prepare_translation(
        source,
        tmp_path / f"work-{run_id}",
        PreparationConfig(
            run_id=run_id,
            auto_extract=False,
            extraction_config={
                "auto_extract": False,
                "strategy": TERM_PLANNER_VERSION,
                "prompt_version": "epubox-v25-3",
                "model": "fake",
                "target_language": "zh-Hans",
            },
            translation_config={
                "target_language": "zh-Hans",
                "model": "fake",
                "context_tokens": 32_768,
                "max_output_tokens": 8192,
                "max_batch_items": 64,
                **({"tpm": tpm} if tpm is not None else {}),
            },
        ),
        StubChecker(),
    )
    return RunStore(prepared.work_dir)


async def ready_derived_store(tmp_path, run_id: str = "derived-run") -> tuple[RunStore, dict]:
    source = make_epub(
        tmp_path / f"{run_id}.epub",
        {"chapter.xhtml": "<h1>Chapter 1</h1><p>Independent body.</p>"},
    )
    prepared = await prepare_translation(
        source,
        tmp_path / f"work-{run_id}",
        PreparationConfig(
            run_id=run_id,
            auto_extract=False,
            extraction_config={
                "auto_extract": False,
                "strategy": TERM_PLANNER_VERSION,
                "prompt_version": "epubox-v25-3",
                "model": "fake",
                "target_language": "zh-Hans",
            },
            translation_config={
                "target_language": "zh-Hans",
                "model": "fake",
                "context_tokens": 32_768,
                "max_output_tokens": 8192,
                "max_batch_items": 64,
            },
        ),
        StubChecker(),
    )
    store = RunStore(prepared.work_dir)
    preparation = store.read_preparation()
    binding = next(
        binding
        for document_id in preparation.document_hashes
        for binding in store.read_document(document_id).derived_bindings
        if binding.get("kind") == "derived_navigation"
    )
    return store, binding


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
    assert record.revision == 1
    assert record.accepted_revision is None
    assert record.counters["replacement_cycle"] == 0
    assert item.status == ItemStatus.NEEDS_ATTENTION
    assert item.failure == {
        "stage": "review",
        "code": "request_failed",
        "message": "replacement review limit exhausted",
    }
    assert [stage for stage, _ in transport.calls] == ["translate", "review", "review"]


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


@pytest.mark.asyncio
async def test_batch_saves_valid_items_and_retries_only_the_missing_item(tmp_path) -> None:
    store = await ready_batch_store(tmp_path)
    transport = PartialBatchTransport()
    result = await TranslationEngine(store, transport=transport).run()

    assert result.status == "translated"
    translate_calls = [ids for stage, ids in transport.calls if stage == "translate"]
    assert len(translate_calls[0]) > 1 and transport.omitted is not None
    assert sum(transport.omitted in ids for ids in translate_calls) == 2
    assert all(
        sum(item_id in ids for ids in translate_calls) == (2 if item_id == transport.omitted else 1)
        for item_id in translate_calls[0]
    )
    records = [store.read_unit(unit_id) for unit_id in store.read_bookplan().unit_ids]
    assert all(record.accepted_revision is not None for record in records)
    assert all(
        _accepted_target(store, store.read_bookplan(), record, planned=True) == record.candidate for record in records
    )
    omitted_unit = next(record for record in records if transport.omitted in record.items)
    assert omitted_unit.counters["http_attempts"] == 3
    assert all(record.counters["http_attempts"] == 2 for record in records if record is not omitted_unit)
    assert result.http_attempts == len(transport.calls) == 5
    assert sum(stage == "coherence" for stage, _ in transport.calls) == 1

    resume_transport = PartialBatchTransport()
    resumed = await TranslationEngine(store, transport=resume_transport).run()
    assert resumed.status == "translated"
    assert resume_transport.calls == []
    assert resumed.http_attempts == result.http_attempts


@pytest.mark.asyncio
async def test_coherence_empty_batch_is_repaired_once_without_manual_resume(tmp_path) -> None:
    store = await ready_batch_store(tmp_path, "coherence-empty-repair")
    transport = PartialBatchTransport(coherence_empty_once=True)
    transport.omitted = "disabled"

    result = await TranslationEngine(store, transport=transport).run()
    coherence_calls = [ids for stage, ids in transport.calls if stage == "coherence"]

    assert result.status == "translated"
    assert len(coherence_calls) == 2
    assert len(coherence_calls[0]) >= 2
    assert coherence_calls[1] == coherence_calls[0]
    assert result.http_attempts == len(transport.calls)


@pytest.mark.asyncio
async def test_coherence_partial_batch_retries_only_the_missing_window(tmp_path) -> None:
    store = await ready_batch_store(tmp_path, "coherence-partial-repair")
    transport = PartialBatchTransport(coherence_omit_once=True)
    transport.omitted = "disabled"

    result = await TranslationEngine(store, transport=transport).run()
    coherence_calls = [ids for stage, ids in transport.calls if stage == "coherence"]

    assert result.status == "translated"
    assert len(coherence_calls) == 2 and transport.coherence_omitted is not None
    assert coherence_calls[1] == (transport.coherence_omitted,)
    assert all(
        item_id not in coherence_calls[1] for item_id in coherence_calls[0] if item_id != transport.coherence_omitted
    )


@pytest.mark.asyncio
async def test_coherence_packs_more_than_eight_small_windows_in_one_http(tmp_path) -> None:
    chapter = "".join(f"<div><p>Item {index}.</p></div>" for index in range(11))
    store = await ready_batch_store(
        tmp_path,
        "coherence-large-batch",
        {"chapter.xhtml": chapter},
    )
    transport = PartialBatchTransport()
    transport.omitted = "disabled"

    result = await TranslationEngine(store, transport=transport).run()
    coherence_calls = [ids for stage, ids in transport.calls if stage == "coherence"]

    assert result.status == "translated"
    assert len(coherence_calls) == 1
    assert len(coherence_calls[0]) > 8


@pytest.mark.asyncio
async def test_coherence_budget_splits_before_manifest_and_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chapter = "".join(f"<div><p>Item {index}.</p></div>" for index in range(8))
    store = await ready_batch_store(tmp_path, "coherence-budget-split", {"chapter.xhtml": chapter})
    transport = PartialBatchTransport()
    transport.omitted = "disabled"
    original = orchestrator_module.model_input_budget
    engine = TranslationEngine(store, transport=transport)
    coherence_limit = engine._coherence_input_limit()

    def budget(stage, payload):
        result = original(stage, payload)
        if stage == "coherence" and len(payload["items"]) > 3:
            return result | {"estimated_input_tokens": coherence_limit + 1}
        return result

    monkeypatch.setattr(orchestrator_module, "model_input_budget", budget)
    result = await engine.run()
    coherence_calls = [ids for stage, ids in transport.calls if stage == "coherence"]
    manifests = [
        store.read_request(path.stem)
        for path in (store.root / "requests").glob("*.json")
        if store.read_request(path.stem).stage == "coherence"
    ]

    assert result.status == "translated"
    assert coherence_limit == MAX_MODEL_INPUT_TOKENS
    assert len(coherence_calls) > 1
    assert all(len(ids) <= 3 for ids in coherence_calls)
    assert all(len(manifest.item_ids) <= 3 for manifest in manifests)


@pytest.mark.asyncio
async def test_coherence_budget_reserves_output_only_when_tpm_is_configured(tmp_path) -> None:
    store = await ready_batch_store(tmp_path, "coherence-tpm", tpm=40_000)
    engine = TranslationEngine(store, transport=PartialBatchTransport())

    assert engine._coherence_input_limit() == 40_000 - engine.output_tokens


@pytest.mark.asyncio
async def test_coherence_minimum_output_splits_before_manifest_and_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chapter = "".join(f"<div><p>Item {index}.</p></div>" for index in range(8))
    store = await ready_batch_store(tmp_path, "coherence-output-split", {"chapter.xhtml": chapter})
    transport = PartialBatchTransport()
    transport.omitted = "disabled"
    engine = TranslationEngine(store, transport=transport)
    original = engine._coherence_min_output_tokens

    monkeypatch.setattr(
        engine,
        "_coherence_min_output_tokens",
        lambda items: engine.output_tokens + 1 if len(items) > 3 else original(items),
    )
    result = await engine.run()
    coherence_calls = [ids for stage, ids in transport.calls if stage == "coherence"]
    manifests = [
        store.read_request(path.stem)
        for path in (store.root / "requests").glob("*.json")
        if store.read_request(path.stem).stage == "coherence"
    ]

    assert result.status == "translated"
    assert len(coherence_calls) > 1
    assert all(len(ids) <= 3 for ids in coherence_calls)
    assert all(len(manifest.item_ids) <= 3 for manifest in manifests)


@pytest.mark.asyncio
async def test_truncated_coherence_batch_is_bisected(tmp_path) -> None:
    chapter = "".join(f"<div><p>Item {index}.</p></div>" for index in range(7))
    store = await ready_batch_store(tmp_path, "coherence-length", {"chapter.xhtml": chapter})
    transport = PartialBatchTransport(coherence_length_once=True)
    transport.omitted = "disabled"

    result = await TranslationEngine(store, transport=transport).run()
    coherence_calls = [ids for stage, ids in transport.calls if stage == "coherence"]

    assert result.status == "translated"
    assert len(coherence_calls) == 3
    assert len(coherence_calls[0]) > 1
    assert set(coherence_calls[1]).isdisjoint(coherence_calls[2])
    assert set(coherence_calls[1]) | set(coherence_calls[2]) == set(coherence_calls[0])


@pytest.mark.asyncio
async def test_failed_coherence_document_does_not_stop_later_document(tmp_path) -> None:
    documents = {
        "one.xhtml": "<div><p>One.</p></div><div><p>Two.</p></div><div><p>Three.</p></div>",
        "two.xhtml": "<div><p>Four.</p></div><div><p>Five.</p></div><div><p>Six.</p></div>",
    }
    store = await ready_batch_store(tmp_path, "coherence-later-document", documents)
    transport = PartialBatchTransport(coherence_request_failures=2)
    transport.omitted = "disabled"
    engine = TranslationEngine(store, transport=transport)

    result = await engine.run()
    coherence_calls = [ids for stage, ids in transport.calls if stage == "coherence"]
    checks = list(engine.checks.values())

    assert result.status == "needs_attention"
    assert len(coherence_calls) == 3
    assert len({item_id for ids in coherence_calls for item_id in ids}) > len(coherence_calls[0])
    assert {check["status"] for check in checks} == {"needs_attention", "valid"}


@pytest.mark.asyncio
async def test_coherence_marks_major_only_after_the_repair_response_is_still_bad(tmp_path) -> None:
    store = await ready_batch_store(tmp_path, "coherence-final-bad")
    transport = PartialBatchTransport(coherence_always_empty=True)
    transport.omitted = "disabled"
    engine = TranslationEngine(store, transport=transport)

    result = await engine.run()
    coherence_calls = [ids for stage, ids in transport.calls if stage == "coherence"]
    checks = [check for check in engine.checks.values() if check["windows"]]

    assert result.status == "needs_attention"
    assert len(coherence_calls) == 2
    assert checks and all(check["status"] == "needs_attention" for check in checks)
    assert all(len(check["checks"]) == len(check["windows"]) for check in checks)
    assert len(coherence_calls) <= sum(int(check["http_limit"]) for check in checks)


@pytest.mark.asyncio
async def test_coherence_transport_pause_keeps_windows_pending_for_resume(tmp_path) -> None:
    store = await ready_batch_store(tmp_path, "coherence-pause")
    transport = PartialBatchTransport(coherence_pause_once=True)
    transport.omitted = "disabled"
    engine = TranslationEngine(store, transport=transport)

    paused = await engine.run()
    checks = [check for check in engine.checks.values() if check["windows"]]

    assert paused.status == "paused"
    assert checks and all(check["status"] == "pending" and not check["checks"] for check in checks)
    resumed = await TranslationEngine(store, transport=PartialBatchTransport()).run()
    assert resumed.status == "translated"


@pytest.mark.asyncio
async def test_coherence_request_failure_is_bounded_to_its_windows(tmp_path) -> None:
    store = await ready_batch_store(tmp_path, "coherence-request-failure")
    transport = PartialBatchTransport(coherence_request_failures=2)
    transport.omitted = "disabled"
    engine = TranslationEngine(store, transport=transport)
    result = await engine.run()
    checks = [check for check in engine.checks.values() if check["windows"]]
    window_count = sum(len(check["windows"]) for check in checks)

    assert result.status == "needs_attention"
    assert window_count > 1
    assert sum(stage == "coherence" for stage, _ in transport.calls) == 2
    assert checks and all(check["status"] == "needs_attention" for check in checks)
    assert all(len(check["checks"]) == len(check["windows"]) for check in checks)
    assert all(
        issue["code"] == "coherence_request_failed"
        for check in checks
        for result in check["checks"].values()
        for issue in result["issues"]
    )


@pytest.mark.asyncio
async def test_malformed_coherence_envelope_is_retried_locally(tmp_path) -> None:
    store = await ready_batch_store(tmp_path, "coherence-malformed")
    transport = PartialBatchTransport(coherence_malformed_once=True)
    transport.omitted = "disabled"

    result = await TranslationEngine(store, transport=transport).run()

    assert result.status == "translated"
    assert sum(stage == "coherence" for stage, _ in transport.calls) >= 2


@pytest.mark.asyncio
async def test_one_oversized_item_does_not_upgrade_or_fail_its_batch_siblings(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await ready_batch_store(tmp_path, "oversized-run")
    engine = TranslationEngine(store, transport=PartialBatchTransport())
    jobs = engine._ready_jobs()
    bad = jobs[0]
    normal_ids = {job.item_id for job in jobs[1:]}
    original = engine._payload_item

    def payload(job):
        value = original(job)
        if job.item_id == bad.item_id:
            value = dict(value) | {"source": "oversized " * 100_000}
        return value

    monkeypatch.setattr(engine, "_payload_item", payload)
    batches = engine._pack_jobs(jobs)
    planned_ids = {job.item_id for batch in batches for job in batch}

    assert bad.item_id not in planned_ids
    assert normal_ids.issubset(planned_ids)
    assert store.read_unit(bad.unit_id).items[bad.item_id].status == ItemStatus.NEEDS_ATTENTION
    assert all(store.read_unit(job.unit_id).items[job.item_id].status == ItemStatus.PENDING for job in jobs[1:])


@pytest.mark.asyncio
async def test_input_budget_splits_before_request_persistence_or_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await ready_batch_store(tmp_path, "input-split")
    transport = PartialBatchTransport()
    transport.omitted = "disabled"
    engine = TranslationEngine(store, transport=transport)
    jobs = tuple(engine._ready_jobs()[:2])
    original = orchestrator_module.model_input_budget

    def budget(stage, payload):
        result = original(stage, payload)
        return result | {
            "estimated_input_tokens": MAX_MODEL_INPUT_TOKENS + 1
            if len(payload["items"]) > 1
            else result["estimated_input_tokens"]
        }

    monkeypatch.setattr(orchestrator_module, "model_input_budget", budget)
    await engine._run_batch(jobs)

    assert [ids for stage, ids in transport.calls if stage == "translate"] == [
        (jobs[0].item_id,),
        (jobs[1].item_id,),
    ]
    manifests = [store.read_request(path.stem) for path in (store.root / "requests").glob("*.json")]
    assert len(manifests) == 2
    assert all(len(manifest.item_ids) == 1 for manifest in manifests)


@pytest.mark.asyncio
async def test_single_oversized_input_needs_attention_without_request_or_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await ready_batch_store(tmp_path, "input-single")
    transport = PartialBatchTransport()
    engine = TranslationEngine(store, transport=transport)
    job = engine._ready_jobs()[0]
    monkeypatch.setattr(
        orchestrator_module,
        "model_input_budget",
        lambda stage, payload: {
            "algorithm_version": 1,
            "cl100k_tokens": 1,
            "rendered_utf8_bytes": 1,
            "wrapper_headroom_bytes": 1,
            "estimated_input_tokens": MAX_MODEL_INPUT_TOKENS + 1,
        },
    )

    await engine._run_batch((job,))

    assert transport.calls == []
    assert not list((store.root / "requests").glob("*.json"))
    assert store.read_unit(job.unit_id).items[job.item_id].status == ItemStatus.NEEDS_ATTENTION


@pytest.mark.asyncio
async def test_journaled_translation_is_replayed_after_crash_without_second_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, unit, _ = ready_store(tmp_path)
    engine = TranslationEngine(store, transport=ScriptedTransport())
    monkeypatch.setattr(
        engine,
        "_apply_response",
        lambda manifest, jobs, raw: (_ for _ in ()).throw(StoreError("crash after response journal")),
    )

    failed = await engine.run()
    assert failed.status == "failed"
    assert next(iter(store.read_unit(unit.unit_id).items.values())).status == ItemStatus.IN_FLIGHT

    resumed_transport = ScriptedTransport()
    resumed = await TranslationEngine(store, transport=resumed_transport).run()

    assert resumed.status == "translated"
    assert all(stage != "translate" for stage, _ in resumed_transport.calls)


@pytest.mark.asyncio
async def test_journaled_review_is_replayed_after_crash_without_second_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, _ = ready_store(tmp_path)
    engine = TranslationEngine(store, transport=ScriptedTransport())
    original = engine._apply_response

    def crash_on_review(manifest, jobs, raw):
        if manifest.stage == "review":
            raise StoreError("crash after review journal")
        return original(manifest, jobs, raw)

    monkeypatch.setattr(engine, "_apply_response", crash_on_review)
    failed = await engine.run()
    assert failed.status == "failed"

    resumed_transport = ScriptedTransport()
    resumed = await TranslationEngine(store, transport=resumed_transport).run()

    assert resumed.status == "translated"
    assert all(stage not in {"translate", "review"} for stage, _ in resumed_transport.calls)


@pytest.mark.asyncio
async def test_journaled_coherence_is_replayed_after_crash_without_second_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await ready_batch_store(tmp_path, "coherence-journal")
    first_transport = PartialBatchTransport()
    first_transport.omitted = "disabled"
    engine = TranslationEngine(store, transport=first_transport)
    original = orchestrator_module.save_window_result
    failed_once = False

    def crash_after_journal(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise StoreError("crash after coherence journal")
        return original(*args, **kwargs)

    monkeypatch.setattr(orchestrator_module, "save_window_result", crash_after_journal)
    failed = await engine.run()
    assert failed.status == "failed"
    monkeypatch.setattr(orchestrator_module, "save_window_result", original)

    resumed_transport = PartialBatchTransport()
    resumed_transport.omitted = "disabled"
    resumed = await TranslationEngine(store, transport=resumed_transport).run()

    assert resumed.status == "translated"
    assert all(stage != "coherence" for stage, _ in resumed_transport.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["translate", "review"])
async def test_truncated_text_batch_is_retried_as_smaller_batches(tmp_path, stage: str) -> None:
    store = await ready_batch_store(tmp_path, f"{stage}-length")
    transport = TruncatedOnceTransport(stage)

    result = await TranslationEngine(store, transport=transport).run()
    calls = [ids for called_stage, ids in transport.calls if called_stage == stage]

    assert result.status == "translated"
    assert len(calls[0]) > 1
    assert all(len(ids) < len(calls[0]) for ids in calls[1:])
    assert set().union(*(set(ids) for ids in calls[1:])) == set(calls[0])


@pytest.mark.asyncio
async def test_singleton_truncated_translation_needs_attention(tmp_path) -> None:
    store, unit, _ = ready_store(tmp_path)
    transport = TruncatedOnceTransport("translate")

    result = await TranslationEngine(store, transport=transport).run()
    item = next(iter(store.read_unit(unit.unit_id).items.values()))

    assert result.status == "needs_attention"
    assert item.status == ItemStatus.NEEDS_ATTENTION
    assert [stage for stage, _ in transport.calls] == ["translate"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["translate", "review", "coherence"])
async def test_truncated_journal_replays_persisted_split_before_new_http(
    tmp_path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    store = await ready_batch_store(tmp_path, f"{stage}-length-journal")
    transport = TruncatedOnceTransport(stage)
    original = store.save_model_response
    crashed = False

    def save_then_crash(saved_stage, request_id, attempt_id, envelope):
        nonlocal crashed
        original(saved_stage, request_id, attempt_id, envelope)
        if saved_stage == stage and not crashed:
            crashed = True
            raise StoreError("crash after truncated response journal")

    monkeypatch.setattr(store, "save_model_response", save_then_crash)
    failed = await TranslationEngine(store, transport=transport).run()
    assert failed.status == "failed"
    original_size = len(next(ids for called_stage, ids in transport.calls if called_stage == stage))
    monkeypatch.setattr(store, "save_model_response", original)

    resumed_transport = PartialBatchTransport()
    resumed_transport.omitted = "disabled"
    resumed = await TranslationEngine(store, transport=resumed_transport).run()
    calls = [ids for called_stage, ids in resumed_transport.calls if called_stage == stage]

    assert resumed.status == "translated"
    assert calls
    assert all(len(ids) < original_size for ids in calls)


@pytest.mark.asyncio
async def test_replayed_actual_input_over_limit_pauses_before_new_dispatch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await ready_batch_store(tmp_path, "journal-input-over-limit")
    transport = TruncatedOnceTransport("never", input_tokens=MAX_MODEL_INPUT_TOKENS + 1)
    original = store.save_model_response
    crashed = False

    def save_then_crash(stage, request_id, attempt_id, envelope):
        nonlocal crashed
        original(stage, request_id, attempt_id, envelope)
        if stage == "translate" and not crashed:
            crashed = True
            raise StoreError("crash before attempt usage was applied")

    monkeypatch.setattr(store, "save_model_response", save_then_crash)
    failed = await TranslationEngine(store, transport=transport).run()
    assert failed.status == "failed"
    monkeypatch.setattr(store, "save_model_response", original)

    resumed_transport = PartialBatchTransport()
    resumed = await TranslationEngine(store, transport=resumed_transport).run()

    assert resumed.status == "paused"
    assert resumed_transport.calls == []


@pytest.mark.asyncio
async def test_document_coherence_budget_exhaustion_is_local(tmp_path) -> None:
    documents = {
        "one.xhtml": "<div><p>One.</p></div><div><p>Two.</p></div><div><p>Three.</p></div>",
        "two.xhtml": "<div><p>Four.</p></div><div><p>Five.</p></div><div><p>Six.</p></div>",
    }
    store = await ready_batch_store(tmp_path, "coherence-document-budget", documents)
    transport = PartialBatchTransport()
    transport.omitted = "disabled"
    engine = TranslationEngine(store, transport=transport)
    engine._prepare_checks()
    first_id = next(key for key, check in engine.checks.items() if check["windows"])
    engine.checks[first_id] = orchestrator_module.save_document_check(
        store,
        dict(engine.checks[first_id]) | {"http_limit": 0},
    )

    result = await engine.run()
    coherence_calls = [ids for called_stage, ids in transport.calls if called_stage == "coherence"]

    assert result.status == "needs_attention"
    assert len(coherence_calls) == 1
    assert engine.checks[first_id]["status"] == "needs_attention"
    assert any(check["status"] == "valid" for key, check in engine.checks.items() if key != first_id)


@pytest.mark.asyncio
async def test_reserved_batch_attempt_is_conservatively_charged_after_second_unit_counter_write_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await ready_batch_store(tmp_path, "ledger-run")
    engine = TranslationEngine(store, transport=PartialBatchTransport())
    jobs = tuple(engine._ready_jobs()[:2])
    second_unit = jobs[1].unit_id
    original = store.save_unit
    failed = False

    def flaky_save(record, *, expected_record_version=None):
        nonlocal failed
        if not failed and record.unit_id == second_unit and record.counters.get("http_attempts", 0) > 0:
            failed = True
            raise StoreError("counter write failed")
        return original(record, expected_record_version=expected_record_version)

    monkeypatch.setattr(store, "save_unit", flaky_save)
    with pytest.raises(StoreError, match="counter write failed"):
        await engine._run_batch(jobs)
    assert engine._spent_total == 1
    assert engine._actual_http_total == 0
    monkeypatch.setattr(store, "save_unit", original)

    resumed_transport = PartialBatchTransport()
    resumed_transport.omitted = "disabled"
    resumed_engine = TranslationEngine(store, transport=resumed_transport)
    assert all(resumed_engine._spent_by_unit[job.unit_id] >= 1 for job in jobs)
    await resumed_engine.run()
    assert store.read_unit(second_unit).counters["http_attempts"] >= 2


@pytest.mark.asyncio
async def test_long_unit_accepts_segment_reviews_from_multiple_manifests(tmp_path) -> None:
    source = make_epub(
        tmp_path / "long.epub",
        {"chapter.xhtml": "<p>" + ("Long sentence. " * 100) + "</p>"},
    )
    prepared = await prepare_translation(
        source,
        tmp_path / "work-long",
        PreparationConfig(
            run_id="long-run",
            auto_extract=False,
            extraction_config={
                "auto_extract": False,
                "strategy": TERM_PLANNER_VERSION,
                "prompt_version": "epubox-v25-3",
                "model": "fake",
                "target_language": "zh-Hans",
            },
            translation_config={
                "target_language": "zh-Hans",
                "model": "fake",
                "context_tokens": 4096,
                "max_output_tokens": 256,
                "review_output_tokens": 160,
                "max_batch_items": 2,
            },
        ),
        StubChecker(),
    )
    store = RunStore(prepared.work_dir)
    transport = PartialBatchTransport()
    transport.omitted = "disabled"
    engine = TranslationEngine(store, transport=transport)
    initial = max(
        (store.read_unit(unit_id) for unit_id in store.read_bookplan().unit_ids),
        key=lambda value: len(value.items),
    )
    assert engine._upgrade_cut_plan(initial.unit_id, "budget") is True
    upgraded = store.read_unit(initial.unit_id)
    assert upgraded.logical_hash == initial.logical_hash
    assert len(upgraded.items) > len(initial.items)
    assert upgraded.counters.get("http_attempts", 0) == initial.counters.get("http_attempts", 0)
    assert engine._upgrade_cut_plan(initial.unit_id, "again") is False
    result = await engine.run()
    record = max(
        (store.read_unit(unit_id) for unit_id in store.read_bookplan().unit_ids),
        key=lambda value: len(value.items),
    )

    assert result.status == "translated"
    assert len(record.items) > 2 and record.accepted_revision == record.revision
    assert record.review is not None
    item_reviews = record.review["item_reviews"]
    assert isinstance(item_reviews, dict) and set(item_reviews) == set(record.items)
    request_ids: set[str] = set()
    for value in item_reviews.values():
        assert isinstance(value, dict)
        request_id = value.get("request_id")
        assert isinstance(request_id, str)
        request_ids.add(request_id)
    assert len(request_ids) > 1


@pytest.mark.asyncio
async def test_coherence_major_issue_gets_one_unit_revision_and_full_rereview(tmp_path) -> None:
    source = make_epub(
        tmp_path / "coherence.epub",
        {"chapter.xhtml": "<div><p>First transition.</p></div><div><p>Second transition.</p></div>"},
    )
    prepared = await prepare_translation(
        source,
        tmp_path / "work-coherence",
        PreparationConfig(
            run_id="coherence-run",
            auto_extract=False,
            extraction_config={
                "auto_extract": False,
                "strategy": TERM_PLANNER_VERSION,
                "prompt_version": "epubox-v25-3",
                "model": "fake",
                "target_language": "zh-Hans",
            },
            translation_config={
                "target_language": "zh-Hans",
                "model": "fake",
                "context_tokens": 16_384,
                "max_output_tokens": 4096,
                "max_batch_items": 16,
            },
        ),
        StubChecker(),
    )
    store = RunStore(prepared.work_dir)
    transport = PartialBatchTransport(coherence_major_once=True)
    transport.omitted = "disabled"
    result = await TranslationEngine(store, transport=transport).run()
    records = [store.read_unit(unit_id) for unit_id in store.read_bookplan().unit_ids]

    assert result.status == "translated"
    revised = [record for record in records if record.counters.get("coherence_revision_rounds") == 1]
    assert len(revised) == 1
    assert revised[0].accepted_revision == revised[0].revision >= 2
    assert not revised[0].unresolved_issues
    assert sum(stage == "coherence" for stage, _ in transport.calls) == 2


def test_explicit_retry_and_versioned_repair_preserve_counters(tmp_path) -> None:
    store, unit, initial = ready_store(tmp_path)
    item_id = next(iter(initial.items))
    failed_item = initial.items[item_id].model_copy(
        update={
            "status": ItemStatus.NEEDS_ATTENTION,
            "failure": {"stage": "translation", "code": "bad", "message": "bad output"},
        }
    )
    failed = store.save_unit(
        initial.model_copy(
            update={
                "record_version": 1,
                "items": {item_id: failed_item},
                "counters": {"http_attempts": 1, "unit_http_limit": 24},
            }
        ),
        expected_record_version=0,
    )
    retried = retry_failed_units(store, (unit.unit_id,))[0]
    assert retried.items[item_id].status == ItemStatus.RETRY_WAIT
    assert retried.counters == failed.counters

    repair_path = tmp_path / "repair.json"
    repair_path.write_text(
        json.dumps(
            {
                "unit_id": unit.unit_id,
                "base_revision": retried.revision,
                "plan_epoch": retried.plan_epoch,
                "target": "该进程使用内存。",
            },
            ensure_ascii=False,
        )
    )
    proposed = validate_repair_file(store, repair_path)
    assert store.read_unit(unit.unit_id) == retried
    assert proposed.revision == retried.revision + 1
    repaired = import_repair_file(store, repair_path)
    assert repaired.revision == retried.revision + 1
    assert repaired.items[item_id].status == ItemStatus.LOCAL_VALID
    assert repaired.counters == retried.counters
    with pytest.raises(ValueError, match="stale"):
        import_repair_file(store, repair_path)


@pytest.mark.asyncio
async def test_retry_prevalidates_every_unit_before_changing_any_record(tmp_path) -> None:
    store = await ready_batch_store(tmp_path, "retry-atomic")
    unit_ids = store.read_bookplan().unit_ids[:2]
    originals = []
    for index, unit_id in enumerate(unit_ids):
        record = store.read_unit(unit_id)
        items = {
            item_id: item.model_copy(
                update={
                    "status": ItemStatus.NEEDS_ATTENTION,
                    "failure": {"stage": "translation", "code": "bad", "message": "bad"},
                }
            )
            for item_id, item in record.items.items()
        }
        counters = dict(record.counters)
        if index == 1:
            counters.update(http_attempts=24, unit_http_limit=24)
        originals.append(
            store.save_unit(
                record.model_copy(
                    update={"record_version": record.record_version + 1, "items": items, "counters": counters}
                ),
                expected_record_version=record.record_version,
            )
        )

    with pytest.raises(ValueError, match="budget remains exhausted"):
        validate_retry_failed_units(store, unit_ids)
    with pytest.raises(ValueError, match="budget remains exhausted"):
        retry_failed_units(store, unit_ids)
    assert [store.read_unit(unit_id) for unit_id in unit_ids] == originals
    validate_retry_failed_units(store, unit_ids, add_unit_http=1)


def test_cut_plan_upgrade_refuses_an_epoch_change_when_no_stricter_cut_exists(tmp_path) -> None:
    store, unit, initial = ready_store(tmp_path)
    engine = TranslationEngine(store, transport=ScriptedTransport())
    assert engine._upgrade_cut_plan(unit.unit_id, "budget") is False
    assert store.read_unit(unit.unit_id) == initial
    assert engine._upgrade_cut_plan(unit.unit_id, "again") is False
