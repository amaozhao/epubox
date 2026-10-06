from __future__ import annotations

import pytest

import engine.execution.coherence as coherence_module
from engine.agents.runtime import MAX_MODEL_INPUT_TOKENS
from engine.epub.publication import _accepted_target
from engine.orchestrator import (
    TranslationEngine,
)
from tests.engine.execution.support import (
    PartialBatchTransport,
    ready_batch_store,
)


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
    original = coherence_module.model_input_budget
    engine = TranslationEngine(store, transport=transport)
    coherence_limit = engine._coherence_input_limit()

    def budget(stage, payload):
        result = original(stage, payload)
        if stage == "coherence" and len(payload["items"]) > 3:
            return result | {"estimated_input_tokens": coherence_limit + 1}
        return result

    monkeypatch.setattr(coherence_module, "model_input_budget", budget)
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
