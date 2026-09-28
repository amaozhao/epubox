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
    DocumentStatus,
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
        coherence_missing_calls: Iterable[int] = (),
        coherence_empty_major_once: bool = False,
        delays: dict[str, float] | None = None,
    ) -> None:
        self.translation_failures = set(translation_failures)
        self.review_failures = set(review_failures)
        self.review_scripts = {key: list(value) for key, value in (review_scripts or {}).items()}
        self.coherence_major_once = coherence_major_once
        self.coherence_affected = list(coherence_affected or ())
        self.malformed_coherence = malformed_coherence
        self.coherence_missing_calls = set(coherence_missing_calls)
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
            if self.coherence_calls in self.coherence_missing_calls:
                return {
                    "raw": json.dumps(
                        {"protocol": "epubox-coherence-1", "request_id": payload["request_id"], "items": []}
                    ),
                    "usage": {"input_tokens": 4, "output_tokens": 1},
                }
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


def _scoped_windows() -> list[dict[str, Any]]:
    return [
        {
            "item_id": "body-window",
            "scope": "chapter",
            "unit_ids": ["body1", "body2"],
            "source": ["Body one.", "Body two."],
            "target": [],
        },
        {
            "item_id": "attr-seam",
            "scope": "unit",
            "unit_ids": ["attr"],
            "source": ["Attribute one.", "Attribute two."],
            "target": [],
        },
    ]


def _advance_accepted_revision(store: Store, unit_id: str) -> None:
    record = store.load_unit(unit_id)
    store.save_unit(
        record.model_copy(
            update={
                "revision": record.revision + 1,
                "accepted_revision": record.revision + 1,
                "review": {**(record.review or {}), "revision": record.revision + 1},
            }
        )
    )


@pytest.mark.asyncio
async def test_unfinished_independent_unit_does_not_block_ready_chapter_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(
        tmp_path,
        ((("body1", ("Body one.",)), ("body2", ("Body two.",)), ("attr", ("Attribute one. ", "Attribute two."))),),
    )
    transport = ScriptedTransport(translation_failures={"attr:e0:s0"})
    engine = TranslationEngine(store, transport=transport)
    monkeypatch.setattr(engine, "_windows", lambda document: _scoped_windows())

    report = await engine.execute()

    check = store.read_document_status("d1")
    assert report["outcome"] == "needs_attention"
    assert "body-window" in check.checks
    assert "attr-seam" not in check.checks
    assert check.status == "blocked_dependency"
    assert check.dependency_ids == ("attr",)
    assert check.candidate_versions == {"body1": 0, "body2": 0}
    assert check.window_versions == {"attr-seam": {"attr": 0}}
    assert transport.coherence_calls == 1


@pytest.mark.asyncio
async def test_unfinished_chapter_unit_does_not_block_ready_independent_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(
        tmp_path,
        ((("body1", ("Body one.",)), ("body2", ("Body two.",)), ("attr", ("Attribute one. ", "Attribute two."))),),
    )
    transport = ScriptedTransport(translation_failures={"body1:e0:s0"})
    engine = TranslationEngine(store, transport=transport)
    monkeypatch.setattr(engine, "_windows", lambda document: _scoped_windows())

    await engine.execute()

    check = store.read_document_status("d1")
    assert "body-window" not in check.checks
    assert "attr-seam" in check.checks
    assert check.status == "blocked_dependency"
    assert check.dependency_ids == ("body1",)
    assert transport.coherence_calls == 1


@pytest.mark.asyncio
async def test_independent_revision_invalidates_only_its_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(
        tmp_path,
        ((("body1", ("Body one.",)), ("body2", ("Body two.",)), ("attr", ("Attribute one. ", "Attribute two."))),),
    )
    first = TranslationEngine(store, transport=ScriptedTransport())
    monkeypatch.setattr(first, "_windows", lambda document: _scoped_windows())
    await first.execute()
    _advance_accepted_revision(store, "attr")
    transport = ScriptedTransport()
    resumed = TranslationEngine(Store(tmp_path), transport=transport)
    monkeypatch.setattr(resumed, "_windows", lambda document: _scoped_windows())

    report = await resumed.execute()

    check = Store(tmp_path).read_document_status("d1")
    assert report["ready_to_publish"] is True
    assert transport.calls == [("coherence", "attr-seam")]
    assert set(check.checks) == {"body-window", "attr-seam"}
    assert check.candidate_versions == {"body1": 0, "body2": 0}
    assert check.window_versions == {"attr-seam": {"attr": 1}}


