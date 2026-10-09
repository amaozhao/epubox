from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from engine.agents.runtime import ProviderError
from engine.schemas.contracts import Attempt, RequestManifest, TermExtractionRecord
from engine.services.terms.candidates import CandidateProposal, EvidenceProposal, validate_candidate_proposals
from engine.services.terms.freeze import prepare_candidate_pool
from engine.services.terms.resolution import TermResolutionRunner
from tests.engine.services.terms.runner import _prepare


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


def _add_conflict(store, pool):
    plan = store.read_term_plan()
    prep = store.read_preparation()
    documents = {document_id: store.read_document(document_id) for document_id in prep.document_hashes}
    occupied = {candidate.extraction_item_id for candidate in pool.candidates}
    item = next(
        item
        for item in plan.items
        if item.item_id not in occupied
        and any("Memory" in documents[item.document_id].source_views[view_id].text for view_id in item.view_ids)
    )
    document = documents[item.document_id]
    view = next(
        document.source_views[view_id] for view_id in item.view_ids if "Memory" in document.source_views[view_id].text
    )
    candidates = validate_candidate_proposals(
        document,
        item,
        (
            CandidateProposal("Memory", "内存", "term", (EvidenceProposal(view.view_id, view.text),)),
            CandidateProposal("Memory", "记忆", "term", (EvidenceProposal(view.view_id, view.text),)),
        ),
    ).candidates
    record = store.read_extraction(item.item_id)
    store.save_extraction(
        record.model_copy(update={"record_version": 1, "candidates": candidates}),
        expected_record_version=0,
    )
    records = {entry.item_id: store.read_extraction(entry.item_id) for entry in plan.items}
    canonical = prepare_candidate_pool(plan, records, prep.user_terms, prep.unit_documents, tuple(documents.values()))
    return store.save_candidate_pool(
        canonical.model_copy(update={"record_version": 1}),
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


def test_resolution_without_atomic_preflight_pauses_before_http(tmp_path: Path) -> None:
    store, _ = _seed_conflict(tmp_path)
    (store.root / "checks" / "preflight.json").unlink()

    async def forbidden(*_args):
        raise AssertionError("resolution must not dispatch without atomic preflight")

    result = asyncio.run(TermResolutionRunner(store, transport=forbidden).run())

    assert result.status == "paused"
    assert result.pending == 1 and result.http_attempts == 0
    assert result.reason is not None and "atomic preflight" in result.reason
    assert all(not request.attempts for request in TermResolutionRunner(store).term_runner._requests())


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


@pytest.mark.parametrize("finish_reason", ("length", "max_tokens"))
def test_v2_splits_a_truncated_batch_before_retrying(tmp_path: Path, finish_reason: str) -> None:
    store, pool = _seed_conflict(tmp_path)
    _add_conflict(store, pool)
    batch_sizes: list[int] = []

    async def transport(_kind, payload):
        batch_sizes.append(len(payload["items"]))
        if len(payload["items"]) > 1:
            return {"raw": "{}", "finish_reason": finish_reason, "usage": {"input_tokens": 1, "output_tokens": 1}}
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


@pytest.mark.parametrize("finish_reason", ("length", "max_tokens"))
def test_v2_replays_truncation_as_split_batches_after_restart(tmp_path: Path, finish_reason: str) -> None:
    store, pool = _seed_conflict(tmp_path)
    _add_conflict(store, pool)

    async def truncated(_kind, _payload):
        return {"raw": "{}", "finish_reason": finish_reason, "usage": {"input_tokens": 1, "output_tokens": 1}}

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


def test_v2_defers_an_oversize_input_group_before_writing_a_manifest(tmp_path: Path) -> None:
    store, _ = _seed_conflict(tmp_path, max_input_tokens=900, max_output_tokens=900)

    async def forbidden(*_args):
        raise AssertionError("oversize resolution group must not dispatch")

    result = asyncio.run(TermResolutionRunner(store, transport=forbidden).run())

    assert result.status == "closed"
    assert result.deferred == 1
    assert not tuple((store.root / "requests").glob("*.json"))


def test_v2_packs_to_frozen_input_limit_before_manifest(tmp_path: Path) -> None:
    store, pool = _seed_conflict(tmp_path, max_input_tokens=1_100, max_output_tokens=256)
    pool = _add_conflict(store, pool)
    groups = pool.conflict_groups
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
    assert runner.term_runner._input_limit() == 1_100
    assert all(runner._budget_ok((group,)) for group in groups)
    assert not runner._budget_ok(groups)

    result = asyncio.run(runner.run())

    assert result.status == "closed"
    assert batch_sizes == [1, 1]
    assert all(len(request.item_ids) == 1 for request in runner.term_runner._requests())


def test_v2_does_not_split_groups_for_a_legacy_output_cap(tmp_path: Path) -> None:
    store, pool = _seed_conflict(tmp_path, max_output_tokens=80)
    _add_conflict(store, pool)
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
    result = asyncio.run(runner.run())

    assert result.status == "closed"
    assert result.deferred == 2
    assert sum(batch_sizes) == 2
    assert batch_sizes == [2]
    assert len(runner.term_runner._requests()[0].item_ids) == 2


def test_v1_defers_oversize_input_group_before_manifest_or_http(tmp_path: Path) -> None:
    store, _ = _seed_conflict(tmp_path, max_input_tokens=1)

    async def forbidden(*_args):
        raise AssertionError("oversize v1 resolution group must not dispatch")

    runner = TermResolutionRunner(store, transport=forbidden)
    result = asyncio.run(runner._run_v1())

    assert result.status == "closed"
    assert result.deferred == 1
    assert not tuple((store.root / "requests").glob("*.json"))


def test_v1_dispatches_when_the_legacy_output_cap_is_tiny(tmp_path: Path) -> None:
    store, _ = _seed_conflict(tmp_path, max_output_tokens=1)

    async def transport(_kind, payload):
        group_id = payload["group_id"]
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-term-resolution-1",
                    "request_id": payload["request_id"],
                    "group_id": group_id,
                    "decision": "defer",
                    "selected_candidate_ids": [],
                    "reason": "No preference.",
                }
            ),
            "usage": {"input_tokens": 1, "output_tokens": 100},
        }

    result = asyncio.run(TermResolutionRunner(store, transport=transport)._run_v1())

    assert result.status == "closed"
    assert result.deferred == 1
    assert len(tuple((store.root / "requests").glob("*.json"))) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("candidate_ids", ["ghost-candidate"]),
        ("allowed_unit_ids", ["ghost-unit"]),
    ),
)
def test_resolution_rejects_ghost_conflict_references_before_dispatch(
    tmp_path: Path, field: str, value: list[str]
) -> None:
    store, pool = _seed_conflict(tmp_path)
    group = pool.conflict_groups[0] | {field: value}
    store.save_candidate_pool(
        pool.model_copy(update={"record_version": 1, "conflict_groups": (group,)}),
        expected_record_version=0,
    )

    with pytest.raises(ValueError, match="facts differ from canonical conflict"):
        TermResolutionRunner(store)

    assert not tuple((store.root / "requests").glob("*.json"))


