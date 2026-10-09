from __future__ import annotations

import asyncio
import json
from pathlib import Path

from engine.agents.runtime import wire_hash
from engine.services.terms.resolution import TermResolutionRunner
from engine.services.terms.runner import TermRunner
from tests.engine.services.terms.resolution import _add_conflict, _seed_conflict
from tests.engine.services.terms.runner import _prepare


def test_terms_ignore_legacy_output_cap_and_accept_large_complete_usage(tmp_path: Path) -> None:
    store, item_ids = _prepare(tmp_path, max_output_tokens=1)
    requested: list[tuple[str, ...]] = []
    payloads: list[dict] = []

    async def transport(_kind, payload):
        payloads.append(payload)
        ids = tuple(item["item_id"] for item in payload["items"])
        requested.append(ids)
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": item_id, "candidates": []} for item_id in ids],
                }
            ),
            "usage": {"input_tokens": 10, "output_tokens": 9_001},
            "finish_reason": "stop",
        }

    result = asyncio.run(TermRunner(store, transport=transport).run())

    assert result.status == "closed"
    assert requested == [item_ids]
    request = next(request for request in TermRunner(store, transport=transport)._requests())
    assert request.output_unlimited is True
    assert request.wire_hash == wire_hash("terms", payloads[0], None)


def test_output_estimate_above_tpm_does_not_shrink_or_block_terms(tmp_path: Path) -> None:
    store, item_ids = _prepare(tmp_path, max_output_tokens=10_000, tpm=3_000)
    requested: list[tuple[str, ...]] = []

    async def transport(_kind, payload):
        ids = tuple(item["item_id"] for item in payload["items"])
        requested.append(ids)
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": item_id, "candidates": []} for item_id in ids],
                }
            ),
            "usage": {"input_tokens": 2_500, "output_tokens": 12_000},
            "finish_reason": "stop",
        }

    runner = TermRunner(store, transport=transport)
    assert runner.output_tokens > runner._input_limit()

    result = asyncio.run(runner.run())

    assert result.status == "closed"
    assert requested == [item_ids]


def test_resolution_ignores_legacy_output_cap_when_packing(tmp_path: Path) -> None:
    store, pool = _seed_conflict(tmp_path, max_output_tokens=1)
    groups = _add_conflict(store, pool).conflict_groups
    requested: list[tuple[str, ...]] = []

    async def transport(_kind, payload):
        ids = tuple(item["group_id"] for item in payload["items"])
        requested.append(ids)
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-term-resolution-2",
                    "request_id": payload["request_id"],
                    "items": [
                        {
                            "group_id": group_id,
                            "decision": "defer",
                            "selected_candidate_ids": [],
                            "reason": "No preference.",
                        }
                        for group_id in ids
                    ],
                }
            ),
            "usage": {"input_tokens": 10, "output_tokens": 9_001},
            "finish_reason": "stop",
        }

    result = asyncio.run(TermResolutionRunner(store, transport=transport).run())

    assert result.status == "closed"
    assert len(requested) == 1
    assert set(requested[0]) == {str(group["group_id"]) for group in groups}
    requests = TermRunner(store, transport=transport)._requests()
    assert len(requests) == 1
    assert requests[0].output_unlimited is True
