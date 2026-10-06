from __future__ import annotations

import json
from typing import Any, cast

import pytest

from engine.epub.preparation import PreparationConfig
from engine.orchestrator import (
    TranslationEngine,
    import_repair_file,
    retry_failed_units,
    validate_repair_file,
    validate_retry_failed_units,
)
from engine.schemas.contracts import ItemStatus, canonical_hash
from engine.services.preparation import prepare_translation
from engine.services.store import RunStore
from engine.services.terms.planning import TERM_PLANNER_VERSION
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker
from tests.engine.execution.support import (
    AlwaysTruncatedTransport,
    BatchNeedsAttentionTransport,
    NeedsAttentionOnceTransport,
    PartialBatchTransport,
    ProjectionFailuresTransport,
    ScriptedTransport,
    ready_batch_store,
    ready_long_store,
    ready_store,
)


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
    assert retried.counters == dict(failed.counters) | {f"explicit_retry_translate:{item_id}": 1}

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
async def test_projection_failure_uses_one_extra_singleton_after_old_logical_cap(tmp_path) -> None:
    store, unit, _ = ready_store(tmp_path)
    transport = ProjectionFailuresTransport(3)

    result = await TranslationEngine(store, transport=transport).run()
    record = store.read_unit(unit.unit_id)
    item_id = next(iter(record.items))

    assert result.status == "translated"
    assert [stage for stage, _ in transport.calls].count("translate") == 4
    assert record.counters[f"automatic_recovery_translate:{item_id}"] == 1


@pytest.mark.asyncio
async def test_automatic_recovery_stops_after_one_grant_and_restart_does_not_loop(tmp_path) -> None:
    store, unit, _ = ready_store(tmp_path)
    first_transport = AlwaysTruncatedTransport()

    first = await TranslationEngine(store, transport=first_transport).run()
    failed = store.read_unit(unit.unit_id)
    item_id = next(iter(failed.items))

    assert first.status == "needs_attention"
    assert [stage for stage, _ in first_transport.calls] == ["translate", "translate"]
    assert failed.counters[f"automatic_recovery_translate:{item_id}"] == 1
    assert failed.items[item_id].status == ItemStatus.NEEDS_ATTENTION

    restarted_transport = ScriptedTransport()
    restarted = await TranslationEngine(store, transport=restarted_transport).run()
    assert restarted.status == "needs_attention"
    assert restarted_transport.calls == []


@pytest.mark.asyncio
async def test_explicit_retry_grant_dispatches_after_automatic_logical_cap(tmp_path) -> None:
    store, unit, _ = ready_store(tmp_path)
    exhausted = await TranslationEngine(store, transport=ProjectionFailuresTransport(4)).run()
    failed = store.read_unit(unit.unit_id)
    item_id = next(iter(failed.items))
    spent = failed.counters["http_attempts"]

    assert exhausted.status == "needs_attention"
    assert failed.items[item_id].failure == {
        "stage": "translate",
        "code": "projection_protocol_rejected",
        "message": "unknown projection marker: unknown",
    }
    retried = retry_failed_units(store, (unit.unit_id,))[0]
    assert retried.counters[f"explicit_retry_translate:{item_id}"] == 1
    assert retried.counters["http_attempts"] == spent

    transport = ScriptedTransport()
    result = await TranslationEngine(store, transport=transport).run()
    assert result.status == "translated"
    assert [stage for stage, _ in transport.calls] == ["translate", "review"]


@pytest.mark.asyncio
async def test_review_needs_attention_requires_replacement_then_full_review(tmp_path) -> None:
    store, unit, _ = ready_store(tmp_path)
    transport = NeedsAttentionOnceTransport()

    result = await TranslationEngine(store, transport=transport).run()
    record = store.read_unit(unit.unit_id)
    review_payloads = transport.review_payloads

    assert result.status == "translated"
    assert record.revision == record.accepted_revision == 2
    assert len(review_payloads) == 3
    assert "required_revision" in review_payloads[1]["items"][0]
    assert review_payloads[1]["items"][0]["required_revision"][0]["issues"][0]["code"] == "meaning"
    assert "required_revision" not in review_payloads[2]["items"][0]
    assert not any(issue.get("code") == "blocking_review" for issue in record.unresolved_issues)


