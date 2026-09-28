from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from html import escape
from pathlib import Path
from typing import Any

import pytest

from engine.orchestrator_v23 import Job, TranslationEngine
from engine.schemas.v23 import (
    BookPlan,
    Counters,
    CutPlan,
    DocumentPlan,
    Event,
    FailureRecord,
    ItemStatus,
    JsonValue,
    NodeRecord,
    ResourceRecord,
    RunConfig,
    Segment,
    SlotRange,
    SourceSlot,
    Unit,
    UnitRecord,
    canonical_hash,
    is_accepted,
)
from engine.services.store import StaleWrite, Store


def _segments(unit_id: str, parts: Iterable[str]) -> CutPlan:
    segments: list[Segment] = []
    start = 0
    for index, part in enumerate(parts):
        end = start + len(part)
        data = {
            "segment_id": f"{unit_id}:e0:s{index}",
            "item_id": f"{unit_id}:e0:s{index}",
            "source_start": start,
            "source_end": end,
            "source_projection": part,
            "events": [Event(kind="text", value=part).model_dump(mode="json")],
            "virtual_boundaries": [],
        }
        segments.append(Segment(**data, segment_hash=canonical_hash(data)))
        start = end
    frozen = tuple(segments)
    return CutPlan(
        plan_epoch=0,
        plan_hash=canonical_hash(
            {
                "plan_epoch": 0,
                "segments": [segment.model_dump(mode="json") for segment in frozen],
            }
        ),
        segments=frozen,
    )


def _make_store(
    root: Path,
    documents: Iterable[Iterable[tuple[str, tuple[str, ...]]]],
    *,
    concurrency: int = 2,
    run_http_limit: int = 500,
    unplanned_units: Iterable[str] = (),
) -> Store:
    source = b"v2.3 scheduler fixture"
    source_hash = hashlib.sha256(source).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    (root / "source.epub").write_bytes(source)
    store = Store(root)
    document_hashes: dict[str, str] = {}
    unit_ids: list[str] = []
    unit_documents: dict[str, str] = {}
    resources: dict[str, ResourceRecord] = {}
    unplanned = set(unplanned_units)

    for document_index, definitions in enumerate(documents, 1):
        document_id = f"d{document_index}"
        resource_path = f"OEBPS/chapter{document_index}.xhtml"
        units: list[Unit] = []
        plans: list[tuple[Unit, CutPlan]] = []
        nodes: dict[str, NodeRecord] = {}
        source_slots: dict[str, SourceSlot] = {}
        markup_parts: list[str] = []
        for unit_index, (unit_id, parts) in enumerate(definitions):
            source_projection = "".join(parts)
            node_key = f"n-{unit_id}"
            slot_id = f"slot-{unit_id}"
            unit = Unit(
                unit_id=unit_id,
                document_id=document_id,
                kind="paragraph",
                source_projection=source_projection,
                node_key=node_key,
                slot_ids=(slot_id,),
                logical_hash=canonical_hash({"unit": unit_id, "parts": parts}),
            )
            nodes[node_key] = NodeRecord(
                node_key=node_key,
                element_path=(1, unit_index),
                qname="p",
            )
            source_slots[slot_id] = SourceSlot(
                slot_id=slot_id,
                node_key=node_key,
                field="text",
                source_value=source_projection,
                ranges=(
                    SlotRange(
                        start=0,
                        end=len(source_projection),
                        owner_kind="unit",
                        owner_unit_id=unit_id,
                    ),
                ),
                owner_kind="unit",
                owner_unit_id=unit_id,
            )
            markup_parts.append(f'<p id="{escape(unit_id)}">{escape(source_projection)}</p>')
            units.append(unit)
            plans.append((unit, _segments(unit_id, parts)))
            unit_ids.append(unit_id)
            unit_documents[unit_id] = document_id
        source_markup = "<html><body>" + "".join(markup_parts) + "</body></html>"
        resource = ResourceRecord(
            path=resource_path,
            media_type="application/xhtml+xml",
            source_sha256=hashlib.sha256(source_markup.encode()).hexdigest(),
        )
        preparation_issues: tuple[dict[str, JsonValue], ...] = tuple(
            {
                "scope": "unit",
                "stage": "preparation",
                "code": "unit_planning_failed",
                "message": "injected planning failure",
                "unit_id": unit.unit_id,
            }
            for unit in units
            if unit.unit_id in unplanned
        )
        document = DocumentPlan(
            document_id=document_id,
            source_hash=source_hash,
            resource=resource,
            adapter_version="test-adapter-1",
            extractor_version="test-extractor-1",
            source_markup=source_markup,
            nodes=nodes,
            source_slots=source_slots,
            units=tuple(units),
            preparation_issues=preparation_issues,
        )
        document_hashes[document_id] = store.write_document(document)
        resources[resource_path] = resource
        for unit, plan in plans:
            if unit.unit_id not in unplanned:
                store.initialize_unit(unit, plan, source_hash=source_hash)
                continue
            store.initialize_unit(
                UnitRecord(
                    unit_id=unit.unit_id,
                    document_id=unit.document_id,
                    source_hash=source_hash,
                    logical_hash=unit.logical_hash,
                    plan_epoch=0,
                    unresolved_issues=(
                        FailureRecord(
                            scope="unit",
                            stage="preparation",
                            code="unit_planning_failed",
                            message="injected planning failure",
                            plan_epoch=0,
                            revision=0,
                            retry_action="repair",
                        ),
                    ),
                    counters=Counters(unit_http_limit=24),
                )
            )

    config = RunConfig(
        model="fake",
        provider="test",
        prompt_version="test-1",
        extractor_version="test-extractor-1",
        max_concurrency=concurrency,
        run_http_limit=run_http_limit,
    )
    store.write_bookplan(
        BookPlan(
            source_hash=source_hash,
            source_path="/fixture/book.epub",
            source_epub_version="3.0",
            run_id="run-1",
            preparation_state="ready",
            resources=resources,
            reading_order=tuple(document_hashes),
            document_hashes=document_hashes,
            unit_ids=tuple(unit_ids),
            unit_documents=unit_documents,
            required_unit_count=len(unit_ids),
            frozen_config=config.model_dump(mode="json"),
            output_policy_hash="test-output-policy",
        )
    )
    return store


