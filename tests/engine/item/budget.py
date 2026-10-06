from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

import pytest
from pydantic import ValidationError

from engine.agents import runtime as runtime_module
from engine.agents.runtime import wire_hash as request_wire_hash
from engine.item import budget as budget_module
from engine.item.budget import BUDGET_VERSION, BudgetLimits, BudgetResult, measure_budget
from engine.item.planner import PlannerConfig


def item(source: str = "A short source.", *, target: str | None = None) -> dict[str, str]:
    result = {"item_id": "item-1", "source": source}
    if target is not None:
        result["target"] = target
    return result


def payload(stage: str, items: Sequence[Mapping[str, object]], *, padding: str = "") -> dict[str, object]:
    result: dict[str, object] = {
        "protocol": "epubox-text-1" if stage == "translate" else "epubox-review-2",
        "request_id": "tx-" + "0" * 32,
        "items": [dict(entry) for entry in items],
    }
    if stage == "translate":
        result["target_language"] = "zh-Hans"
    if padding:
        result["shared_context"] = padding
    return result


def limits(**changes: float) -> BudgetLimits:
    values: dict[str, int | float] = {
        "source_tokens": 2000,
        "input_tokens": 50_000,
        "output_tokens": 4096,
        "context_tokens": 60_000,
        "safety_tokens": 256,
        "target_ratio": 1.6,
    }
    values.update(changes)
    return BudgetLimits(**values)  # type: ignore[arg-type]


def test_complete_payload_budget_is_token_based_bound_and_serializable() -> None:
    source = "x" * 20_000
    request = payload("translate", (item(source),))
    result = measure_budget(stage="translate", payload=request, limits=limits())

    assert result.source_tokens < len(source.encode())
    assert result.input_tokens < len(str(request).encode())
    assert result.input_reserve > result.input_tokens
    assert result.context_tokens == result.input_reserve + result.output_tokens + 256
    assert result.identity.source_limit == 2000
    assert result.identity.version == BUDGET_VERSION == 2
    assert result.identity.tokenizer_version
    assert result.identity.strategy == "cl100k+50pct+256"
    assert result.wire_hash == request_wire_hash("translate", request, result.output_tokens)
    assert BudgetResult.model_validate(result.model_dump()) == result


def test_saved_budget_rejects_changed_arithmetic_or_failures() -> None:
    result = measure_budget(stage="translate", payload=payload("translate", (item(),)), limits=limits())
    wrong_input = result.model_dump()
    wrong_input["input_reserve"] += 1
    with pytest.raises(ValidationError, match="input reserve"):
        BudgetResult.model_validate(wrong_input)

    wrong_failures = result.model_dump()
    wrong_failures["failures"] = ["source budget 1 exceeds 1"]
    with pytest.raises(ValidationError, match="failures"):
        BudgetResult.model_validate(wrong_failures)


def test_payload_alone_controls_messages_and_wire_identity() -> None:
    plain = measure_budget(stage="translate", payload=payload("translate", (item(),)), limits=limits())
    padded_request = payload("translate", (item(),), padding="context " * 100)
    padded = measure_budget(stage="translate", payload=padded_request, limits=limits())

    assert padded.input_tokens > plain.input_tokens
    assert padded.wire_hash == request_wire_hash("translate", padded_request, padded.output_tokens)
    assert padded.wire_hash != plain.wire_hash


def test_wire_identity_changes_with_output_cap_or_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    request = payload("translate", (item(),))
    result = measure_budget(stage="translate", payload=request, limits=limits())

    assert request_wire_hash("translate", request, result.output_tokens + 1) != result.wire_hash
    monkeypatch.setitem(
        runtime_module._SYSTEM_PROMPTS,
        "translate",
        runtime_module._SYSTEM_PROMPTS["translate"] + " Changed prompt.",
    )
    changed = measure_budget(stage="translate", payload=request, limits=limits())
    assert changed.wire_hash != result.wire_hash