@pytest.mark.asyncio
async def test_chapter_revision_invalidates_chapter_windows_but_reuses_independent_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(
        tmp_path,
        ((("body1", ("Body one.",)), ("body2", ("Body two.",)), ("attr", ("Attribute one. ", "Attribute two."))),),
    )
    first = TranslationEngine(store, transport=ScriptedTransport())
    monkeypatch.setattr(first, "_windows", lambda document: _scoped_windows())
    await first.execute()
    _advance_accepted_revision(store, "body1")
    transport = ScriptedTransport()
    resumed = TranslationEngine(Store(tmp_path), transport=transport)
    monkeypatch.setattr(resumed, "_windows", lambda document: _scoped_windows())

    report = await resumed.execute()

    check = Store(tmp_path).read_document_status("d1")
    assert report["ready_to_publish"] is True
    assert transport.calls == [("coherence", "body-window")]
    assert set(check.checks) == {"body-window", "attr-seam"}
    assert check.candidate_versions == {"body1": 1, "body2": 0}
    assert check.window_versions == {"attr-seam": {"attr": 0}}


def test_document_status_defaults_window_versions_for_old_json() -> None:
    assert DocumentStatus.model_validate({"document_id": "d1"}).window_versions == {}


@pytest.mark.asyncio
async def test_ready_nav_document_with_derived_units_recovers_without_model_calls(tmp_path: Path) -> None:
    store = _make_store(
        tmp_path,
        (
            (("nav1", ("Chapter one",)), ("nav2", ("Chapter two",))),
            (("source1", ("Chapter one",)), ("source2", ("Chapter two",))),
        ),
    )
    await TranslationEngine(store, transport=ScriptedTransport()).execute()
    book = store.read_bookplan(ready=True)
    document = store.read_document("d1")
    nodes = {
        **document.nodes,
        "nav-root": NodeRecord(
            node_key="nav-root",
            element_path=(1,),
            qname="{http://www.w3.org/1999/xhtml}nav",
        ),
    }
    bindings = tuple(
        {
            "kind": "derived_navigation",
            "unit_id": target,
            "source_unit_id": source,
        }
        for target, source in (("nav1", "source1"), ("nav2", "source2"))
    )
    updated_document = document.model_copy(update={"nodes": nodes, "derived_bindings": bindings})
    document_hash = store._atomic_write(tmp_path / "documents" / "d1.json", updated_document)
    store._atomic_write(
        tmp_path / "bookplan.json",
        book.model_copy(update={"document_hashes": {**book.document_hashes, "d1": document_hash}}),
    )
    for target, source in (("nav1", "source1"), ("nav2", "source2")):
        record = store.load_unit(target)
        source_record = store.load_unit(source)
        store.save_unit(
            record.model_copy(
                update={
                    "accepted_revision": None,
                    "accepted_target_hash": None,
                    "review": None,
                    "derived": {
                        "state": "valid",
                        "source_unit_id": source,
                        "source_revision": source_record.revision,
                        "source_target_hash": source_record.target_hash,
                        "target": record.candidate,
                        "target_hash": record.target_hash,
                    },
                }
            )
        )
    check = store.read_document_status("d1")
    store.write_document_status(
        check.model_copy(update={"status": "blocked_dependency", "dependency_ids": ("nav1", "nav2")})
    )
    transport = ScriptedTransport()

    report = await TranslationEngine(Store(tmp_path), transport=transport).execute()

    assert report["ready_to_publish"] is True
    assert report["completed_units"] == report["required_units"] == 4
    assert transport.calls == []
    assert Store(tmp_path).read_document_status("d1").status == "valid"
    assert all(Store(tmp_path).load_unit(unit_id).accepted_revision is None for unit_id in ("nav1", "nav2"))


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
    assert failed_check.http_attempts == 2
    assert failed_check.retry_history[-1]["action"] == "automatic_protocol_repair"
    resumed_engine = TranslationEngine(Store(tmp_path), transport=ScriptedTransport())
    resumed_engine.retry_checks(["d1"])

    resumed = await resumed_engine.execute()

    completed_check = Store(tmp_path).read_document_status("d1")
    assert resumed["ready_to_publish"] is True
    assert completed_check.status == "valid"
    assert completed_check.http_attempts == 3
    assert completed_check.retry_history[-1]["http_attempts"] == 2