def _translation(payload: dict[str, Any], item_id: str, target: str) -> dict[str, Any]:
    return {
        "raw": json.dumps(
            {
                "protocol": "epubox-text-1",
                "request_id": payload["request_id"],
                "items": [{"item_id": item_id, "target": target}],
            },
            ensure_ascii=False,
        ),
        "usage": {"input_tokens": 7, "output_tokens": 3},
    }


def _review(
    payload: dict[str, Any],
    item_id: str,
    decision: str = "no_change",
    target: str | None = None,
) -> dict[str, Any]:
    item = payload["items"][0]
    issues = (
        [{"code": "meaning", "severity": "major", "message": "meaning is wrong"}]
        if decision == "needs_attention"
        else []
    )
    response_item = {
        "item_id": item_id,
        "base_revision": item["base_revision"],
        "decision": decision,
        "checks": {
            "accuracy": "fail" if decision == "needs_attention" else "pass",
            "fluency": "pass",
            "terminology": "not_applicable",
            "bindings": "not_applicable",
            "script": "pass",
        },
        "issues": issues,
    }
    if decision == "replace":
        response_item["target"] = target or f"修订-{item_id}"
    return {
        "raw": json.dumps(
            {
                "protocol": "epubox-review-1",
                "request_id": payload["request_id"],
                "items": [response_item],
            },
            ensure_ascii=False,
        ),
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }


class ScriptedTransport:
    def __init__(
        self,
        *,
        translation_failures: Iterable[str] = (),
        review_failures: Iterable[str] = (),
        review_scripts: dict[str, list[tuple[str, str | None]]] | None = None,
        coherence_major_once: str | None = None,
        coherence_affected: list[tuple[str, ...]] | None = None,
        malformed_coherence: bool = False,
        coherence_empty_major_once: bool = False,
        delays: dict[str, float] | None = None,
    ) -> None:
        self.translation_failures = set(translation_failures)
        self.review_failures = set(review_failures)
        self.review_scripts = {key: list(value) for key, value in (review_scripts or {}).items()}
        self.coherence_major_once = coherence_major_once
        self.coherence_affected = list(coherence_affected or ())
        self.malformed_coherence = malformed_coherence
        self.coherence_empty_major_once = coherence_empty_major_once
        self.coherence_calls = 0
        self.delays = delays or {}
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = payload["items"][0]["item_id"]
        self.calls.append((kind, item_id))
        await asyncio.sleep(self.delays.get(item_id, 0))
        if kind == "translate":
            if item_id in self.translation_failures:
                return {"raw": "{bad", "usage": {}}
            return _translation(payload, item_id, f"译文-{item_id}")
        if kind == "review":
            script = self.review_scripts.get(item_id, [])
            if script:
                decision, target = script.pop(0)
                return _review(payload, item_id, decision, target)
            return _review(
                payload,
                item_id,
                "needs_attention" if item_id in self.review_failures else "no_change",
            )
        if kind == "coherence":
            self.coherence_calls += 1
            if self.malformed_coherence:
                return {"raw": "{truncated", "usage": {"input_tokens": 4, "output_tokens": 1}}
            affected = (
                self.coherence_affected[self.coherence_calls - 1]
                if self.coherence_calls <= len(self.coherence_affected)
                else ((self.coherence_major_once,) if self.coherence_major_once and self.coherence_calls == 1 else ())
            )
            blocking = bool(affected) or (self.coherence_empty_major_once and self.coherence_calls == 1)
            return {
                "raw": json.dumps(
                    {
                        "protocol": "epubox-coherence-1",
                        "request_id": payload["request_id"],
                        "items": [
                            {
                                "item_id": item_id,
                                "unit_ids": list(affected),
                                "issues": (
                                    [
                                        {
                                            "code": "continuity",
                                            "severity": "major",
                                            "message": "term changes across the boundary",
                                        }
                                    ]
                                    if blocking
                                    else []
                                ),
                            }
                        ],
                    }
                ),
                "usage": {"input_tokens": 4, "output_tokens": 1},
            }
        raise AssertionError(f"unexpected request kind: {kind}")


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 2])
async def test_local_translation_and_review_failures_do_not_stop_later_units(tmp_path: Path, concurrency: int) -> None:
    store = _make_store(
        tmp_path,
        (
            (("u1", ("First.",)), ("u2", ("Second.",)), ("u3", ("Third.",))),
            (("u4", ("Fourth.",)),),
        ),
        concurrency=concurrency,
    )
    transport = ScriptedTransport(
        translation_failures={"u2:e0:s0"},
        review_failures={"u3:e0:s0"},
        delays={"u1:e0:s0": 0.02},
    )

    report = await TranslationEngine(store, transport=transport).execute()

    assert report["outcome"] == "needs_attention"
    assert report["required_units"] == 4
    assert report["accepted_units"] == 2
    assert len(report["local_failures"]) == 2
    assert store.load_unit("u1").accepted_revision == 0
    assert store.load_unit("u2").items["u2:e0:s0"].status == ItemStatus.NEEDS_ATTENTION
    assert store.load_unit("u3").candidate == "译文-u3:e0:s0"
    assert store.load_unit("u3").accepted_revision is None
    assert store.load_unit("u4").accepted_revision == 0
    assert ("translate", "u4:e0:s0") in transport.calls
    assert ("review", "u4:e0:s0") in transport.calls