def test_new_source_limit_is_independent_of_the_historical_1200_cap() -> None:
    source = "word " * 1300
    result = measure_budget(stage="translate", payload=payload("translate", (item(source),)), limits=limits())

    assert result.source_tokens > 1200
    assert not any(reason.startswith("source ") for reason in result.failures)
    with pytest.raises(ValueError, match="cannot exceed 1200"):
        PlannerConfig(context_tokens=60_000, max_source_tokens=1201)


def test_each_budget_dimension_reports_its_own_failure() -> None:
    request = payload("translate", (item(),))
    base = measure_budget(stage="translate", payload=request, limits=limits())
    constrained = limits(
        source_tokens=max(1, base.source_tokens - 1),
        input_tokens=max(1, base.input_reserve - 1),
        output_tokens=max(1, base.output_tokens - 1),
        context_tokens=max(1, base.context_tokens - 1),
    )
    result = measure_budget(stage="translate", payload=request, limits=constrained)

    assert {reason.split()[0] for reason in result.failures} == {"source", "input", "output", "context"}
    assert not result.fits


def test_review_actual_and_estimated_targets_are_distinct() -> None:
    source = "source " * 30
    actual = measure_budget(
        stage="review", payload=payload("review", (item(source, target="短译文"),)), limits=limits()
    )
    estimated = measure_budget(
        stage="review",
        payload=payload("review", (item(source),)),
        limits=limits(),
        review_targets="estimated",
    )

    assert actual.review_targets == "actual"
    assert estimated.review_targets == "estimated"
    assert actual.review_target_input_tokens == 0
    assert estimated.review_target_input_tokens > 0
    assert estimated.input_reserve > actual.input_reserve
    assert actual.output_tokens < estimated.output_tokens
    with pytest.raises(ValueError, match="requires saved targets"):
        measure_budget(stage="review", payload=payload("review", (item(source),)), limits=limits())


def test_review_none_target_and_invalid_payload_boundaries_are_rejected() -> None:
    with pytest.raises(TypeError, match="target must be"):
        measure_budget(
            stage="review",
            payload=payload("review", ({"item_id": "item-1", "source": "source", "target": None},)),
            limits=limits(),
        )
    with pytest.raises(ValueError, match="at least one item"):
        measure_budget(stage="translate", payload=payload("translate", ()), limits=limits())
    with pytest.raises(ValueError, match="duplicate budget item_id"):
        measure_budget(stage="translate", payload=payload("translate", (item(), item("other"))), limits=limits())
    bad_protocol = payload("translate", (item(),)) | {"protocol": "epubox-review-2"}
    with pytest.raises(ValueError, match="protocol"):
        measure_budget(stage="translate", payload=bad_protocol, limits=limits())


