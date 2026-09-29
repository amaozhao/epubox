from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from engine.agents.runtime import ProviderError
from engine.schemas.contracts import JsonValue, TermExtractionRecord
from engine.services.term_candidates import CandidateProposal, EvidenceProposal, validate_candidate_proposals
from engine.services.term_freeze import prepare_candidate_pool
from engine.services.term_resolution import TermResolutionRunner
from tests.engine.services.test_term_runner import _prepare


def _seed_conflict(tmp_path: Path, **extraction_overrides: int):
    store, _ = _prepare(tmp_path, **extraction_overrides)
    plan = store.read_term_plan()
    prep = store.read_preparation()
    documents = tuple(store.read_document(document_id) for document_id in prep.document_hashes)
    records = {}
    seeded = False
    for item in plan.items:
        document = next(document for document in documents if document.document_id == item.document_id)
        proposals = ()
        for view_id in item.view_ids:
            view = document.source_views[view_id]
            if not seeded and "Memory" in view.text:
                proposals = (
                    CandidateProposal(
                        source="Memory",
                        target="内存",
                        category="term",
                        evidence=(EvidenceProposal(view_id, view.text),),
                    ),
                    CandidateProposal(
                        source="Memory",
                        target="记忆",
                        category="term",
                        evidence=(EvidenceProposal(view_id, view.text),),
                    ),
                )
                seeded = True
                break
        candidates = validate_candidate_proposals(document, item, proposals).candidates
        record = TermExtractionRecord(
            item_id=item.item_id,
            document_id=item.document_id,
            view_ids=item.view_ids,
            extraction_input_hash=item.extraction_input_hash,
            status="succeeded",
            candidates=candidates,
        )
        records[item.item_id] = store.save_extraction(record)
    pool = prepare_candidate_pool(plan, records, (), prep.unit_documents, documents)
    store.save_candidate_pool(pool)
    assert len(pool.conflict_groups) >= 1
    return store, pool


def _add_conflict(store, pool, suffix: str = "second"):
    first = pool.conflict_groups[0]
    extra = first | {"group_id": f"{first['group_id']}-{suffix}", "group_input_hash": f"hash-{suffix}"}
    return store.save_candidate_pool(
        pool.model_copy(update={"record_version": 1, "conflict_groups": (*pool.conflict_groups, extra)}),
        expected_record_version=0,
    )


def test_one_bounded_resolution_selects_only_existing_candidate(tmp_path: Path) -> None:
    store, _ = _seed_conflict(tmp_path)
    calls: list[str] = []

    async def transport(kind, payload):
        assert kind == "resolution"
        group = payload["items"][0]
        calls.append(group["group_id"])
        chosen = next(candidate["candidate_id"] for candidate in group["candidates"] if candidate["target"] == "内存")
        raw = json.dumps(
            {
                "protocol": "epubox-term-resolution-2",
                "request_id": payload["request_id"],
                "items": [
                    {
                        "group_id": group["group_id"],
                        "decision": "select",
                        "selected_candidate_ids": [chosen],
                        "reason": "The technical sense matches the source.",
                    }
                ],
            }
        )
        return {"raw": raw, "usage": {"input_tokens": 1, "output_tokens": 1}}

    result = asyncio.run(TermResolutionRunner(store, transport=transport).run())
    assert result.status == "closed"
    assert result.selected == len(calls) == 1
    assert store.read_candidate_pool().record_version == 1
    assert TermResolutionRunner(store, transport=transport).decisions()[0].decision == "select"


def test_v2_batches_groups_and_retries_only_a_missing_group(tmp_path: Path) -> None:
    store, pool = _seed_conflict(tmp_path)
    pool = _add_conflict(store, pool)
    batch_sizes: list[int] = []

    async def transport(_kind, payload):
        batch_sizes.append(len(payload["items"]))
        requested = payload["items"] if len(batch_sizes) > 1 else payload["items"][:1]
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-term-resolution-2",
                    "request_id": payload["request_id"],
                    "items": [
                        {
                            "group_id": group["group_id"],
                            "decision": "select",
                            "selected_candidate_ids": [group["candidates"][0]["candidate_id"]],
                            "reason": "Supported by the supplied evidence.",
                        }
                        for group in requested
                    ],
                }
            ),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = asyncio.run(TermResolutionRunner(store, transport=transport).run())

    assert result.status == "closed"
    assert result.selected == len(pool.conflict_groups) == 2
    assert batch_sizes == [2, 1]