@pytest.mark.asyncio
async def test_segment_failure_is_persisted_and_restart_only_fills_the_hole(tmp_path: Path) -> None:
    store = _make_store(tmp_path, ((("long", ("One. ", "Two. ", "Three.")),),))
    first = ScriptedTransport(translation_failures={"long:e0:s1"})

    first_report = await TranslationEngine(store, transport=first).execute()

    assert first_report["outcome"] == "needs_attention"
    record = store.load_unit("long")
    assert record.items["long:e0:s0"].target_projection == "译文-long:e0:s0"
    assert record.items["long:e0:s1"].status == ItemStatus.NEEDS_ATTENTION
    assert record.items["long:e0:s2"].target_projection == "译文-long:e0:s2"
    assert record.candidate is None

    repaired = record.items["long:e0:s1"].model_copy(
        update={
            "status": ItemStatus.PENDING,
            "failure": None,
            "next_action": "translate",
            "attempts": {
                **record.items["long:e0:s1"].attempts,
                "translate_cycle_start": record.items["long:e0:s1"].attempts["translate"],
            },
        }
    )
    store.save_unit(
        record.model_copy(
            update={
                "items": {**record.items, repaired.item_id: repaired},
                "unresolved_issues": (),
            }
        )
    )
    second = ScriptedTransport()

    second_report = await TranslationEngine(Store(tmp_path), transport=second).execute()

    assert second_report["outcome"] == "needs_attention"
    assert second_report["ready_to_publish"] is True
    assert Counter(second.calls)[("translate", "long:e0:s1")] == 1
    assert not [call for call in second.calls if call[0] == "translate" and call[1] != "long:e0:s1"]
    final = Store(tmp_path).load_unit("long")
    assert final.accepted_revision == 0
    assert all(item.status == ItemStatus.REVIEWED for item in final.items.values())


@pytest.mark.asyncio
async def test_resume_scans_the_whole_inventory_and_does_not_repeat_saved_work(tmp_path: Path) -> None:
    store = _make_store(
        tmp_path,
        ((("u1", ("One.",)), ("u2", ("Two.",)), ("u3", ("Three.",))),),
    )
    first = ScriptedTransport(translation_failures={"u2:e0:s0"})
    await TranslationEngine(store, transport=first).execute()
    record = store.load_unit("u2")
    item = record.items["u2:e0:s0"].model_copy(
        update={
            "status": ItemStatus.PENDING,
            "failure": None,
            "next_action": "translate",
            "attempts": {
                **record.items["u2:e0:s0"].attempts,
                "translate_cycle_start": record.items["u2:e0:s0"].attempts["translate"],
            },
        }
    )
    store.save_unit(record.model_copy(update={"items": {item.item_id: item}, "unresolved_issues": ()}))
    resumed = ScriptedTransport()

    report = await TranslationEngine(Store(tmp_path), transport=resumed).execute()

    assert report["outcome"] == "needs_attention"
    assert report["ready_to_publish"] is True
    assert [call for call in resumed.calls if call[0] != "coherence"] == [
        ("translate", "u2:e0:s0"),
        ("review", "u2:e0:s0"),
    ]
    assert Store(tmp_path).load_unit("u1").accepted_revision == 0
    assert Store(tmp_path).load_unit("u3").accepted_revision == 0


@pytest.mark.asyncio
async def test_unit_budget_exhaustion_is_local_but_run_budget_pauses_dispatch(tmp_path: Path) -> None:
    local = _make_store(tmp_path / "local", ((("u1", ("One.",)), ("u2", ("Two.",))),))
    record = local.load_unit("u1")
    local.save_unit(
        record.model_copy(
            update={"counters": record.counters.model_copy(update={"http_attempts": record.counters.unit_http_limit})}
        )
    )
    local_transport = ScriptedTransport()

    local_report = await TranslationEngine(local, transport=local_transport).execute()

    assert local_report["outcome"] == "needs_attention"
    assert local.load_unit("u1").items["u1:e0:s0"].status == ItemStatus.NEEDS_ATTENTION
    assert local.load_unit("u2").accepted_revision == 0
    assert not [call for call in local_transport.calls if call[1] == "u1:e0:s0"]

    global_store = _make_store(
        tmp_path / "global",
        ((("u1", ("One.",)), ("u2", ("Two.",)), ("u3", ("Three.",))),),
        concurrency=1,
        run_http_limit=2,
    )
    global_transport = ScriptedTransport()

    global_report = await TranslationEngine(global_store, transport=global_transport).execute()

    assert global_report["outcome"] == "paused"
    assert len(global_transport.calls) == 2
    assert global_store.load_unit("u3").items["u3:e0:s0"].status == ItemStatus.PENDING
    assert global_report["required_units"] == 3
    assert global_report["stop_reason"] == "run HTTP budget exhausted"


