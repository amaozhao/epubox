from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest

from engine.agents.runtime import model_input_budget as runtime_input_budget
from engine.epub.preparation import PreparationConfig, prepare_book
from engine.schemas.contracts import Attempt, RequestManifest, TermExtractionRecord
from engine.services.preparation_pipeline import resume_preparation
from engine.services.store import RunStore
from engine.services.term_runner import TermRunner
from engine.services.terms.planning import TERM_PLANNER_VERSION, plan_term_extraction
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker


def _prepare(tmp_path: Path, **extraction_overrides: int) -> tuple[RunStore, tuple[str, ...]]:
    source = make_epub(
        tmp_path / "source.epub",
        {
            "one.xhtml": "<p>Memory allocation is fast.</p>",
            "two.xhtml": "<p>Memory recovery is important.</p>",
        },
    )
    prepared = prepare_book(
        source,
        tmp_path / "work",
        PreparationConfig(
            run_id="test-run",
            extraction_config={
                "max_primary_chars": 100,
                "strategy": TERM_PLANNER_VERSION,
                "prompt_version": "epubox-v25-3",
                "model": "fake",
                "target_language": "zh-Hans",
                "max_output_tokens": 10_400,
                **extraction_overrides,
            },
        ),
        StubChecker(),
    )
    store = RunStore(prepared.work_dir)
    prep = store.read_preparation()
    ids = (*prep.reading_order, *(item for item in prep.document_hashes if item not in prep.reading_order))
    plan = plan_term_extraction(
        tuple(store.read_document(item) for item in ids),
        prep.user_terms,
        source_hash=prep.source_hash,
        preparation_hash=prepared.preparation_hash,
        max_primary_chars=100,
        reading_edges=tuple(zip(prep.reading_order, prep.reading_order[1:], strict=False)),
        extraction_identity=prep.extraction_config,
    ).plan
    store.write_term_plan(plan)
    return store, tuple(item.item_id for item in plan.items)


def test_failed_term_window_does_not_stop_later_windows_or_reset_on_resume(tmp_path: Path) -> None:
    store, item_ids = _prepare(tmp_path)
    assert len(item_ids) > 1
    calls: list[tuple[str, ...]] = []

    async def transport(kind, payload):
        assert kind == "terms"
        assert payload["target_language"] == "zh-Hans"
        assert len(list((store.root / "glossary" / "extraction").glob("*.json"))) == len(item_ids)
        requested = tuple(item["item_id"] for item in payload["items"])
        calls.append(requested)
        raw = json.dumps(
            {
                "protocol": "epubox-terms-1",
                "request_id": payload["request_id"],
                "items": [
                    ({"item_id": item_id} if item_id == item_ids[0] else {"item_id": item_id, "candidates": []})
                    for item_id in requested
                ],
            }
        )
        return {"raw": raw, "usage": {"input_tokens": 1, "output_tokens": 1}}

    first = asyncio.run(TermRunner(store, transport=transport).run())
    assert first.status == "closed_with_gaps"
    assert first.failed == 1
    assert first.succeeded == len(item_ids) - 1
    assert sum(item_ids[0] in call for call in calls) == 2
    assert calls[0] == item_ids
    assert calls[-1] == (item_ids[0],)
    assert store.read_extraction(item_ids[-1]).status == "succeeded"
    spent = first.http_attempts

    resumed = asyncio.run(TermRunner(store, transport=transport).run())
    assert resumed == first
    assert len(calls) == spent