def test_resolution_rejects_a_candidate_absent_from_extraction_records(tmp_path: Path) -> None:
    store, pool = _seed_conflict(tmp_path)
    ghost = pool.candidates[0].model_copy(update={"candidate_id": "ghost-candidate"})
    store.save_candidate_pool(
        pool.model_copy(update={"record_version": 1, "candidates": (*pool.candidates, ghost)}),
        expected_record_version=0,
    )

    with pytest.raises(ValueError, match="differs from committed extraction records"):
        TermResolutionRunner(store)

    assert not tuple((store.root / "requests").glob("*.json"))


def test_resolution_rejects_candidate_content_forged_under_an_existing_id(tmp_path: Path) -> None:
    store, pool = _seed_conflict(tmp_path)
    forged = pool.candidates[0].model_copy(update={"target": "幽灵"})
    store.save_candidate_pool(
        pool.model_copy(update={"record_version": 1, "candidates": (forged, *pool.candidates[1:])}),
        expected_record_version=0,
    )

    with pytest.raises(ValueError, match="differs from committed extraction records"):
        TermResolutionRunner(store)

    assert not tuple((store.root / "requests").glob("*.json"))


def test_resolution_rejects_an_extra_conflict_group_before_dispatch(tmp_path: Path) -> None:
    store, pool = _seed_conflict(tmp_path)
    forged = pool.conflict_groups[0] | {
        "group_id": "tcg-forged",
        "group_input_hash": "forged-input",
    }
    store.save_candidate_pool(
        pool.model_copy(update={"record_version": 1, "conflict_groups": (*pool.conflict_groups, forged)}),
        expected_record_version=0,
    )

    with pytest.raises(ValueError, match="conflict groups differ from canonical conflicts"):
        TermResolutionRunner(store)

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
    assert first.reason == "credential unavailable"
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