@pytest.mark.asyncio
async def test_manual_dependency_does_not_hold_a_worker_or_hide_the_denominator(tmp_path: Path) -> None:
    store = _make_store(tmp_path, ((("blocked", ("Title.",)), ("ready", ("Body.",))),))
    blocked = store.load_unit("blocked")
    dependency = blocked.items["blocked:e0:s0"].model_copy(
        update={"status": ItemStatus.BLOCKED_DEPENDENCY, "next_action": "dependency"}
    )
    store.save_unit(blocked.model_copy(update={"items": {dependency.item_id: dependency}}))
    transport = ScriptedTransport()

    report = await asyncio.wait_for(TranslationEngine(store, transport=transport).execute(), timeout=2)

    assert report["outcome"] == "needs_attention"
    assert report["required_units"] == 2
    assert report["accepted_units"] == 1
    assert report["blocked_dependencies"] >= 1
    assert store.load_unit("ready").accepted_revision == 0
    assert not [call for call in transport.calls if call[1] == "blocked:e0:s0"]


def test_late_old_revision_result_cannot_overwrite_the_current_generation(tmp_path: Path) -> None:
    store = _make_store(tmp_path, ((("long", ("One. ", "Two.")),),))
    engine = TranslationEngine(store, transport=ScriptedTransport())
    engine._load()
    job = Job("translate", "long", "long:e0:s0", 0, 0, "d1")
    payload, manifest = engine._make_request(job)
    current = engine.records["long"]
    engine._save(current.model_copy(update={"revision": 1}))
    raw = _translation(payload, job.item_id, "迟到译文")["raw"]

    with pytest.raises(StaleWrite, match="late item result"):
        engine._apply_translation(job, manifest, raw)

    saved = store.load_unit("long")
    assert saved.revision == 1
    assert saved.items["long:e0:s0"].target_projection is None
    assert saved.items["long:e0:s1"].target_projection is None


@pytest.mark.asyncio
async def test_review_replacement_starts_a_new_revision_and_requires_all_five_checks(
    tmp_path: Path,
) -> None:
    store = _make_store(tmp_path, ((("u1", ("Original meaning.",)),),))
    transport = ScriptedTransport(
        review_scripts={
            "u1:e0:s0": [("replace", "修订后的完整译文"), ("no_change", None)],
        }
    )

    report = await TranslationEngine(store, transport=transport).execute()

    record = store.load_unit("u1")
    assert report["ready_to_publish"] is True
    assert record.revision == 1
    assert record.accepted_revision == 1
    assert record.candidate == "修订后的完整译文"
    assert len(record.history) == 1
    assert record.history[0]["accepted_revision"] is None
    assert record.items["u1:e0:s0"].attempts["review"] == 2
    saved_checks = record.items["u1:e0:s0"].checks["checks"]
    assert isinstance(saved_checks, dict)
    assert set(saved_checks) == {
        "accuracy",
        "fluency",
        "terminology",
        "bindings",
        "script",
    }
    assert Counter(transport.calls)[("review", "u1:e0:s0")] == 2


@pytest.mark.asyncio
async def test_coherence_major_issue_revises_only_the_named_unit_then_rechecks_window(
    tmp_path: Path,
) -> None:
    store = _make_store(
        tmp_path,
        ((("u1", ("First paragraph.",)), ("u2", ("Second paragraph.",))),),
    )
    transport = ScriptedTransport(
        review_scripts={
            "u1:e0:s0": [
                ("no_change", None),
                ("replace", "衔接修订译文"),
                ("no_change", None),
            ]
        },
        coherence_major_once="u1",
    )

    report = await TranslationEngine(store, transport=transport).execute()

    revised = store.load_unit("u1")
    untouched = store.load_unit("u2")
    check = store.read_document_status("d1")
    assert report["ready_to_publish"] is True
    assert revised.revision == 1
    assert revised.accepted_revision == 1
    assert revised.candidate == "衔接修订译文"
    assert revised.counters.coherence_revision_rounds == 1
    assert untouched.revision == 0
    assert untouched.accepted_revision == 0
    assert check.status == "valid"
    assert check.candidate_versions == {"u1": 1, "u2": 0}
    assert transport.coherence_calls == 2


