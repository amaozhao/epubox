"""Pure token budgeting for complete translation and review requests."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

import tiktoken

from engine.agents.runtime import MAX_MODEL_INPUT_TOKENS, request_messages
from engine.agents.runtime import wire_hash as request_wire_hash
from engine.item.planner import MAX_SOURCE_TOKENS, PlannerConfig, _planner_tokenizer, recommended_output_tokens
from engine.schemas.budget import (
    BUDGET_VERSION,
    BudgetIdentity,
    BudgetLimits,
    BudgetResult,
    BudgetStage,
    ReviewTargets,
)

TOKENIZER_MARGIN_PERCENT = 50
WRAPPER_HEADROOM_TOKENS = 256
BUDGET_STRATEGY = "cl100k+50pct+256"


def measure_budget(
    *,
    stage: BudgetStage,
    payload: dict[str, Any],
    limits: BudgetLimits,
    review_targets: ReviewTargets = "actual",
    tokenizer_model: str = "gpt-3.5-turbo",
) -> BudgetResult:
    """Measure a complete candidate request without dispatching it."""
    if stage not in ("translate", "review"):
        raise ValueError(f"unsupported budget stage: {stage}")
    items_value = payload.get("items")
    if not isinstance(items_value, list) or any(not isinstance(item, Mapping) for item in items_value):
        raise TypeError("budget payload requires an items array of objects")
    items = tuple(items_value)
    if not items:
        raise ValueError("budget requires at least one item")
    _validate_items(items)
    messages = request_messages(stage, payload)

    tokenizer, tokenizer_name, fallback = _tokenizer(tokenizer_model)
    source_wire = [{"item_id": _item_id(item), "source": _source(item)} for item in items]
    source_tokens = _count(_json(source_wire), tokenizer)
    input_tokens = _count(_json({"messages": list(messages)}), tokenizer)
    review_target_input_tokens = 0

    output_items: Sequence[Mapping[str, Any]] = items
    target_basis: ReviewTargets | None = None
    if stage == "review":
        target_basis = review_targets
        if review_targets == "actual":
            missing = tuple(_item_id(item) for item in items if _target(item) is None)
            if missing:
                raise ValueError(f"review actual budget requires saved targets: {', '.join(missing)}")
        elif review_targets == "estimated":
            output_items = tuple(_without_target(item) for item in items)
            review_target_input_tokens = math.ceil(
                sum(_count(_source(item), tokenizer) for item in items) * limits.target_ratio
            )
        else:
            raise ValueError(f"unsupported review target basis: {review_targets}")

    input_reserve = (
        input_tokens
        + math.ceil(input_tokens * TOKENIZER_MARGIN_PERCENT / 100)
        + WRAPPER_HEADROOM_TOKENS
        + review_target_input_tokens
    )

    estimate_config = PlannerConfig(
        context_tokens=max(1, limits.context_tokens),
        max_source_tokens=MAX_SOURCE_TOKENS,
        max_output_tokens=1,
        review_output_tokens=1,
        safety_margin=max(1, limits.safety_tokens),
        target_ratio=limits.target_ratio,
    )
    output_tokenizer = _planner_tokenizer()
    if output_tokenizer is None or output_tokenizer.name != tokenizer_name:
        raise RuntimeError("output tokenizer does not match budget tokenizer")
    output_tokens = recommended_output_tokens(
        output_items,
        estimate_config,
        stage="translation" if stage == "translate" else "review",
        request_id=str(payload.get("request_id", "r00000000000000000000000000000000")),
    )
    context_tokens = input_reserve + output_tokens + limits.safety_tokens
    input_limit = min(limits.input_tokens, MAX_MODEL_INPUT_TOKENS)
    failures: list[str] = []
    if source_tokens > limits.source_tokens:
        failures.append(f"source budget {source_tokens} exceeds {limits.source_tokens}")
    if input_reserve > input_limit:
        failures.append(f"input budget {input_reserve} exceeds {input_limit}")
    if output_tokens > limits.output_tokens:
        failures.append(f"output budget {output_tokens} exceeds {limits.output_tokens}")
    if context_tokens > limits.context_tokens:
        failures.append(f"context budget {context_tokens} exceeds {limits.context_tokens}")

    return BudgetResult(
        stage=stage,
        source_tokens=source_tokens,
        input_tokens=input_tokens,
        review_target_input_tokens=review_target_input_tokens,
        input_reserve=input_reserve,
        output_tokens=output_tokens,
        context_tokens=context_tokens,
        review_targets=target_basis,
        failures=tuple(failures),
        identity=BudgetIdentity(
            version=BUDGET_VERSION,
            tokenizer=tokenizer_name,
            tokenizer_version=tiktoken.__version__,
            tokenizer_model=tokenizer_model,
            tokenizer_fallback=fallback,
            strategy=BUDGET_STRATEGY,
            margin_percent=TOKENIZER_MARGIN_PERCENT,
            wrapper_tokens=WRAPPER_HEADROOM_TOKENS,
            safety_tokens=limits.safety_tokens,
            target_ratio=limits.target_ratio,
            source_limit=limits.source_tokens,
            input_limit=input_limit,
            output_limit=limits.output_tokens,
            context_limit=limits.context_tokens,
        ),
        wire_hash=request_wire_hash(stage, payload, output_tokens),
    )


@lru_cache(maxsize=16)
def _tokenizer(model: str) -> tuple[Any, str, bool]:
    try:
        requested = tiktoken.encoding_for_model(model)
    except KeyError:
        requested = None
    except Exception as error:
        raise RuntimeError("tokenizer unavailable for budget calculation") from error
    if requested is not None and requested.name == "cl100k_base":
        return requested, requested.name, False
    try:
        tokenizer = tiktoken.get_encoding("cl100k_base")
        return tokenizer, tokenizer.name, True
    except Exception as error:
        raise RuntimeError("tokenizer unavailable for budget calculation") from error


def _count(value: str, tokenizer: Any) -> int:
    return len(tokenizer.encode(value))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _item_id(item: Mapping[str, Any]) -> str:
    value = item.get("item_id", item.get("unit_id"))
    if not isinstance(value, str) or not value:
        raise ValueError("budget item requires a non-empty item_id")
    return value


def _source(item: Mapping[str, Any]) -> str:
    value = item.get("source", item.get("source_projection"))
    if not isinstance(value, str):
        raise TypeError(f"budget item {_item_id(item)} requires a source string")
    return value


def _target(item: Mapping[str, Any]) -> str | None:
    value = item.get("target", item.get("target_projection"))
    return value if isinstance(value, str) and value else None


def _without_target(item: Mapping[str, Any]) -> Mapping[str, Any]:
    return {key: value for key, value in item.items() if key not in {"target", "target_projection"}}


def _validate_items(items: Sequence[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for item in items:
        item_id = _item_id(item)
        if item_id in seen:
            raise ValueError(f"duplicate budget item_id: {item_id}")
        seen.add(item_id)
        if not _source(item):
            raise ValueError(f"budget item {item_id} requires non-empty source")
        targets = [item[key] for key in ("target", "target_projection") if key in item]
        if any(not isinstance(target, str) or not target for target in targets):
            raise TypeError(f"budget item {item_id} target must be a non-empty string")
        if len(targets) == 2 and targets[0] != targets[1]:
            raise ValueError(f"budget item {item_id} has conflicting targets")