def test_finished_unknown_resolution_is_deferred_without_another_http_call(tmp_path: Path) -> None:
    store, pool = _seed_conflict(tmp_path)
    group = pool.conflict_groups[0]
    group_id = str(group["group_id"])
    request_id = "rr-finished-unknown"
    attempt_id = "attempt-finished-unknown"
    store.write_request(
        RequestManifest(
            request_id=request_id,
            stage="resolution",
            owner_kind="resolution_group",
            owner_id=group_id,
            item_ids=(group_id,),
            input_hashes={group_id: str(group["group_input_hash"])},
            wire_hash="finished-unknown-wire",
        )
    )
    store.reserve_attempt(
        request_id,
        Attempt(
            attempt_id=attempt_id,
            affected_items=(group_id,),
            state="reserved",
            created_at=datetime.now(UTC).isoformat(),
        ),
    )
    store.finish_attempt(
        request_id,
        attempt_id,
        state="unknown",
        finished_at=datetime.now(UTC).isoformat(),
        error="provider outcome unavailable",
    )

    async def forbidden(*_args):
        raise AssertionError("a finished unknown resolution must not dispatch again")

    result = asyncio.run(TermResolutionRunner(store, transport=forbidden).run())

    assert result.status == "closed"
    assert result.deferred == 1
    assert store.read_candidate_pool().conflict_groups[0]["reason"] == "resolution_unknown_result"


def test_later_success_for_same_group_supersedes_an_old_unknown_attempt(tmp_path: Path) -> None:
    store, pool = _seed_conflict(tmp_path)
    group = pool.conflict_groups[0]
    group_id = str(group["group_id"])
    candidate_ids = group["candidate_ids"]
    assert isinstance(candidate_ids, list) and candidate_ids and isinstance(candidate_ids[0], str)
    candidate_id = candidate_ids[0]
    attempts: tuple[tuple[str, str, Literal["unknown", "sent"]], ...] = (
        ("rr-a-unknown", "attempt-unknown", "unknown"),
        ("rr-z-success", "attempt-success", "sent"),
    )
    for request_id, attempt_id, state in attempts:
        store.write_request(
            RequestManifest(
                request_id=request_id,
                stage="resolution",
                owner_kind="resolution_group",
                owner_id=group_id,
                item_ids=(group_id,),
                input_hashes={group_id: str(group["group_input_hash"])},
                wire_hash=f"{request_id}-wire",
            )
        )
        store.reserve_attempt(
            request_id,
            Attempt(
                attempt_id=attempt_id,
                affected_items=(group_id,),
                created_at=datetime.now(UTC).isoformat(),
            ),
        )
        store.finish_attempt(request_id, attempt_id, state=state, finished_at=datetime.now(UTC).isoformat())
    store.save_model_response(
        "resolution",
        "rr-z-success",
        "attempt-success",
        {
            "raw": json.dumps(
                {
                    "protocol": "epubox-term-resolution-2",
                    "request_id": "rr-z-success",
                    "items": [
                        {
                            "group_id": group_id,
                            "decision": "select",
                            "selected_candidate_ids": [candidate_id],
                            "reason": "Supported by the supplied evidence.",
                        }
                    ],
                }
            ),
            "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        },
    )

    async def forbidden(*_args):
        raise AssertionError("a later saved success must replay without HTTP")

    first = asyncio.run(TermResolutionRunner(store, transport=forbidden).run())
    second = asyncio.run(TermResolutionRunner(store, transport=forbidden).run())

    assert first.status == second.status == "closed"
    assert first.selected == second.selected == 1
    assert first.http_attempts == second.http_attempts == 2
    assert store.read_request("rr-z-success").attempts[0].state == "succeeded"