@pytest.mark.asyncio
async def test_last_missing_coherence_window_gets_one_persisted_automatic_repair(tmp_path: Path) -> None:
    store = _make_store(
        tmp_path,
        ((("u1", ("First.",)), ("u2", ("Second.",)), ("u3", ("Third.",))),),
    )
    transport = ScriptedTransport(coherence_missing_calls={2})

    report = await TranslationEngine(store, transport=transport).execute()

    check = store.read_document_status("d1")
    assert report["ready_to_publish"] is True
    assert transport.coherence_calls == 3
    assert len(check.checks) == len(check.windows) == 2
    repairs = [event for event in check.retry_history if event.get("action") == "automatic_protocol_repair"]
    assert len(repairs) == 1
    assert repairs[0]["window_id"] == check.windows[-1]["item_id"]
    assert repairs[0]["summary_hash"] == check.summary_hash
    assert not [issue for issue in check.issues if issue.code == "coherence_protocol_retry"]


@pytest.mark.asyncio
async def test_resumed_coherence_window_does_not_get_a_second_free_protocol_repair(tmp_path: Path) -> None:
    store = _make_store(
        tmp_path,
        ((("u1", ("First.",)), ("u2", ("Second.",)), ("u3", ("Third.",))),),
    )
    await TranslationEngine(store, transport=ScriptedTransport()).execute()
    check = store.read_document_status("d1")
    window_id = str(check.windows[-1]["item_id"])
    issue = FailureRecord(
        scope="request",
        stage="coherence",
        code="coherence_protocol_retry",
        message="missing coherence window",
        request_id="prior-request",
        item_id=window_id,
        plan_epoch=0,
        revision=0,
        retry_action="automatic",
    )
    store.write_document_status(
        check.model_copy(
            update={
                "status": "pending",
                "checks": {key: value for key, value in check.checks.items() if key != window_id},
                "issues": (*check.issues, issue),
                "retry_history": (
                    *check.retry_history,
                    {
                        "action": "automatic_protocol_repair",
                        "window_id": window_id,
                        "summary_hash": check.summary_hash,
                        "request_id": "prior-request",
                        "reason": "missing coherence window",
                        "http_attempts": check.http_attempts,
                    },
                ),
            }
        )
    )
    transport = ScriptedTransport(coherence_missing_calls={1})

    report = await TranslationEngine(Store(tmp_path), transport=transport).execute()

    resumed = Store(tmp_path).read_document_status("d1")
    assert report["outcome"] == "needs_attention"
    assert transport.coherence_calls == 1
    assert resumed.status == "needs_attention"
    assert len([event for event in resumed.retry_history if event.get("action") == "automatic_protocol_repair"]) == 1


@pytest.mark.asyncio
async def test_explicit_check_retry_preserves_passed_windows_and_only_fills_the_hole(tmp_path: Path) -> None:
    store = _make_store(
        tmp_path,
        ((("u1", ("First.",)), ("u2", ("Second.",)), ("u3", ("Third.",))),),
    )
    first_transport = ScriptedTransport(coherence_missing_calls={2, 3})
    await TranslationEngine(store, transport=first_transport).execute()
    failed = store.read_document_status("d1")
    passed_ids = set(failed.checks)
    assert failed.status == "needs_attention"
    assert failed.http_attempts == 3
    assert len(passed_ids) == 1

    resumed_transport = ScriptedTransport()
    retry = TranslationEngine(Store(tmp_path), transport=resumed_transport)
    retry.retry_checks(["d1"])
    pending = Store(tmp_path).read_document_status("d1")
    assert set(pending.checks) == passed_ids
    assert pending.http_attempts == 3

    report = await retry.execute()

    completed = Store(tmp_path).read_document_status("d1")
    assert report["ready_to_publish"] is True
    assert resumed_transport.coherence_calls == 1
    assert completed.http_attempts == 4
    assert len(completed.checks) == len(completed.windows) == 2


