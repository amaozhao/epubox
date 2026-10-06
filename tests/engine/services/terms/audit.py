from __future__ import annotations

import asyncio
import json

import pytest

from engine.schemas.contracts import canonical_hash
from engine.services.report import write_report
from engine.services.terms.freeze import freeze_terminology, prepare_candidate_pool
from engine.services.terms.runner import TermRunner
from tests.engine.services.terms.runner import _prepare


def test_exhausted_evidence_rejections_remain_auditable(tmp_path) -> None:
    store, item_ids = _prepare(tmp_path)
    target_item = item_ids[0]
    calls = 0

    async def transport(_kind, payload):
        nonlocal calls
        response_items = []
        for item in payload["items"]:
            calls += item["item_id"] == target_item
            _view_id, view_text = next(iter(item["views"].items()))
            candidates = (
                [
                    {
                        "source": view_text.split()[0],
                        "target": "术语",
                        "category": "term",
                        "evidence": [{"view_id": "sv-unknown", "source_quote": view_text}],
                    }
                ]
                if item["item_id"] == target_item
                else []
            )
            response_items.append({"item_id": item["item_id"], "candidates": candidates})
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

    assert result.status == "closed_with_gaps" and calls == 2
    assert record.status == "failed_exhausted"
    assert len(record.candidates) == 1 and record.candidates[0].status == "rejected_evidence"
    assert len(record.request_ids) == 2
    assert all(diagnostic.get("request_id") in record.request_ids for diagnostic in record.diagnostics)
    assert all(
        store.read_term_response(request_id, store.read_request(request_id).attempts[0].attempt_id) is not None
        for request_id in record.request_ids
    )


@pytest.mark.parametrize("retry_empty", (True, False))
def test_schema_rejection_survives_retry_freeze_and_report(tmp_path, retry_empty: bool) -> None:
    store, item_ids = _prepare(tmp_path)
    target_item = item_ids[0]
    calls = 0

    async def transport(_kind, payload):
        nonlocal calls
        response_items = []
        for item in payload["items"]:
            candidates = []
            if item["item_id"] == target_item and (calls == 0 or not retry_empty):
                view_id, view_text = next(iter(item["views"].items()))
                candidates = [
                    {
                        "source": "Memory",
                        "target": "内存",
                        "category": "term",
                        "evidence": [{"view_id": view_id, "source_quote": view_text}],
                        "unexpected": True,
                    }
                ]
            if item["item_id"] == target_item:
                calls += 1
            response_items.append({"item_id": item["item_id"], "candidates": candidates})
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

    runner = TermRunner(store, transport=transport)
    result = asyncio.run(runner.run())
    record = store.read_extraction(target_item)
    preparation = store.read_preparation()
    records = {item.item_id: store.read_extraction(item.item_id) for item in runner.plan.items}
    pool = prepare_candidate_pool(
        runner.plan,
        records,
        preparation.user_terms,
        preparation.unit_documents,
        tuple(runner.documents.values()),
    )
    store.save_candidate_pool(pool)
    frozen = freeze_terminology(
        runner.plan,
        records,
        preparation.user_terms,
        preparation.unit_documents,
        tuple(runner.documents.values()),
        extraction_config_hash=canonical_hash(preparation.extraction_config),
    )
    report = json.loads(write_report(store, status="paused", phase="terms").read_text())

    assert calls == 2
    assert result.status == ("closed" if retry_empty else "closed_with_gaps")
    assert record.status == ("succeeded_with_rejections" if retry_empty else "failed_exhausted")
    assert len(record.rejections) == len(pool.rejections) == (1 if retry_empty else 2)
    rejection = pool.rejections[0]
    assert (rejection.candidate_index, rejection.source, rejection.target, rejection.category) == (
        0,
        "Memory",
        "内存",
        "term",
    )
    assert {entry.request_id for entry in pool.rejections} == set(record.request_ids[: len(pool.rejections)])
    assert report["terminology"]["rejected_candidate_count"] == len(pool.rejections)
    assert report["terminology"]["succeeded_with_rejections_windows"] == int(retry_empty)
    assert frozen.freeze_intent.coverage["candidates_rejected_schema"] == len(pool.rejections)
    assert frozen.freeze_intent.coverage["candidates_rejected"] == len(pool.rejections)
    assert any(f"{len(pool.rejections)} terminology candidate" in warning for warning in frozen.glossary.warnings)