def test_v2_splits_a_truncated_batch_before_retrying(tmp_path: Path) -> None:
    store, pool = _seed_conflict(tmp_path)
    _add_conflict(store, pool)
    batch_sizes: list[int] = []

    async def transport(_kind, payload):
        batch_sizes.append(len(payload["items"]))
        if len(payload["items"]) > 1:
            return {"raw": "{}", "finish_reason": "length", "usage": {"input_tokens": 1, "output_tokens": 1}}
        group = payload["items"][0]
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-term-resolution-2",
                    "request_id": payload["request_id"],
                    "items": [
                        {
                            "group_id": group["group_id"],
                            "decision": "select",
                            "selected_candidate_ids": [group["candidates"][0]["candidate_id"]],
                            "reason": "Supported by the supplied evidence.",
                        }
                    ],
                }
            ),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = asyncio.run(TermResolutionRunner(store, transport=transport).run())

    assert result.status == "closed"
    assert result.selected == 2
    assert batch_sizes == [2, 1, 1]


def test_v2_replays_truncation_as_split_batches_after_restart(tmp_path: Path) -> None:
    store, pool = _seed_conflict(tmp_path)
    _add_conflict(store, pool)

    async def truncated(_kind, _payload):
        return {"raw": "{}", "finish_reason": "length", "usage": {"input_tokens": 1, "output_tokens": 1}}

    interrupted = TermResolutionRunner(store, transport=truncated)
    invoke = interrupted.term_runner.runtime.invoke

    async def crash_after_journal(*args, **kwargs):
        await invoke(*args, **kwargs)
        raise KeyboardInterrupt

    interrupted.term_runner.runtime.invoke = crash_after_journal  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(interrupted.run())

    batch_sizes: list[int] = []

    async def transport(_kind, payload):
        batch_sizes.append(len(payload["items"]))
        group = payload["items"][0]
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-term-resolution-2",
                    "request_id": payload["request_id"],
                    "items": [
                        {
                            "group_id": group["group_id"],
                            "decision": "select",
                            "selected_candidate_ids": [group["candidates"][0]["candidate_id"]],
                            "reason": "Supported by the supplied evidence.",
                        }
                    ],
                }
            ),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = asyncio.run(TermResolutionRunner(store, transport=transport).run())

    assert result.status == "closed"
    assert result.selected == 2
    assert batch_sizes == [1, 1]


def test_v2_defers_an_oversize_group_before_writing_a_manifest(tmp_path: Path) -> None:
    store, pool = _seed_conflict(tmp_path)
    group = pool.conflict_groups[0] | {
        "allowed_unit_ids": ["u" + "x" * 50_000],
        "group_input_hash": "oversize",
    }
    store.save_candidate_pool(
        pool.model_copy(update={"record_version": 1, "conflict_groups": (group,)}),
        expected_record_version=0,
    )

    async def forbidden(*_args):
        raise AssertionError("oversize resolution group must not dispatch")

    result = asyncio.run(TermResolutionRunner(store, transport=forbidden).run())

    assert result.status == "closed"
    assert result.deferred == 1
    assert not tuple((store.root / "requests").glob("*.json"))