def test_typed_review_failure_keeps_structured_issues_beyond_message_limit(tmp_path) -> None:
    store, unit, initial = ready_store(tmp_path)
    item_id = next(iter(initial.items))
    assert initial.cut_plan is not None
    source = initial.cut_plan.segments[0].source_projection
    item = initial.items[item_id].model_copy(
        update={
            "status": ItemStatus.LOCAL_VALID,
            "target_projection": source,
            "target_hash": canonical_hash(source),
            "next_action": "review",
        }
    )
    record = store.save_unit(
        initial.model_copy(update={"record_version": initial.record_version + 1, "items": {item_id: item}}),
        expected_record_version=initial.record_version,
    )
    engine = TranslationEngine(store, transport=ScriptedTransport())
    long_message = "x" * 2500
    issues = [{"code": "meaning", "severity": "major", "message": long_message}]

    engine._fail_item(
        record,
        item_id,
        "review",
        str(issues),
        retry=False,
        code="review_needs_attention",
        details=cast(Any, {"issues": issues}),
    )
    failed = store.read_unit(unit.unit_id).items[item_id]

    assert failed.failure is not None
    saved_issues = cast(list[dict[str, Any]], failed.failure["issues"])
    assert len(str(failed.failure["message"])) == 2000
    assert saved_issues[0]["message"] == long_message
    assert engine._recover_terminal_items() is True


@pytest.mark.asyncio
async def test_legacy_truncated_review_failure_recovers_from_validated_response_journal(tmp_path) -> None:
    store, unit, initial = ready_store(tmp_path)
    item_id = next(iter(initial.items))
    assert initial.cut_plan is not None
    source = initial.cut_plan.segments[0].source_projection
    item = initial.items[item_id].model_copy(
        update={
            "status": ItemStatus.LOCAL_VALID,
            "target_projection": source,
            "target_hash": canonical_hash(source),
            "next_action": "review",
        }
    )
    store.save_unit(
        initial.model_copy(
            update={"record_version": initial.record_version + 1, "items": {item_id: item}, "candidate": source}
        ),
        expected_record_version=initial.record_version,
    )
    long_message = "journal issue " * 250
    transport = ScriptedTransport(
        review_decision="needs_attention",
        review_issues=[{"code": "meaning", "severity": "major", "message": long_message}],
    )
    engine = TranslationEngine(store, transport=transport)
    job = next(job for job in engine._ready_jobs() if job.stage == "review")
    await engine._run_batch((job,))
    typed = store.read_unit(unit.unit_id)
    typed_item = typed.items[item_id]
    assert typed_item.failure is not None and typed_item.request_id is not None
    legacy = typed_item.model_copy(
        update={
            "failure": {
                "stage": "review",
                "code": "request_failed",
                "message": str(typed_item.failure["issues"])[:2000],
            }
        }
    )
    store.save_unit(
        typed.model_copy(update={"record_version": typed.record_version + 1, "items": {item_id: legacy}}),
        expected_record_version=typed.record_version,
    )

    resumed_transport = ScriptedTransport()
    resumed = TranslationEngine(store, transport=resumed_transport)
    assert resumed._recover_terminal_items() is True
    recovered = store.read_unit(unit.unit_id)
    blocking = next(issue for issue in recovered.unresolved_issues if issue.get("code") == "blocking_review")
    saved_issues = cast(list[dict[str, Any]], blocking["issues"])
    assert saved_issues[0]["message"] == long_message
    assert resumed_transport.calls == []


