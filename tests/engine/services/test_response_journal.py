from __future__ import annotations

import json

import pytest

from engine.schemas.contracts import (
    Attempt,
    CandidatePool,
    RequestManifest,
    TermExtractionRecord,
)
from engine.services.atomic_store import CorruptRecord, IdentityMismatch, StaleWrite
from engine.services.store import ModelResponseStage, RunStore
from tests.engine.services.test_store import _prepare, _write_term_plan


def _request(store: RunStore, stage: ModelResponseStage) -> RequestManifest:
    if stage in {"terms", "resolution"}:
        preparation = store.read_preparation()
        plan = _write_term_plan(store, preparation)
        item = plan.items[0]
        store.save_extraction(
            TermExtractionRecord(
                item_id=item.item_id,
                document_id=item.document_id,
                view_ids=item.view_ids,
                extraction_input_hash=item.extraction_input_hash,
            )
        )
        if stage == "terms":
            request = RequestManifest(
                request_id=f"request-{stage}",
                stage=stage,
                owner_kind="extraction_item",
                owner_id=item.item_id,
                item_ids=(item.item_id,),
                input_hashes={item.item_id: item.extraction_input_hash},
                wire_hash=f"wire-{stage}",
            )
        else:
            group_id = "group-1"
            group_hash = "group-hash"
            store.save_candidate_pool(
                CandidatePool(
                    source_hash=plan.source_hash,
                    preparation_hash=plan.preparation_hash,
                    term_plan_hash=plan.plan_hash,
                    conflict_groups=({"group_id": group_id, "group_input_hash": group_hash},),
                )
            )
            request = RequestManifest(
                request_id=f"request-{stage}",
                stage=stage,
                owner_kind="resolution_group",
                owner_id=group_id,
                item_ids=(group_id,),
                input_hashes={group_id: group_hash},
                wire_hash=f"wire-{stage}",
            )
    else:
        item_id = "item-1"
        target_hashes = {item_id: "target-hash"} if stage in {"review", "coherence"} else {}
        request = RequestManifest(
            request_id=f"request-{stage}",
            stage=stage,
            owner_kind="translation_item",
            owner_id=item_id,
            item_ids=(item_id,),
            input_hashes={item_id: "input-hash"},
            wire_hash=f"wire-{stage}",
            record_versions={"unit-1": 0},
            item_unit_ids={item_id: ("unit-1",)},
            unit_document_ids={"unit-1": "document-1"},
            plan_epochs={"unit-1": 0},
            revisions={"unit-1": 0},
            target_hashes=target_hashes,
            glossary_file_sha256="glossary-hash",
            freeze_id="freeze-1",
            term_ids_by_item={item_id: ()},
            terms_hashes={item_id: "terms-hash"},
            context_hashes={item_id: "context-hash"},
        )
    store.write_request(request)
    for attempt_id in ("attempt-1", "attempt-2"):
        store.reserve_attempt(
            request.request_id,
            Attempt(attempt_id=attempt_id, affected_items=request.item_ids, created_at="2026-09-30T00:00:00Z"),
        )
    return store.read_request(request.request_id)


@pytest.mark.parametrize("stage", ("terms", "resolution", "translate", "review", "coherence"))
def test_model_response_journal_round_trips_every_stage(tmp_path, stage: ModelResponseStage) -> None:
    store, _ = _prepare(tmp_path)
    request = _request(store, stage)
    envelope = {
        "raw": '{"ok":true}',
        "finish_reason": "stop",
        "usage": {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24, "known_cost": 0.01},
        "metadata": {"response_id": "response-1", "cached": True},
    }

    store.save_model_response(stage, request.request_id, "attempt-1", envelope)
    store.save_model_response(stage, request.request_id, "attempt-1", envelope)
    replay = store.read_model_response(stage, request.request_id, "attempt-1")

    assert replay is not None
    assert replay.raw == '{"ok":true}'
    assert replay.usage is not None and replay.usage.total_tokens == 24
    expected = (
        tmp_path / "glossary" / "responses" / request.request_id / "attempt-1.json"
        if stage == "terms"
        else tmp_path / "responses" / stage / request.request_id / "attempt-1.json"
    )
    assert expected.is_file()
    with pytest.raises(IdentityMismatch, match="does not match request stage"):
        store.read_model_response("review" if stage != "review" else "translate", request.request_id, "attempt-1")


def test_model_response_journal_rejects_altered_or_cross_attempt_replay(tmp_path) -> None:
    store, _ = _prepare(tmp_path)
    request = _request(store, "review")
    envelope = {"raw": "original", "metadata": {"response_id": "response-1"}}
    store.save_model_response("review", request.request_id, "attempt-1", envelope)

    with pytest.raises(StaleWrite, match="immutable model response"):
        store.save_model_response("review", request.request_id, "attempt-1", envelope | {"raw": "changed"})

    first = store._model_response_path("review", request.request_id, "attempt-1")
    second = store._model_response_path("review", request.request_id, "attempt-2")
    second.parent.mkdir(parents=True, exist_ok=True)
    second.write_bytes(first.read_bytes())
    with pytest.raises(IdentityMismatch, match="identity"):
        store.read_model_response("review", request.request_id, "attempt-2")

    payload = json.loads(first.read_text())
    payload["response"]["raw"] = "tampered"
    first.write_text(json.dumps(payload))
    with pytest.raises(CorruptRecord, match="size or hash mismatch"):
        store.read_model_response("review", request.request_id, "attempt-1")