def test_v2_packs_to_50000_auxiliary_input_limit_before_manifest(tmp_path: Path) -> None:
    store, pool = _seed_conflict(tmp_path, context_tokens=8192, max_output_tokens=4096)
    pool = _add_conflict(store, pool)
    padded_groups: list[dict[str, JsonValue]] = []
    for index, group in enumerate(pool.conflict_groups):
        allowed = group["allowed_unit_ids"]
        assert isinstance(allowed, list)
        allowed_ids: list[JsonValue] = [*allowed, f"u-{index}-" + "x" * 24_000]
        padded_group: dict[str, JsonValue] = dict(group)
        padded_group["allowed_unit_ids"] = allowed_ids
        padded_groups.append(padded_group)
    padded = tuple(padded_groups)
    store.save_candidate_pool(
        pool.model_copy(update={"record_version": 2, "conflict_groups": padded}),
        expected_record_version=1,
    )
    batch_sizes: list[int] = []

    async def transport(_kind, payload):
        batch_sizes.append(len(payload["items"]))
        group = payload["items"][0]
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-term-resolution-2",
                    "request_id": payload["request_id"],
                    "items": [
                        {
                            "group_id": group["group_id"],
                            "decision": "select",
                            "selected_candidate_ids": [group["candidates"][0]["candidate_id"]],
                            "reason": "Supported by the supplied evidence.",
                        }
                    ],
                }
            ),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    runner = TermResolutionRunner(store, transport=transport)
    assert runner.term_runner._input_limit() == 50_000
    assert all(runner._budget_ok((group,)) for group in padded)
    assert not runner._budget_ok(padded)

    result = asyncio.run(runner.run())

    assert result.status == "closed"
    assert batch_sizes == [1, 1]
    assert all(len(request.item_ids) == 1 for request in runner.term_runner._requests())


def test_v2_splits_many_tiny_groups_for_minimum_output_envelope(tmp_path: Path) -> None:
    store, pool = _seed_conflict(tmp_path, max_output_tokens=256)
    first = pool.conflict_groups[0]
    groups = tuple(
        first | {"group_id": f"{first['group_id']}-{index}", "group_input_hash": f"hash-{index}"}
        for index in range(20)
    )
    store.save_candidate_pool(
        pool.model_copy(update={"record_version": 1, "conflict_groups": groups}),
        expected_record_version=0,
    )
    batch_sizes: list[int] = []

    async def transport(_kind, payload):
        batch_sizes.append(len(payload["items"]))
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-term-resolution-2",
                    "request_id": payload["request_id"],
                    "items": [
                        {
                            "group_id": group["group_id"],
                            "decision": "defer",
                            "selected_candidate_ids": [],
                            "reason": "x",
                        }
                        for group in payload["items"]
                    ],
                }
            ),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    runner = TermResolutionRunner(store, transport=transport)
    assert runner._minimal_response_tokens(groups) > runner.term_runner.output_tokens
    assert all(runner._minimal_response_tokens((group,)) <= runner.term_runner.output_tokens for group in groups)

    result = asyncio.run(runner.run())

    assert result.status == "closed"
    assert result.deferred == 20
    assert sum(batch_sizes) == 20
    assert 1 < max(batch_sizes) < 20
    by_id = {str(group["group_id"]): group for group in groups}
    assert all(
        runner._minimal_response_tokens(tuple(by_id[group_id] for group_id in request.item_ids))
        <= runner.term_runner.output_tokens
        for request in runner.term_runner._requests()
    )


def test_v1_defers_oversize_group_before_manifest_or_http(tmp_path: Path) -> None:
    store, pool = _seed_conflict(tmp_path)
    group = pool.conflict_groups[0] | {
        "allowed_unit_ids": ["u" + "x" * 50_000],
        "group_input_hash": "oversize-v1",
    }
    store.save_candidate_pool(
        pool.model_copy(update={"record_version": 1, "conflict_groups": (group,)}),
        expected_record_version=0,
    )

    async def forbidden(*_args):
        raise AssertionError("oversize v1 resolution group must not dispatch")

    runner = TermResolutionRunner(store, transport=forbidden)
    result = asyncio.run(runner._run_v1())

    assert result.status == "closed"
    assert result.deferred == 1
    assert not tuple((store.root / "requests").glob("*.json"))