@pytest.mark.asyncio
async def test_two_units_recover_independently_from_one_legacy_review_journal(tmp_path) -> None:
    store = await ready_batch_store(tmp_path, "shared-legacy-review")
    transport = BatchNeedsAttentionTransport()
    transport.omitted = "disabled"
    engine = TranslationEngine(store, transport=transport)
    translate_jobs = engine._ready_jobs()
    for batch in engine._pack_jobs(translate_jobs):
        await engine._run_batch(batch)
    engine._advance_local_state()
    review_jobs = [job for job in engine._ready_jobs() if job.stage == "review"][:2]
    assert len(review_jobs) == 2 and len({job.unit_id for job in review_jobs}) == 2
    await engine._run_batch(tuple(review_jobs))

    for job in review_jobs:
        record = store.read_unit(job.unit_id)
        item = record.items[job.item_id]
        assert item.failure is not None and item.request_id is not None
        legacy = item.model_copy(
            update={
                "failure": {
                    "stage": "review",
                    "code": "request_failed",
                    "message": str(item.failure["issues"])[:12],
                }
            }
        )
        store.save_unit(
            record.model_copy(
                update={
                    "record_version": record.record_version + 1,
                    "items": dict(record.items) | {job.item_id: legacy},
                }
            ),
            expected_record_version=record.record_version,
        )

    resumed_transport = ScriptedTransport()
    resumed = TranslationEngine(store, transport=resumed_transport)
    assert resumed._recover_terminal_items() is True

    for job in review_jobs:
        recovered = store.read_unit(job.unit_id)
        assert recovered.counters[f"automatic_recovery_review:{job.item_id}"] == 1
        assert any(
            issue.get("code") == "blocking_review" and issue.get("item_id") == job.item_id
            for issue in recovered.unresolved_issues
        )
    assert resumed_transport.calls == []


@pytest.mark.asyncio
async def test_two_review_failures_in_one_unit_each_get_one_replacement_then_full_review(tmp_path) -> None:
    store = await ready_long_store(tmp_path, "two-review-failures")
    transport = PartialBatchTransport()
    transport.omitted = "disabled"
    assert (await TranslationEngine(store, transport=transport).run()).status == "translated"
    record = max(
        (store.read_unit(unit_id) for unit_id in store.read_bookplan().unit_ids),
        key=lambda value: len(value.items),
    )
    failed_ids = tuple(record.items)[:2]
    issue = [{"code": "meaning", "severity": "major", "message": "replace this item"}]
    items = dict(record.items)
    for item_id in failed_ids:
        items[item_id] = items[item_id].model_copy(
            update={
                "status": ItemStatus.NEEDS_ATTENTION,
                "failure": {
                    "stage": "review",
                    "code": "review_needs_attention",
                    "message": str(issue),
                    "issues": issue,
                },
                "next_action": "repair",
            }
        )
    store.save_unit(
        record.model_copy(
            update={
                "record_version": record.record_version + 1,
                "items": items,
                "accepted_revision": None,
                "accepted_target_hash": None,
                "review": None,
            }
        ),
        expected_record_version=record.record_version,
    )

    resumed_transport = PartialBatchTransport()
    resumed_transport.omitted = "disabled"
    result = await TranslationEngine(store, transport=resumed_transport).run()
    recovered = store.read_unit(record.unit_id)

    assert result.status == "translated"
    assert recovered.accepted_revision == recovered.revision
    assert all(recovered.counters[f"replacement_cycle:{item_id}"] >= 1 for item_id in failed_ids)
    assert not any(issue.get("code") == "blocking_review" for issue in recovered.unresolved_issues)


