from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest

from engine.epub.preparation import PreparationConfig, prepare_book
from engine.schemas.contracts import Attempt, RequestManifest, TermExtractionRecord
from engine.services.preparation_pipeline import resume_preparation
from engine.services.store import RunStore
from engine.services.term_planning import TERM_PLANNER_VERSION, plan_term_extraction
from engine.services.term_runner import TermRunner
from tests.engine.epub.book_factory import make_epub
from tests.engine.epub.test_preparation import StubChecker


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
    calls: list[str] = []

    async def transport(kind, payload):
        assert kind == "terms"
        assert payload["target_language"] == "zh-Hans"
        assert len(list((store.root / "glossary" / "extraction").glob("*.json"))) == len(item_ids)
        item_id = payload["items"][0]["item_id"]
        calls.append(item_id)
        if item_id == item_ids[0]:
            return {"raw": "not-json", "usage": {"input_tokens": 1, "output_tokens": 1}}
        raw = json.dumps(
            {
                "protocol": "epubox-terms-1",
                "request_id": payload["request_id"],
                "items": [{"item_id": item_id, "candidates": []}],
            }
        )
        return {"raw": raw, "usage": {"input_tokens": 1, "output_tokens": 1}}

    first = asyncio.run(TermRunner(store, transport=transport).run())
    assert first.status == "closed_with_gaps"
    assert first.failed == 1
    assert first.succeeded == len(item_ids) - 1
    assert calls.count(item_ids[0]) == 2
    assert calls[-1] == item_ids[-1]
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
        item = payload["items"][0]
        item_id = item["item_id"]
        calls[item_id] += 1
        candidates = []
        if item_id == target_item:
            view = item["views"][0]
            source = view["text"].split()[0]
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
                        [] if first_failure == "schema" else [{"view_id": "sv-unknown", "source_quote": view["text"]}]
                    )
                    if calls[item_id] == 1
                    else [{"view_id": view["view_id"], "source_quote": view["text"]}],
                }
            ]
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": item_id, "candidates": candidates}],
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