def test_v1_replays_journaled_response_without_duplicate_http(tmp_path: Path) -> None:
    store, _ = _seed_conflict(tmp_path)

    async def transport(_kind, payload):
        chosen = next(
            candidate["candidate_id"] for candidate in payload["candidates"] if candidate["target"] == "内存"
        )
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-term-resolution-1",
                    "request_id": payload["request_id"],
                    "group_id": payload["group_id"],
                    "decision": "select",
                    "selected_candidate_ids": [chosen],
                    "reason": "Supported by the supplied evidence.",
                }
            ),
            "usage": {"input_tokens": 7, "output_tokens": 3},
        }

    interrupted = TermResolutionRunner(store, transport=transport)
    finish = interrupted.term_runner.runtime._finish_attempt

    def crash_after_journal(request_id, attempt_id, **fields):
        if fields.get("state") == "succeeded":
            raise KeyboardInterrupt
        assert finish is not None
        return finish(request_id, attempt_id, **fields)

    interrupted.term_runner.runtime._finish_attempt = crash_after_journal
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(interrupted._run_v1())

    async def forbidden(*_args):
        raise AssertionError("v1 journal replay must not call the provider")

    resumed = TermResolutionRunner(store, transport=forbidden)
    result = asyncio.run(resumed._run_v1())

    assert result.status == "closed"
    assert result.selected == 1
    request = resumed.term_runner._requests()[0]
    attempt = request.attempts[0]
    assert attempt.state == "succeeded"
    assert attempt.usage is not None
    assert (attempt.usage.input_tokens, attempt.usage.output_tokens) == (7, 3)
    assert store.read_model_response("resolution", request.request_id, attempt.attempt_id) is not None


def test_v1_retries_a_known_failed_attempt_after_provider_recovery(tmp_path: Path) -> None:
    store, _ = _seed_conflict(tmp_path)

    async def unavailable(*_args):
        raise ProviderError("credential unavailable", status_code=401)

    first = asyncio.run(TermResolutionRunner(store, transport=unavailable)._run_v1())
    assert first.status == "paused"
    failed_request = TermResolutionRunner(store, transport=unavailable).term_runner._requests()[0]
    assert failed_request.attempts[0].state == "failed"

    async def recovered(_kind, payload):
        chosen = next(
            candidate["candidate_id"] for candidate in payload["candidates"] if candidate["target"] == "内存"
        )
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-term-resolution-1",
                    "request_id": payload["request_id"],
                    "group_id": payload["group_id"],
                    "decision": "select",
                    "selected_candidate_ids": [chosen],
                    "reason": "Supported by the supplied evidence.",
                }
            ),
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }

    result = asyncio.run(TermResolutionRunner(store, transport=recovered)._run_v1())

    assert result.status == "closed"
    assert result.selected == 1
    assert result.http_attempts == 2
    requests = TermResolutionRunner(store, transport=recovered).term_runner._requests()
    assert len(requests) == 2
    assert sorted(attempt.state for request in requests for attempt in request.attempts) == ["failed", "succeeded"]


def test_v2_replays_a_journaled_response_without_another_http_call(tmp_path: Path) -> None:
    store, _ = _seed_conflict(tmp_path)

    async def transport(_kind, payload):
        group = payload["items"][0]
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-term-resolution-2",
                    "request_id": payload["request_id"],
                    "items": [
                        {
                            "group_id": group["group_id"],
                            "decision": "select",
                            "selected_candidate_ids": [group["candidates"][0]["candidate_id"]],
                            "reason": "Supported by the supplied evidence.",
                        }
                    ],
                }
            ),
            "usage": {"input_tokens": 7, "output_tokens": 3},
        }

    interrupted = TermResolutionRunner(store, transport=transport)
    interrupted._apply_batch_response = lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt())  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(interrupted.run())

    async def forbidden(*_args):
        raise AssertionError("journal replay must not call the provider")

    result = asyncio.run(TermResolutionRunner(store, transport=forbidden).run())

    assert result.status == "closed"
    assert result.selected == 1