@pytest.mark.asyncio
async def test_truncation_replan_precedes_review_recovery_and_clears_stale_block(tmp_path) -> None:
    store = await ready_long_store(tmp_path, "mixed-recovery")
    record = max(
        (store.read_unit(unit_id) for unit_id in store.read_bookplan().unit_ids),
        key=lambda value: len(value.items),
    )
    assert len(record.items) >= 2 and record.plan_epoch == 0
    assert record.cut_plan is not None
    review_id, translate_id = tuple(record.items)[:2]
    review_segment = next(segment for segment in record.cut_plan.segments if segment.item_id == review_id)
    review_item = record.items[review_id].model_copy(
        update={
            "status": ItemStatus.NEEDS_ATTENTION,
            "target_projection": review_segment.source_projection,
            "target_hash": canonical_hash(review_segment.source_projection),
            "failure": {
                "stage": "review",
                "code": "review_needs_attention",
                "message": str([{"code": "meaning", "severity": "major", "message": "replace"}]),
                "issues": [{"code": "meaning", "severity": "major", "message": "replace"}],
            },
        }
    )
    translate_item = record.items[translate_id].model_copy(
        update={
            "status": ItemStatus.NEEDS_ATTENTION,
            "failure": {
                "stage": "translate",
                "code": "model_response_truncated",
                "message": "model response was truncated",
            },
        }
    )
    saved = store.save_unit(
        record.model_copy(
            update={
                "record_version": record.record_version + 1,
                "items": dict(record.items) | {review_id: review_item, translate_id: translate_item},
                "unresolved_issues": (
                    {
                        "stage": "review",
                        "code": "blocking_review",
                        "item_id": review_id,
                        "message": "stale",
                    },
                ),
            }
        ),
        expected_record_version=record.record_version,
    )

    engine = TranslationEngine(store, transport=ScriptedTransport())
    assert engine._recover_terminal_items() is True
    replanned = store.read_unit(saved.unit_id)

    assert replanned.plan_epoch == 1
    assert not any(issue.get("code") == "blocking_review" for issue in replanned.unresolved_issues)
    assert not any(key.startswith("automatic_recovery_review:") for key in replanned.counters)
    assert all(item.target_projection is None for item in replanned.items.values())


@pytest.mark.asyncio
async def test_unresolved_blocking_review_can_never_be_accepted(tmp_path) -> None:
    store, unit, _ = ready_store(tmp_path)
    transport = ScriptedTransport()
    assert (await TranslationEngine(store, transport=transport).run()).status == "translated"
    record = store.read_unit(unit.unit_id)
    blocked = store.save_unit(
        record.model_copy(
            update={
                "record_version": record.record_version + 1,
                "accepted_revision": None,
                "accepted_target_hash": None,
                "review": None,
                "unresolved_issues": (
                    {
                        "stage": "review",
                        "code": "blocking_review",
                        "item_id": next(iter(record.items)),
                        "message": "must not accept",
                    },
                ),
            }
        ),
        expected_record_version=record.record_version,
    )

    resumed_transport = ScriptedTransport()
    result = await TranslationEngine(store, transport=resumed_transport).run()
    persisted = store.read_unit(unit.unit_id)

    assert result.status == "needs_attention"
    assert persisted.accepted_revision is None
    assert persisted.record_version == blocked.record_version
    assert resumed_transport.calls == []


