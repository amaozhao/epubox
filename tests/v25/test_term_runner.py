from __future__ import annotations

import asyncio
import json
from pathlib import Path

from engine.epub.preparation_v25 import PreparationConfig, prepare_book
from engine.schemas.v25 import Attempt, RequestManifest, TermExtractionRecord
from engine.services.store_v25 import StoreV25
from engine.services.term_planning import TERM_PLANNER_VERSION, plan_term_extraction
from engine.services.term_runner import TermRunner
from tests.v23.book_factory import make_epub
from tests.v25.test_preparation_v25 import StubChecker


def _prepare(tmp_path: Path, **extraction_overrides: int) -> tuple[StoreV25, tuple[str, ...]]:
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
                "prompt_version": "epubox-v25-1",
                "model": "fake",
                "target_language": "zh-Hans",
                **extraction_overrides,
            },
        ),
        StubChecker(),
    )
    store = StoreV25(prepared.work_dir)
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


def test_unplannable_term_windows_do_not_enter_an_unbounded_retry_loop(tmp_path: Path) -> None:
    store, item_ids = _prepare(tmp_path, tpm=1)

    async def unexpected_transport(*_):
        raise AssertionError("unplannable input must never be sent")

    result = asyncio.run(TermRunner(store, transport=unexpected_transport).run())
    assert result.status == "closed_with_gaps"
    assert result.failed == len(item_ids)
    assert result.http_attempts == 0