@pytest.mark.parametrize("first_failure", ("schema", "source_evidence"))
def test_all_rejected_candidates_trigger_feedback_retry_and_save_valid_evidence(
    tmp_path: Path, first_failure: str
) -> None:
    store, item_ids = _prepare(tmp_path)
    target_item = item_ids[0]
    calls: Counter[str] = Counter()
    feedback_seen = False

    async def transport(_kind, payload):
        nonlocal feedback_seen
        response_items = []
        for item in payload["items"]:
            item_id = item["item_id"]
            calls[item_id] += 1
            candidates = []
            if item_id == target_item:
                view_id, view_text = next(iter(item["views"].items()))
                source = view_text.split()[0]
                feedback_seen = calls[item_id] == 2 and bool(item.get("retry_feedback"))
                candidates = [
                    {
                        "source": source,
                        "target": "术语",
                        "category": "term",
                        "aliases": [],
                        "scope_hint": "document",
                        "note": "",
                        "evidence": (
                            [] if first_failure == "schema" else [{"view_id": "sv-unknown", "source_quote": view_text}]
                        )
                        if calls[item_id] == 1
                        else [{"view_id": view_id, "source_quote": view_text}],
                    }
                ]
            response_items.append({"item_id": item_id, "candidates": candidates})
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": response_items,
                }
            ),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = asyncio.run(TermRunner(store, transport=transport).run())
    record = store.read_extraction(target_item)

    assert result.status == "closed"
    assert calls[target_item] == 2 and feedback_seen
    assert record.status == "succeeded_with_rejections" and len(record.candidates) == 1
    assert record.candidates[0].status == "proposed"
    assert any(
        entry.get("code") == f"rejected_{first_failure if first_failure == 'schema' else 'evidence'}"
        for entry in record.diagnostics
    )
    assert all(entry.get("request_id") in record.request_ids for entry in record.diagnostics)


def test_saved_term_response_replays_after_record_write_crash_without_new_http(tmp_path: Path, monkeypatch) -> None:
    store, item_ids = _prepare(tmp_path)
    items = {item.item_id: item for item in store.read_term_plan().items}
    for item_id in item_ids[1:]:
        item = items[item_id]
        store.save_extraction(
            TermExtractionRecord(
                item_id=item_id,
                document_id=item.document_id,
                view_ids=item.view_ids,
                extraction_input_hash=item.extraction_input_hash,
                status="succeeded",
            )
        )
    calls = 0

    async def transport(_kind, payload):
        nonlocal calls
        calls += 1
        item_id = payload["items"][0]["item_id"]
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": item_id, "candidates": []}],
                }
            ),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    runner = TermRunner(store, transport=transport)
    monkeypatch.setattr(
        runner,
        "_accept_response",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("crash after response journal")),
    )
    with pytest.raises(RuntimeError, match="crash after response journal"):
        asyncio.run(runner.run())
    record = store.read_extraction(item_ids[0])
    request_id = record.request_ids[0]
    attempt = store.read_request(request_id).attempts[0]
    assert record.status == "in_flight" and attempt.state == "succeeded"
    assert store.read_term_response(request_id, attempt.attempt_id) is not None

    async def forbidden(*_args):
        raise AssertionError("journal replay must not call the provider")

    resumed = asyncio.run(TermRunner(store, transport=forbidden).run())
    assert resumed.status == "closed"
    assert store.read_extraction(item_ids[0]).status == "succeeded"
    assert calls == 1 and resumed.http_attempts == 1


def test_journal_replay_finishes_a_sent_attempt_before_freeze(tmp_path: Path, monkeypatch) -> None:
    store, item_ids = _prepare(tmp_path)
    items = {item.item_id: item for item in store.read_term_plan().items}
    for item_id in item_ids[1:]:
        item = items[item_id]
        store.save_extraction(
            TermExtractionRecord(
                item_id=item_id,
                document_id=item.document_id,
                view_ids=item.view_ids,
                extraction_input_hash=item.extraction_input_hash,
                status="succeeded",
            )
        )

    async def transport(_kind, payload):
        item_id = payload["items"][0]["item_id"]
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": item_id, "candidates": []}],
                }
            ),
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "metadata": {"response_id": "term-replay-response"},
        }

    real_finish = store.finish_attempt

    def interrupted_finish(*args, **kwargs):
        if kwargs["state"] == "succeeded":
            raise RuntimeError("crash before succeeded attempt was committed")
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(store, "finish_attempt", interrupted_finish)
    with pytest.raises(RuntimeError, match="crash before succeeded"):
        asyncio.run(TermRunner(store, transport=transport).run())
    record = store.read_extraction(item_ids[0])
    request_id = record.request_ids[0]
    attempt = store.read_request(request_id).attempts[0]
    assert attempt.state == "sent" and store.read_term_response(request_id, attempt.attempt_id) is not None

    async def forbidden(*_args):
        raise AssertionError("journal replay must not call the provider")

    recovered = asyncio.run(resume_preparation(store.root, StubChecker(), term_transport=forbidden))
    assert recovered.status == "ready"
    recovered_attempt = store.read_request(request_id).attempts[0]
    assert recovered_attempt.state == "succeeded"
    assert recovered_attempt.usage is not None and recovered_attempt.usage.input_tokens == 1
    assert recovered_attempt.metadata.get("response_id") == "term-replay-response"
    assert store.read_bookplan().required_unit_count > 0


