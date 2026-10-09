from __future__ import annotations

import pytest

import engine.execution.coherence as coherence_module
import engine.execution.request as request_module
from engine.agents.runtime import MAX_MODEL_INPUT_TOKENS
from engine.orchestrator import (
    TranslationEngine,
)
from engine.schemas.contracts import ItemStatus
from engine.services.atomic import StoreError
from tests.engine.execution.support import (
    PartialBatchTransport,
    ScriptedTransport,
    TruncatedOnceTransport,
    ready_batch_store,
    ready_store,
)


@pytest.mark.asyncio
async def test_one_oversized_input_does_not_dispatch_or_fail_its_batch_siblings(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await ready_batch_store(tmp_path, "oversized-run")
    transport = PartialBatchTransport()
    transport.omitted = "disabled"
    engine = TranslationEngine(store, transport=transport)
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
    for batch in batches:
        await engine._run_batch(batch)
    dispatched_ids = {item_id for stage, item_ids in transport.calls if stage == "translate" for item_id in item_ids}

    assert bad.item_id not in dispatched_ids
    assert normal_ids.issubset(dispatched_ids)
    assert store.read_unit(bad.unit_id).items[bad.item_id].status == ItemStatus.NEEDS_ATTENTION
    assert all(store.read_unit(job.unit_id).items[job.item_id].status == ItemStatus.LOCAL_VALID for job in jobs[1:])


@pytest.mark.asyncio
async def test_input_budget_splits_before_request_persistence_or_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await ready_batch_store(tmp_path, "input-split")
    transport = PartialBatchTransport()
    transport.omitted = "disabled"
    engine = TranslationEngine(store, transport=transport)
    jobs = tuple(engine._ready_jobs()[:2])
    original = request_module.model_input_budget

    def budget(stage, payload):
        result = original(stage, payload)
        return result | {
            "estimated_input_tokens": MAX_MODEL_INPUT_TOKENS + 1
            if len(payload["items"]) > 1
            else result["estimated_input_tokens"]
        }

    monkeypatch.setattr(request_module, "model_input_budget", budget)
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
        request_module,
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
    original = coherence_module.save_window_result
    failed_once = False

    def crash_after_journal(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise StoreError("crash after coherence journal")
        return original(*args, **kwargs)

    monkeypatch.setattr(coherence_module, "save_window_result", crash_after_journal)
    failed = await engine.run()
    assert failed.status == "failed"
    monkeypatch.setattr(coherence_module, "save_window_result", original)

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
async def test_singleton_truncated_translation_gets_one_automatic_fallback(tmp_path) -> None:
    store, unit, _ = ready_store(tmp_path)
    transport = TruncatedOnceTransport("translate")

    result = await TranslationEngine(store, transport=transport).run()
    item = next(iter(store.read_unit(unit.unit_id).items.values()))

    assert result.status == "translated"
    assert item.status == ItemStatus.REVIEWED
    assert store.read_unit(unit.unit_id).counters[f"automatic_recovery_translate:{item.item_id}"] == 1
    assert [stage for stage, _ in transport.calls] == ["translate", "translate", "review"]


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
    engine.checks[first_id] = coherence_module.save_document_check(
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