@pytest.mark.asyncio
async def test_coherence_rechecks_after_one_affected_unit_changes_and_the_other_passes_review(
    tmp_path: Path,
) -> None:
    store = _make_store(tmp_path, ((("u1", ("First.",)), ("u2", ("Second.",))),))
    transport = ScriptedTransport(
        review_scripts={
            "u1:e0:s0": [("no_change", None), ("replace", "衔接修订"), ("no_change", None)],
            "u2:e0:s0": [("no_change", None), ("no_change", None)],
        },
        coherence_affected=[("u1", "u2"), ()],
    )

    report = await TranslationEngine(store, transport=transport).execute()

    assert report["ready_to_publish"] is True
    assert store.load_unit("u1").accepted_revision == 1
    assert store.load_unit("u2").accepted_revision == 0
    assert not store.load_unit("u2").unresolved_issues
    assert store.read_document_status("d1").status == "valid"
    assert transport.coherence_calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("second_affected", "expected_status"), [((), "valid"), (("u1", "u2"), "needs_attention")])
async def test_coherence_rechecks_same_revision_after_all_affected_units_pass_review(
    tmp_path: Path,
    second_affected: tuple[str, ...],
    expected_status: str,
) -> None:
    store = _make_store(tmp_path, ((("u1", ("First.",)), ("u2", ("Second.",))),))
    transport = ScriptedTransport(
        review_scripts={
            "u1:e0:s0": [("no_change", None), ("no_change", None)],
            "u2:e0:s0": [("no_change", None), ("no_change", None)],
        },
        coherence_affected=[("u1", "u2"), second_affected],
    )

    report = await TranslationEngine(store, transport=transport).execute()

    check = store.read_document_status("d1")
    assert check.status == expected_status
    assert check.repair_rounds == 1
    assert transport.coherence_calls == 2
    assert store.load_unit("u1").accepted_revision == 0
    assert store.load_unit("u2").accepted_revision == 0
    assert report["ready_to_publish"] is (expected_status == "valid")


@pytest.mark.asyncio
async def test_resume_drops_cached_failed_coherence_window_without_reopening_accepted_targets(
    tmp_path: Path,
) -> None:
    store = _make_store(tmp_path, ((("u1", ("First.",)), ("u2", ("Second.",))),))
    await TranslationEngine(store, transport=ScriptedTransport()).execute()
    check = store.read_document_status("d1")
    window_id = str(check.windows[0]["item_id"])
    failed_window = {
        "item_id": window_id,
        "unit_ids": ["u1", "u2"],
        "issues": [{"code": "continuity", "severity": "major", "message": "mismatch"}],
    }
    store.write_document_status(
        check.model_copy(
            update={
                "status": "blocked_dependency",
                "repair_rounds": 1,
                "dependency_ids": ("u1", "u2"),
                "checks": {**check.checks, window_id: failed_window},
            }
        )
    )
    for unit_id in ("u1", "u2"):
        record = store.load_unit(unit_id)
        issue = FailureRecord(
            scope="unit",
            stage="coherence",
            code="blocking_coherence",
            message="mismatch",
            plan_epoch=record.plan_epoch,
            revision=record.revision,
            retry_action="repair",
        )
        store.save_unit(
            record.model_copy(
                update={
                    "accepted_revision": None,
                    "accepted_target_hash": None,
                    "review": None,
                    "unresolved_issues": (*record.unresolved_issues, issue),
                }
            )
        )
    transport = ScriptedTransport()

    report = await TranslationEngine(Store(tmp_path), transport=transport).execute()

    assert report["ready_to_publish"] is True
    assert transport.calls == [("coherence", window_id)]
    assert all(Store(tmp_path).load_unit(unit_id).accepted_revision == 0 for unit_id in ("u1", "u2"))
    assert Store(tmp_path).read_document_status("d1").status == "valid"