def test_reserved_attempt_occupies_budget_without_claiming_an_http_call(tmp_path: Path) -> None:
    store, item_ids = _prepare(tmp_path)
    item = store.read_term_plan().items[0]
    store.save_extraction(
        TermExtractionRecord(
            item_id=item.item_id,
            document_id=item.document_id,
            view_ids=item.view_ids,
            extraction_input_hash=item.extraction_input_hash,
        )
    )
    store.write_request(
        RequestManifest(
            request_id="reserved-only",
            stage="terms",
            owner_kind="extraction_item",
            owner_id=item.item_id,
            item_ids=(item.item_id,),
            input_hashes={item.item_id: item.extraction_input_hash},
            wire_hash="planned-wire",
        )
    )
    store.reserve_attempt(
        "reserved-only",
        Attempt(attempt_id="a1", affected_items=(item.item_id,), created_at="2026-09-29T00:00:00Z"),
    )
    runner = TermRunner(store, transport=lambda *_: None)
    assert runner._spent(item_ids[0]) == 1
    assert runner._spent(item_ids[0], actual=True) == 0
    assert runner._logical_calls(item_ids[0]) == 0


def test_unplannable_term_windows_do_not_enter_an_unbounded_retry_loop(tmp_path: Path) -> None:
    store, item_ids = _prepare(tmp_path, tpm=1)

    async def unexpected_transport(*_):
        raise AssertionError("unplannable input must never be sent")

    result = asyncio.run(TermRunner(store, transport=unexpected_transport).run())
    assert result.status == "closed_with_gaps"
    assert result.failed == len(item_ids)
    assert result.http_attempts == 0


def test_term_payload_uses_compact_view_maps_and_independent_50k_limit(tmp_path: Path) -> None:
    store, _item_ids = _prepare(tmp_path, context_tokens=1_000, max_output_tokens=900)
    runner = TermRunner(store, transport=lambda *_: None)
    item = runner.plan.items[0]
    payload = runner._payload((item,), "request-1")
    wire_item = payload["items"][0]

    assert runner._input_limit() == 50_000
    assert isinstance(wire_item["views"], dict) and wire_item["views"]
    assert isinstance(wire_item["context"], dict)
    assert all(isinstance(slices, list) for slices in wire_item["context"].values())
    assert set(wire_item["views"]) == set(item.view_ids)
    assert all(isinstance(view_id, str) and isinstance(text, str) for view_id, text in wire_item["views"].items())
    assert "unit_id" not in json.dumps(wire_item, ensure_ascii=False)


def test_term_payload_preserves_repeated_context_slices_for_one_view(tmp_path: Path) -> None:
    store, _item_ids = _prepare(tmp_path)
    runner = TermRunner(store, transport=lambda *_: None)
    item = runner.plan.items[0]
    view_id = item.view_ids[0]
    text = runner.documents[item.document_id].source_views[view_id].text
    item = item.model_copy(
        update={
            "context_ranges": (
                {"view_id": view_id, "start": 0, "end": 3},
                {"view_id": view_id, "start": 3, "end": 7},
            )
        }
    )

    payload = runner._payload((item,), "request-1")

    assert payload["items"][0]["context"][view_id] == [text[:3], text[3:7]]


