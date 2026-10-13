"""Interrupted terminology attempts must not strand a frozen glossary."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Literal

import pytest

from engine.schemas.contracts import Attempt, RequestManifest, TermExtractionRecord, canonical_hash
from engine.services import state
from engine.services.atomic import IdentityMismatch
from engine.services.preparation import resume_preparation
from engine.services.ready import read_ready
from engine.services.terms.freeze import freeze_terminology
from engine.services.terms.recovery import reconcile_term_attempts, require_terminal_requests
from engine.services.terms.resolution import TermResolutionRunner
from tests.engine.epub.test_preparation import StubChecker
from tests.engine.services.terms.test_resolution import _seed_conflict
from tests.engine.services.terms.test_storage import atomic_plan


def frozen_interrupted_store(tmp_path, attempt_state: Literal["sent", "unknown"] = "sent"):
    store, inventory, plan = atomic_plan(tmp_path)
    store.write_term_plan(plan)
    item = plan.items[0]
    request_id = "tr-interrupted-before-freeze"
    records = {
        member.item_id: store.save_extraction(
            TermExtractionRecord(
                item_id=member.item_id,
                document_id=member.document_id,
                view_ids=member.view_ids,
                extraction_input_hash=member.extraction_input_hash,
                status="failed_exhausted",
                request_ids=(request_id,) if member.item_id == item.item_id else (),
            )
        )
        for member in plan.items
    }
    store.write_request(
        RequestManifest(
            request_id=request_id,
            stage="terms",
            owner_kind="extraction_item",
            owner_id=item.item_id,
            item_ids=(item.item_id,),
            input_hashes={item.item_id: item.extraction_input_hash},
            wire_hash="frozen-wire",
            attempts=(
                Attempt(
                    attempt_id="interrupted",
                    affected_items=(item.item_id,),
                    state=attempt_state,
                    created_at=datetime.now(UTC).isoformat(),
                    sent_at=datetime.now(UTC).isoformat(),
                ),
            ),
        )
    )
    preparation = store.read_preparation()
    frozen = freeze_terminology(
        plan,
        records,
        (),
        preparation.unit_documents,
        (inventory.document,),
        extraction_config_hash=canonical_hash(preparation.extraction_config),
    )
    store.save_candidate_pool(frozen.candidate_pool)
    store.write_freeze(frozen.freeze_intent)
    store.write_glossary(frozen.glossary)
    return store, request_id


@pytest.mark.parametrize("attempt_state", ["sent", "unknown"])
def test_frozen_glossary_resumes_after_an_interrupted_attempt_without_response(tmp_path, attempt_state):
    store, request_id = frozen_interrupted_store(tmp_path, attempt_state)
    frozen_paths = [store.root / "glossary.json", *state.glob(store.root / "glossary", "*.json")]
    frozen_paths.extend(state.glob(store.root / "glossary" / "extraction", "*.json"))
    before = {path: state.read(path) for path in frozen_paths}

    async def forbidden(*_args):
        raise AssertionError("frozen terminology recovery must not call the provider")

    result = asyncio.run(
        resume_preparation(store.root, StubChecker(), term_transport=forbidden, resolution_transport=forbidden)
    )
    assert result.prepared is not None and result.status == "needs_attention"
    attempt = store.read_request(request_id).attempts[0]
    assert attempt.state == "unknown" and attempt.finished_at is not None
    assert attempt.usage is None and "unknown" in (attempt.error or "")
    assert read_ready(store) == result.prepared
    assert {path: state.read(path) for path in frozen_paths} == before
    again = asyncio.run(resume_preparation(store.root, StubChecker(), term_transport=forbidden))
    assert again.prepared == result.prepared
    assert store.read_request(request_id).attempts[0] == attempt


@pytest.mark.parametrize("finished_unknown", [False, True])
def test_saved_response_takes_precedence_and_does_not_change_frozen_extraction(tmp_path, finished_unknown):
    store, request_id = frozen_interrupted_store(tmp_path)
    request = store.read_request(request_id)
    if finished_unknown:
        store.finish_attempt(
            request_id,
            request.attempts[0].attempt_id,
            state="unknown",
            finished_at=datetime.now(UTC).isoformat(),
            error="provider outcome unknown",
        )
    item_id = request.item_ids[0]
    prior_record = store.read_extraction(item_id)
    store.save_term_response(
        request_id,
        request.attempts[0].attempt_id,
        {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": request_id,
                    "items": [{"item_id": item_id, "candidates": []}],
                }
            ),
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            "metadata": {"response_id": "saved-response"},
        },
    )
    recovered = reconcile_term_attempts(store)
    assert len(recovered) == 1
    attempt = recovered[0].attempts[0]
    assert attempt.state == "succeeded" and attempt.finished_at is not None
    assert attempt.usage is not None and attempt.usage.input_tokens == 3
    assert attempt.metadata["response_id"] == "saved-response"
    assert store.read_extraction(item_id) == prior_record
    assert reconcile_term_attempts(store) == ()


def test_interrupted_resolution_is_deferred_and_its_attempt_is_closed_without_http(tmp_path):
    store, pool = _seed_conflict(tmp_path)
    group = pool.conflict_groups[0]
    group_id = str(group["group_id"])
    request_id = "rr-interrupted"
    store.write_request(
        RequestManifest(
            request_id=request_id,
            stage="resolution",
            owner_kind="resolution_group",
            owner_id=group_id,
            item_ids=(group_id,),
            input_hashes={group_id: str(group["group_input_hash"])},
            wire_hash="resolution-wire",
            attempts=(
                Attempt(
                    attempt_id="interrupted",
                    affected_items=(group_id,),
                    state="sent",
                    created_at=datetime.now(UTC).isoformat(),
                    sent_at=datetime.now(UTC).isoformat(),
                ),
            ),
        )
    )

    async def forbidden(*_args):
        raise AssertionError("unknown resolution must not be silently sent again")

    result = asyncio.run(TermResolutionRunner(store, transport=forbidden).run())
    attempt = store.read_request(request_id).attempts[0]
    assert result.status == "closed" and result.deferred == 1
    assert attempt.state == "unknown" and attempt.finished_at is not None
    require_terminal_requests(store)


def test_recovery_preserves_unsent_reservations_and_rejects_inconsistent_ones(tmp_path):
    store, request_id = frozen_interrupted_store(tmp_path)
    request = store.read_request(request_id)
    reserved = request.attempts[0].model_copy(update={"state": "reserved", "sent_at": None})
    store._atomic_write(store._path("requests", request_id), request.model_copy(update={"attempts": (reserved,)}))
    assert reconcile_term_attempts(store) == ()
    require_terminal_requests(store)
    invalid = reserved.model_copy(update={"error": "unexpected send evidence"})
    store._atomic_write(store._path("requests", request_id), request.model_copy(update={"attempts": (invalid,)}))
    assert reconcile_term_attempts(store) == ()
    with pytest.raises(IdentityMismatch, match="term request is not terminal"):
        require_terminal_requests(store)