def test_automatic_recovery_requires_matching_item_state_and_remaining_budget(tmp_path) -> None:
    store, unit, initial = ready_store(tmp_path)
    item_id = next(iter(initial.items))
    mismatched = initial.items[item_id].model_copy(
        update={
            "status": ItemStatus.NEEDS_ATTENTION,
            "failure": {
                "stage": "review",
                "code": "review_needs_attention",
                "message": str([{"code": "meaning", "severity": "major", "message": "replace"}]),
                "issues": [{"code": "meaning", "severity": "major", "message": "replace"}],
            },
        }
    )
    saved = store.save_unit(
        initial.model_copy(update={"record_version": initial.record_version + 1, "items": {item_id: mismatched}}),
        expected_record_version=initial.record_version,
    )
    assert TranslationEngine(store, transport=ScriptedTransport())._recover_terminal_items() is False

    eligible = mismatched.model_copy(
        update={
            "failure": {
                "stage": "translate",
                "code": "projection_protocol_rejected",
                "message": "unknown projection marker: bad",
            }
        }
    )
    exhausted = store.save_unit(
        saved.model_copy(
            update={
                "record_version": saved.record_version + 1,
                "items": {item_id: eligible},
                "counters": {"http_attempts": 24, "unit_http_limit": 24},
            }
        ),
        expected_record_version=saved.record_version,
    )
    assert TranslationEngine(store, transport=ScriptedTransport())._recover_terminal_items() is False
    assert store.read_unit(unit.unit_id) == exhausted

    run_store, run_unit, run_initial = ready_store(tmp_path / "run-budget")
    run_item_id = next(iter(run_initial.items))
    run_item = run_initial.items[run_item_id].model_copy(
        update={
            "status": ItemStatus.NEEDS_ATTENTION,
            "failure": {
                "stage": "translate",
                "code": "projection_protocol_rejected",
                "message": "unknown projection marker: bad",
            },
        }
    )
    run_store.save_unit(
        run_initial.model_copy(
            update={"record_version": run_initial.record_version + 1, "items": {run_item_id: run_item}}
        ),
        expected_record_version=run_initial.record_version,
    )
    run_engine = TranslationEngine(run_store, transport=ScriptedTransport())
    run_engine.run_limit = run_engine._spent()
    assert run_engine._recover_terminal_items() is False
    assert run_store.read_unit(run_unit.unit_id).items[run_item_id].status == ItemStatus.NEEDS_ATTENTION


@pytest.mark.asyncio
async def test_automatic_recovery_is_classified_and_persistently_idempotent(tmp_path) -> None:
    store = await ready_batch_store(tmp_path, "classified-recovery")
    unit_ids = store.read_bookplan().unit_ids[:2]
    messages = ("unknown projection marker: bad", "provider returned 500")
    item_ids: list[str] = []
    for unit_id, message in zip(unit_ids, messages, strict=True):
        record = store.read_unit(unit_id)
        item_id = next(iter(record.items))
        item_ids.append(item_id)
        item = record.items[item_id].model_copy(
            update={
                "status": ItemStatus.NEEDS_ATTENTION,
                "failure": {"stage": "translate", "code": "request_failed", "message": message},
            }
        )
        store.save_unit(
            record.model_copy(
                update={"record_version": record.record_version + 1, "items": dict(record.items) | {item_id: item}}
            ),
            expected_record_version=record.record_version,
        )

    first = TranslationEngine(store, transport=ScriptedTransport())
    assert first._recover_terminal_items() is True
    eligible = store.read_unit(unit_ids[0])
    ineligible = store.read_unit(unit_ids[1])
    assert eligible.items[item_ids[0]].status == ItemStatus.RETRY_WAIT
    assert eligible.counters[f"automatic_recovery_translate:{item_ids[0]}"] == 1
    assert ineligible.items[item_ids[1]].status == ItemStatus.NEEDS_ATTENTION

    restarted = TranslationEngine(store, transport=ScriptedTransport())
    assert restarted._recover_terminal_items() is False
    assert store.read_unit(unit_ids[0]) == eligible
    assert store.read_unit(unit_ids[1]) == ineligible


def test_explicit_retry_grant_is_integer_and_duplicate_state_action_is_idempotent(tmp_path) -> None:
    store, unit, initial = ready_store(tmp_path)
    item_id = next(iter(initial.items))
    failed = initial.items[item_id].model_copy(
        update={
            "status": ItemStatus.NEEDS_ATTENTION,
            "failure": {"stage": "translate", "code": "request_failed", "message": "manual retry"},
        }
    )
    store.save_unit(
        initial.model_copy(update={"record_version": initial.record_version + 1, "items": {item_id: failed}}),
        expected_record_version=initial.record_version,
    )

    first = retry_failed_units(store, (unit.unit_id,))[0]
    second = retry_failed_units(store, (unit.unit_id,))

    assert first.counters[f"explicit_retry_translate:{item_id}"] == 1
    assert second == ()
    assert store.read_unit(unit.unit_id) == first


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
