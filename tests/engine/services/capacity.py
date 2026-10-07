from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from engine.agents import runtime as runtime_module
from engine.agents import wire
from engine.agents.protocol import validate_translation_response
from engine.agents.runtime import wire_hash
from engine.item.budget import measure_budget
from engine.schemas.contracts import Attempt
from engine.services.atomic import IdentityMismatch
from engine.services.journal import BodyJournal
from engine.services.ready import limits_for
from tests.engine.agents.workflow import MODEL, prepare_case


@pytest.mark.parametrize("limit", ("input", "context"))
def test_physical_reserve_is_checked_even_when_frozen_logical_request_fits(tmp_path, limit):
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store, case.session)
    journal.runtime(transport=lambda *_args: None)
    measured = measure_budget(
        stage="translate",
        payload=case.batch.payload,
        limits=limits_for(case.prepared.preparation),
        tokenizer_model=MODEL,
    )
    assert measured.fits
    reserve = (
        measured.identity.input_limit + 1
        if limit == "input"
        else measured.identity.context_limit - measured.output_tokens - measured.identity.safety_tokens + 1
    )
    context = case.batch.manifest.model_dump(mode="python") | {
        "output_tokens": case.batch.budget.output_tokens,
        "physical_budget": {"cl100k_tokens": reserve // 2, "estimated_input_tokens": reserve},
    }
    journal._prepare("translate", case.batch.payload, context)
    with pytest.raises(IdentityMismatch, match="physical request exceeds"):
        journal._reserve(
            case.batch.manifest.request_id,
            SimpleNamespace(
                metadata={"wire_version": "epubox-wire-6"}, reservation={"estimated_input_tokens": reserve}
            ),
        )
    assert not journal.store.read_request(case.batch.manifest.request_id).attempts


def test_historical_wire_response_replays_before_new_physical_capacity_check(tmp_path, monkeypatch):
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store, case.session)
    journal.runtime(transport=lambda *_args: None)
    context = case.batch.manifest.model_dump(mode="python") | {"output_tokens": case.batch.budget.output_tokens}
    journal._prepare("translate", case.batch.payload, context)
    attempt = Attempt(
        attempt_id="wire-5",
        affected_items=case.batch.manifest.item_ids,
        reservation={"estimated_input_tokens": 1, "output_tokens": case.batch.budget.output_tokens},
        created_at="now",
        metadata={
            "wire_version": "epubox-wire-5",
            "wire_hash": wire_hash(
                "translate",
                case.batch.payload,
                case.batch.budget.output_tokens,
                compact=True,
                wire_version="epubox-wire-5",
            ),
        },
    )
    case.session.store.reserve_attempt(case.batch.manifest.request_id, attempt)
    case.session.store.save_model_response(
        "translate",
        case.batch.manifest.request_id,
        attempt.attempt_id,
        {
            "raw": json.dumps(
                {
                    "protocol": "epubox-text-1",
                    "request_id": case.batch.manifest.request_id,
                    "items": [{"item_id": "1", "target": {"1": "第一。"}}],
                }
            ),
            "finish_reason": "stop",
        },
    )

    resumed = BodyJournal(case.session.store)
    runtime = resumed.runtime(transport=lambda *_args: (_ for _ in ()).throw(AssertionError("must not dispatch")))
    runtime._compact = True
    original_budget = runtime_module.model_input_budget
    frozen = case.batch.budget.identity
    oversized = frozen.context_limit - case.batch.budget.output_tokens - frozen.safety_tokens + 1

    def budget(kind, payload, *, algorithm_version=1, compact=False):
        measured = original_budget(kind, payload, algorithm_version=algorithm_version, compact=compact)
        return measured | {"estimated_input_tokens": oversized} if compact else measured

    monkeypatch.setattr(runtime_module, "model_input_budget", budget)
    response = asyncio.run(runtime.invoke("translate", case.batch.payload, context))
    accepted = validate_translation_response(
        response["raw"], case.batch.manifest.request_id, {case.batch.items[0].item_id: "First."}
    ).accepted

    assert wire.VERSION != "epubox-wire-5" and "epubox-wire-5" in wire.VERSIONS
    assert oversized + case.batch.budget.output_tokens + frozen.safety_tokens > frozen.context_limit
    assert accepted[case.batch.items[0].item_id]["target"] == "第一。"
    request = resumed.store.read_request(case.batch.manifest.request_id)
    assert len(request.attempts) == 1 and request.attempts[0].state == "succeeded"