def test_late_review_of_an_old_target_hash_is_rejected_without_undoing_replacement(
    tmp_path: Path,
) -> None:
    store = _make_store(tmp_path, ((("u1", ("Original.",)),),))
    engine = TranslationEngine(store, transport=ScriptedTransport())
    engine._load()
    translation = Job("translate", "u1", "u1:e0:s0", 0, 0, "d1")
    translation_payload, translation_manifest = engine._make_request(translation)
    engine._apply_translation(
        translation,
        translation_manifest,
        _translation(translation_payload, translation.item_id, "初始译文")["raw"],
    )
    engine._complete_candidate("u1")
    review = Job("review", "u1", "u1:e0:s0", 0, 0, "d1")
    review_payload, review_manifest = engine._make_request(review)
    engine._apply_review(
        review,
        review_manifest,
        _review(review_payload, review.item_id, "replace", "当前修订译文")["raw"],
    )

    with pytest.raises(StaleWrite, match="reviewed target changed"):
        engine._apply_review(
            review,
            review_manifest,
            _review(review_payload, review.item_id, "no_change")["raw"],
        )

    saved = store.load_unit("u1")
    assert saved.revision == 1
    assert saved.candidate == "当前修订译文"
    assert saved.accepted_revision is None


def test_accepted_fields_cannot_bypass_local_and_review_evidence(tmp_path: Path) -> None:
    store = _make_store(tmp_path, ((("u1", ("Original.",)),),))
    record = store.load_unit("u1")
    target = "完整译文"
    target_hash = canonical_hash(target)
    items = {
        item_id: item.model_copy(
            update={
                "status": ItemStatus.REVIEWED,
                "target_projection": target,
                "target_hash": target_hash,
            }
        )
        for item_id, item in record.items.items()
    }
    forged = record.model_copy(
        update={
            "candidate": target,
            "target_hash": target_hash,
            "accepted_revision": record.revision,
            "accepted_target_hash": target_hash,
            "items": items,
        }
    )

    assert is_accepted(forged) is False
    with_local_checks = forged.model_copy(
        update={
            "local_checks": {
                "passed": True,
                "input_hash": record.input_hash,
                "revision": record.revision,
                "target_hash": target_hash,
            }
        }
    )
    assert is_accepted(with_local_checks) is False
    fully_evidenced = with_local_checks.model_copy(
        update={
            "review": {
                "passed": True,
                "input_hash": record.input_hash,
                "revision": record.revision,
                "target_hash": target_hash,
            }
        }
    )
    assert is_accepted(fully_evidenced) is True


@pytest.mark.asyncio
async def test_exhausted_run_budget_never_dispatches_or_counts_review_on_resume(
    tmp_path: Path,
) -> None:
    store = _make_store(tmp_path, ((("u1", ("Original.",)),),), run_http_limit=1)
    first_transport = ScriptedTransport()

    first = await TranslationEngine(store, transport=first_transport).execute()

    assert first["outcome"] == "paused"
    assert first_transport.calls == [("translate", "u1:e0:s0")]
    for _ in range(2):
        resumed_transport = ScriptedTransport()
        resumed = await TranslationEngine(Store(tmp_path), transport=resumed_transport).execute()
        assert resumed["outcome"] == "paused"
        assert resumed_transport.calls == []

    record = Store(tmp_path).load_unit("u1")
    assert record.counters.http_attempts == 1
    assert record.counters.translation_attempts == 1
    assert record.counters.review_attempts == 0
    assert record.items["u1:e0:s0"].attempts.get("review", 0) == 0


@pytest.mark.asyncio
async def test_failed_coherence_can_be_explicitly_retried_without_resetting_usage(
    tmp_path: Path,
) -> None:
    store = _make_store(
        tmp_path,
        ((("u1", ("First.",)), ("u2", ("Second.",))),),
    )
    failed_transport = ScriptedTransport(malformed_coherence=True)

    first = await TranslationEngine(store, transport=failed_transport).execute()

    assert first["outcome"] == "needs_attention"
    failed_check = store.read_document_status("d1")
    assert failed_check.status == "needs_attention"
    assert failed_check.http_attempts == 1
    resumed_engine = TranslationEngine(Store(tmp_path), transport=ScriptedTransport())
    resumed_engine.retry_checks(["d1"])

    resumed = await resumed_engine.execute()

    completed_check = Store(tmp_path).read_document_status("d1")
    assert resumed["ready_to_publish"] is True
    assert completed_check.status == "valid"
    assert completed_check.http_attempts == 2
    assert completed_check.retry_history[-1]["http_attempts"] == 1