@pytest.mark.parametrize(
    ("bad_item", "error"),
    [
        ({"item_id": "item-1", "source": None}, TypeError),
        ({"item_id": "item-1", "source": ""}, ValueError),
        ({"item_id": "item-1", "source": "source", "target": 1}, TypeError),
        ({"item_id": "item-1", "source": "source", "target": ""}, TypeError),
    ],
)
def test_invalid_source_and_target_values_are_rejected(bad_item: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        measure_budget(stage="translate", payload=payload("translate", (bad_item,)), limits=limits())


@pytest.mark.parametrize("ratio", [float("nan"), float("inf"), float("-inf")])
def test_target_ratio_must_be_finite(ratio: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        limits(target_ratio=ratio)


def test_unknown_model_uses_declared_cl100k_fallback() -> None:
    result = measure_budget(
        stage="translate",
        payload=payload("translate", (item(),)),
        limits=limits(),
        tokenizer_model="unknown-local-model",
    )

    assert result.identity.tokenizer == "cl100k_base"
    assert result.identity.tokenizer_fallback is True


def test_non_cl100k_model_uses_same_declared_fallback_as_output_estimate() -> None:
    result = measure_budget(
        stage="translate",
        payload=payload("translate", (item(),)),
        limits=limits(),
        tokenizer_model="gpt-4o",
    )

    assert result.identity.tokenizer == "cl100k_base"
    assert result.identity.tokenizer_fallback is True


def test_tokenizer_failure_is_closed_before_a_result(monkeypatch: pytest.MonkeyPatch) -> None:
    budget_module._tokenizer.cache_clear()
    monkeypatch.setattr(budget_module.tiktoken, "encoding_for_model", lambda _model: (_ for _ in ()).throw(OSError()))

    with pytest.raises(RuntimeError, match="tokenizer unavailable"):
        measure_budget(stage="translate", payload=payload("translate", (item(),)), limits=limits())
    budget_module._tokenizer.cache_clear()


def test_output_tokenizer_mismatch_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(budget_module, "_planner_tokenizer", lambda: None)
    with pytest.raises(RuntimeError, match="output tokenizer"):
        measure_budget(stage="translate", payload=payload("translate", (item(),)), limits=limits())


def test_measured_budget_round_trips_into_the_frozen_batch_contract() -> None:
    from engine.schemas.bridge import BATCH_FORMAT, RequestBatch
    from engine.schemas.contracts import canonical_json_bytes, parse_contract
    from tests.engine.schemas.bridge import batch_data

    data = batch_data()
    measured = measure_budget(stage="translate", payload=data["payload"], limits=limits())
    assert measured.fits
    data["budget"] = measured.model_dump()
    data["manifest"]["wire_hash"] = measured.wire_hash
    batch = RequestBatch.model_validate(data)
    assert parse_contract(canonical_json_bytes(batch), RequestBatch, BATCH_FORMAT) == batch


def test_review_output_reserves_the_actual_request_id_and_revision_envelope() -> None:
    import json

    import tiktoken

    wire: dict[str, object] = dict(item("Hello.", target="你好。"))
    wire["base_revision"] = 987_654_321
    request = payload("review", (wire,))
    request["request_id"] = "request-" + "abcdef" * 150
    result = measure_budget(stage="review", payload=request, limits=limits())
    envelope = {
        "protocol": "epubox-review-2",
        "request_id": request["request_id"],
        "items": [
            {
                "item_id": "item-1",
                "base_revision": wire["base_revision"],
                "decision": "replace",
                "checks": {
                    "accuracy": "pass",
                    "fluency": "pass",
                    "terminology": "not_applicable",
                    "bindings": "not_applicable",
                    "script": "pass",
                },
                "issues": [],
                "target": "",
            }
        ],
    }
    tokenizer = tiktoken.get_encoding("cl100k_base")
    expected = len(tokenizer.encode(json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
    expected += len(tokenizer.encode("你好。"))

    assert result.output_tokens == expected


@pytest.mark.parametrize("stage", ["translate", "review"])
def test_provider_cap_reserves_full_configured_output_for_short_json(stage: Literal["translate", "review"]) -> None:
    request = payload(
        stage, (item("AI Side Hustle Playbook", target="AI 副业实战指南" if stage == "review" else None),)
    )
    measured = measure_budget(stage=stage, payload=request, limits=limits(output_version=3))
    assert measured.fits
    assert measured.identity.version == 3
    assert measured.output_tokens == 4096
    assert measured.context_tokens == measured.input_reserve + 4096 + 256


def test_new_output_policy_preserves_legacy_frozen_limits() -> None:
    assert "output_version" not in limits().to_dict()
    assert limits(output_version=3).to_dict()["output_version"] == 3
    request = payload("translate", (item(),))
    legacy = measure_budget(stage="translate", payload=request, limits=limits())
    current = measure_budget(stage="translate", payload=request, limits=limits(output_version=3))
    assert legacy.identity.version == 2
    assert legacy.output_tokens < 4096
    assert legacy.wire_hash != current.wire_hash