def test_term_items_share_one_budgeted_request_and_charge_each_item(tmp_path: Path) -> None:
    store, item_ids = _prepare(tmp_path)
    calls = 0

    async def transport(_kind, payload):
        nonlocal calls
        calls += 1
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": item["item_id"], "candidates": []} for item in payload["items"]],
                }
            ),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = asyncio.run(TermRunner(store, transport=transport).run())
    request = next(
        request for request in TermRunner(store, transport=transport)._requests() if request.stage == "terms"
    )

    assert result.status == "closed" and result.http_attempts == 1 and calls == 1
    assert request.item_ids == item_ids
    assert request.attempts[0].affected_items == item_ids
    assert all(store.read_extraction(item_id).request_ids == (request.request_id,) for item_id in item_ids)
    assert all(store.read_extraction(item_id).counters["http_attempts"] == 1 for item_id in item_ids)


def test_term_batches_reserve_output_for_at_most_three_items(tmp_path: Path) -> None:
    store, item_ids = _prepare(tmp_path, max_output_tokens=4_096)
    batches: list[tuple[str, ...]] = []

    async def transport(_kind, payload):
        requested = tuple(item["item_id"] for item in payload["items"])
        batches.append(requested)
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": item_id, "candidates": []} for item_id in requested],
                }
            ),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = asyncio.run(TermRunner(store, transport=transport).run())

    assert result.status == "closed"
    assert all(1 <= len(batch) <= 3 for batch in batches)
    assert tuple(item_id for batch in batches for item_id in batch) == item_ids


def test_oversized_term_item_is_local_and_later_items_continue(tmp_path: Path, monkeypatch) -> None:
    store, item_ids = _prepare(tmp_path)

    def selective_budget(kind, payload):
        if payload["items"][0]["item_id"] == item_ids[0]:
            return runtime_input_budget(kind, payload) | {"estimated_input_tokens": 50_001}
        return runtime_input_budget(kind, payload)

    monkeypatch.setattr("engine.services.term_runner.model_input_budget", selective_budget)
    sent: list[tuple[str, ...]] = []

    async def transport(_kind, payload):
        requested = tuple(item["item_id"] for item in payload["items"])
        sent.append(requested)
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": item_id, "candidates": []} for item_id in requested],
                }
            ),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = asyncio.run(TermRunner(store, transport=transport).run())

    assert result.status == "closed_with_gaps" and result.failed == 1
    assert store.read_extraction(item_ids[0]).status == "unplannable"
    assert sent == [item_ids[1:]]
    assert all(item_ids[0] not in request.item_ids for request in TermRunner(store, transport=transport)._requests())


def test_truncated_multi_item_response_retries_smaller_batches(tmp_path: Path) -> None:
    store, item_ids = _prepare(tmp_path)
    batches: list[tuple[str, ...]] = []

    async def transport(_kind, payload):
        requested = tuple(item["item_id"] for item in payload["items"])
        assert requested
        batches.append(requested)
        if len(batches) == 1:
            return {
                "raw": "",
                "finish_reason": "length",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": requested_id, "candidates": []} for requested_id in requested],
                }
            ),
            "finish_reason": "stop",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = asyncio.run(TermRunner(store, transport=transport).run())

    assert result.status == "closed"
    assert batches[0] == item_ids
    midpoint = (len(item_ids) + 1) // 2
    assert batches[1:] == [item_ids[:midpoint], item_ids[midpoint:]]
    assert result.http_attempts == 3


def test_partial_multi_item_response_retries_only_missing_items(tmp_path: Path) -> None:
    store, item_ids = _prepare(tmp_path)
    batches: list[tuple[str, ...]] = []

    async def transport(_kind, payload):
        requested = tuple(item["item_id"] for item in payload["items"])
        batches.append(requested)
        returned = requested[:1] if len(requested) > 1 else requested
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": item_id, "candidates": []} for item_id in returned],
                }
            ),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = asyncio.run(TermRunner(store, transport=transport).run())

    assert result.status == "closed"
    assert batches[0] == item_ids
    assert batches[1:] == [(item_id,) for item_id in item_ids[1:]]
    assert sum(item_ids[0] in batch for batch in batches) == 1