@pytest.mark.asyncio
async def test_explicit_retry_of_complete_valid_check_is_a_noop_without_http(tmp_path: Path) -> None:
    store = _make_store(
        tmp_path,
        ((("u1", ("First.",)), ("u2", ("Second.",)), ("u3", ("Third.",))),),
    )
    await TranslationEngine(store, transport=ScriptedTransport()).execute()
    before = store.read_document_status("d1")
    resumed_transport = ScriptedTransport()
    retry = TranslationEngine(Store(tmp_path), transport=resumed_transport)

    retry.retry_checks(["d1"])
    pending = Store(tmp_path).read_document_status("d1")
    report = await retry.execute()

    after = Store(tmp_path).read_document_status("d1")
    assert pending.status == "valid"
    assert set(pending.checks) == set(before.checks)
    assert report["ready_to_publish"] is True
    assert resumed_transport.coherence_calls == 0
    assert after.http_attempts == before.http_attempts


@pytest.mark.parametrize("invalid", ["item_id", "unit_ids", "issues", "severity"])
def test_explicit_retry_drops_protocol_invalid_cached_window(tmp_path: Path, invalid: str) -> None:
    store = _make_store(
        tmp_path,
        ((("u1", ("First.",)), ("u2", ("Second.",)), ("u3", ("Third.",))),),
    )
    asyncio.run(TranslationEngine(store, transport=ScriptedTransport()).execute())
    check = store.read_document_status("d1")
    window_id = str(check.windows[0]["item_id"])
    saved = check.checks[window_id]
    assert isinstance(saved, dict)
    cached: dict[str, Any] = dict(saved)
    if invalid == "item_id":
        cached["item_id"] = "wrong-window"
    elif invalid == "unit_ids":
        cached["unit_ids"] = ["outside-window"]
    elif invalid == "issues":
        cached["issues"] = "not-an-array"
    else:
        cached["issues"] = [{"code": "x", "severity": "bogus", "message": "bad"}]
    store.write_document_status(
        check.model_copy(update={"status": "needs_attention", "checks": {**check.checks, window_id: cached}})
    )

    TranslationEngine(Store(tmp_path), transport=ScriptedTransport()).retry_checks(["d1"])

    pending = Store(tmp_path).read_document_status("d1")
    assert pending.status == "pending"
    assert window_id not in pending.checks


def test_explicit_check_retry_invalidates_cache_when_candidate_version_changes(tmp_path: Path) -> None:
    store = _make_store(
        tmp_path,
        ((("u1", ("First.",)), ("u2", ("Second.",)), ("u3", ("Third.",))),),
    )
    engine = TranslationEngine(store, transport=ScriptedTransport())
    asyncio.run(engine.execute())
    record = store.load_unit("u1")
    review = {**(record.review or {}), "revision": record.revision + 1}
    store.save_unit(
        record.model_copy(
            update={
                "revision": record.revision + 1,
                "accepted_revision": record.revision + 1,
                "review": review,
            }
        )
    )

    TranslationEngine(Store(tmp_path), transport=ScriptedTransport()).retry_checks(["d1"])

    pending = Store(tmp_path).read_document_status("d1")
    assert pending.status == "pending"
    assert pending.checks == {}


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
async def test_blocking_coherence_without_affected_units_gets_one_protocol_repair(
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
    assert report["ready_to_publish"] is True
    assert check.status == "valid"
    assert check.dependency_ids == ()
    assert check.http_attempts == 2
    assert not check.issues


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