@pytest.mark.asyncio
async def test_missing_check_file_recovers_repair_round_and_forbids_second_auto_revision(
    tmp_path: Path,
) -> None:
    store = _make_store(
        tmp_path,
        ((("u1", ("First.",)), ("u2", ("Second.",))),),
    )
    first_transport = ScriptedTransport(
        review_scripts={
            "u1:e0:s0": [
                ("no_change", None),
                ("replace", "第一次衔接修订"),
                ("no_change", None),
            ]
        },
        coherence_major_once="u1",
    )
    first = await TranslationEngine(store, transport=first_transport).execute()
    assert first["ready_to_publish"] is True
    before = store.load_unit("u1")
    assert before.counters.coherence_revision_rounds == 1
    (tmp_path / "checks" / "d1.json").unlink()
    second_transport = ScriptedTransport(coherence_major_once="u1")

    second = await TranslationEngine(Store(tmp_path), transport=second_transport).execute()

    after = Store(tmp_path).load_unit("u1")
    rebuilt = Store(tmp_path).read_document_status("d1")
    assert second["outcome"] == "needs_attention"
    assert after.revision == before.revision
    assert after.counters.coherence_revision_rounds == 1
    assert rebuilt.repair_rounds == 1
    assert rebuilt.status == "needs_attention"
    assert not [call for call in second_transport.calls if call[0] == "review"]


@pytest.mark.asyncio
async def test_blocking_coherence_without_affected_units_is_a_protocol_failure(
    tmp_path: Path,
) -> None:
    store = _make_store(
        tmp_path,
        ((("u1", ("First.",)), ("u2", ("Second.",))),),
    )

    report = await asyncio.wait_for(
        TranslationEngine(
            store,
            transport=ScriptedTransport(coherence_empty_major_once=True),
        ).execute(),
        timeout=2,
    )

    check = store.read_document_status("d1")
    assert report["outcome"] == "needs_attention"
    assert check.status == "needs_attention"
    assert check.dependency_ids == ()
    assert check.issues[-1].code == "invalid_response"


@pytest.mark.asyncio
async def test_review_replacement_with_english_prose_never_becomes_accepted(
    tmp_path: Path,
) -> None:
    store = _make_store(tmp_path, ((("u1", ("Original prose.",)),),))
    transport = ScriptedTransport(
        review_scripts={
            "u1:e0:s0": [
                (
                    "replace",
                    "The replacement still contains a complete untranslated English sentence with ordinary prose.",
                ),
                ("no_change", None),
            ]
        }
    )

    report = await TranslationEngine(store, transport=transport).execute()

    record = store.load_unit("u1")
    assert report["outcome"] == "needs_attention"
    assert report["ready_to_publish"] is False
    assert record.revision == 0
    assert record.accepted_revision is None
    assert record.candidate == "译文-u1:e0:s0"
    assert record.items["u1:e0:s0"].status == ItemStatus.NEEDS_ATTENTION
    assert Counter(transport.calls)[("review", "u1:e0:s0")] == 1
    assert "untranslated English prose" in record.unresolved_issues[-1].message


def test_retry_units_rejects_unplanned_unit_without_erasing_preparation_issue(
    tmp_path: Path,
) -> None:
    store = _make_store(
        tmp_path,
        ((("unplanned", ("Cannot fit.",)),),),
        unplanned_units=("unplanned",),
    )
    before = store.load_unit("unplanned")
    engine = TranslationEngine(store, transport=ScriptedTransport())

    with pytest.raises(ValueError, match="no executable source plan"):
        engine.retry_units(["unplanned"], add_unit_http=10, add_run_http=10)

    after = Store(tmp_path).load_unit("unplanned")
    assert after.cut_plan is None
    assert after.counters.unit_http_limit == before.counters.unit_http_limit
    assert after.unresolved_issues == before.unresolved_issues
    assert after.unresolved_issues[0].code == "unit_planning_failed"