def test_truncated_batch_is_retried_by_binary_halves(tmp_path: Path) -> None:
    store, item_ids = _prepare(tmp_path)
    items = {item.item_id: item for item in store.read_term_plan().items}
    active = item_ids[:4]
    for item_id in item_ids[4:]:
        item = items[item_id]
        store.save_extraction(
            TermExtractionRecord(
                item_id=item_id,
                document_id=item.document_id,
                view_ids=item.view_ids,
                extraction_input_hash=item.extraction_input_hash,
                status="succeeded",
            )
        )
    batches: list[tuple[str, ...]] = []

    async def transport(_kind, payload):
        requested = tuple(item["item_id"] for item in payload["items"])
        batches.append(requested)
        if len(requested) > 2:
            return {
                "raw": "",
                "finish_reason": "length",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": item_id, "candidates": []} for item_id in requested],
                }
            ),
            "finish_reason": "stop",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = asyncio.run(TermRunner(store, transport=transport).run())

    assert result.status == "closed"
    assert batches == [active, active[:2], active[2:]]


def test_journaled_input_over_limit_pauses_before_fresh_http(tmp_path: Path, monkeypatch) -> None:
    store, item_ids = _prepare(tmp_path)
    items = {item.item_id: item for item in store.read_term_plan().items}
    for item_id in item_ids[1:]:
        item = items[item_id]
        store.save_extraction(
            TermExtractionRecord(
                item_id=item_id,
                document_id=item.document_id,
                view_ids=item.view_ids,
                extraction_input_hash=item.extraction_input_hash,
                status="succeeded",
            )
        )

    async def transport(_kind, payload):
        item_id = payload["items"][0]["item_id"]
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": item_id, "candidates": []}],
                }
            ),
            "finish_reason": "length",
            "usage": {"input_tokens": 50_001, "output_tokens": 1},
        }

    real_finish = store.finish_attempt

    def interrupted_finish(*args, **kwargs):
        if kwargs["state"] == "succeeded":
            raise RuntimeError("crash before over-limit usage was committed")
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(store, "finish_attempt", interrupted_finish)
    with pytest.raises(RuntimeError, match="over-limit usage"):
        asyncio.run(TermRunner(store, transport=transport).run())

    monkeypatch.setattr(store, "finish_attempt", real_finish)
    request_count = len(tuple((store.root / "requests").glob("*.json")))

    async def forbidden(*_args):
        raise AssertionError("journaled over-limit usage must pause before a fresh HTTP call")

    result = asyncio.run(TermRunner(store, transport=forbidden).run())

    assert result.status == "paused"
    assert len(tuple((store.root / "requests").glob("*.json"))) == request_count
    request = store.read_request(store.read_extraction(item_ids[0]).request_ids[0])
    assert request.attempts[0].state == "succeeded"
    assert request.attempts[0].usage is not None and request.attempts[0].usage.input_tokens == 50_001


def test_replayed_truncated_batch_keeps_binary_split(tmp_path: Path, monkeypatch) -> None:
    store, item_ids = _prepare(tmp_path)
    items = {item.item_id: item for item in store.read_term_plan().items}
    active = item_ids[:4]
    for item_id in item_ids[4:]:
        item = items[item_id]
        store.save_extraction(
            TermExtractionRecord(
                item_id=item_id,
                document_id=item.document_id,
                view_ids=item.view_ids,
                extraction_input_hash=item.extraction_input_hash,
                status="succeeded",
            )
        )
    batches: list[tuple[str, ...]] = []

    async def truncated(_kind, payload):
        requested = tuple(item["item_id"] for item in payload["items"])
        batches.append(requested)
        return {
            "raw": "",
            "finish_reason": "length",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    runner = TermRunner(store, transport=truncated)
    monkeypatch.setattr(
        runner,
        "_mark_batch_error",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crash before record update")),
    )
    with pytest.raises(RuntimeError, match="before record update"):
        asyncio.run(runner.run())

    async def recovered(_kind, payload):
        requested = tuple(item["item_id"] for item in payload["items"])
        batches.append(requested)
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": item_id, "candidates": []} for item_id in requested],
                }
            ),
            "finish_reason": "stop",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = asyncio.run(TermRunner(store, transport=recovered).run())

    assert result.status == "closed"
    assert batches == [active, active[:2], active[2:]]
