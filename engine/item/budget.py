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
from engine.item.inline import parse_projection
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


def request_source_tokens(payload: Mapping[str, Any], tokenizer_model: str) -> int:
    """Count translatable source text, excluding markers and shared context."""
    items = payload.get("items")
    if not isinstance(items, list) or any(not isinstance(item, Mapping) for item in items):
        raise TypeError("body request requires an items array")
    _validate_items(items)
    tokenizer, _name, _fallback = _tokenizer(tokenizer_model)
    return sum(_projection_parts(_source(item), tokenizer)[0] for item in items)


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
    messages = (
        request_messages(stage, payload, compact=True, wire_version="epubox-wire-5")
        if limits.output_version == 5
        else request_messages(stage, payload, compact=limits.output_version in {6, 7})
    )

    tokenizer, tokenizer_name, fallback = _tokenizer(tokenizer_model)
    source_wire = [{"item_id": _item_id(item), "source": _source(item)} for item in items]
    source_tokens = (
        sum(_projection_parts(_source(item), tokenizer)[0] for item in items)
        if limits.output_version in {4, 5, 6, 7}
        else _count(_json(source_wire), tokenizer)
    )
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
            if limits.output_version in {4, 5, 6, 7}:
                review_target_input_tokens = sum(
                    markers + math.ceil(text * limits.target_ratio)
                    for item in items
                    for text, markers in (_projection_parts(_source(item), tokenizer),)
                )
            else:
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
    if limits.output_version in {5, 6, 7}:
        output_tokens = _slotted_output_tokens(
            stage,
            output_items,
            payload,
            limits,
            tokenizer,
            formatted=limits.output_version in {6, 7},
        )
    elif limits.output_version == 4:
        output_tokens = _v4_output_tokens(stage, output_items, payload, limits, estimate_config, tokenizer)
    else:
        output_tokens = recommended_output_tokens(
            output_items,
            estimate_config,
            stage="translation" if stage == "translate" else "review",
            request_id=str(payload.get("request_id", "r00000000000000000000000000000000")),
        )
    if limits.output_version == 3:
        assert limits.output_tokens is not None
        estimated_output = (
            output_tokens + math.ceil(output_tokens * TOKENIZER_MARGIN_PERCENT / 100) + WRAPPER_HEADROOM_TOKENS
        )
        output_tokens = max(limits.output_tokens, estimated_output)
    context_tokens = input_reserve + output_tokens + limits.safety_tokens
    input_limit = min(limits.input_tokens, MAX_MODEL_INPUT_TOKENS)
    failures: list[str] = []
    if source_tokens > limits.source_ceiling:
        failures.append(f"source budget {source_tokens} exceeds {limits.source_ceiling}")
    if input_reserve > input_limit:
        failures.append(f"input budget {input_reserve} exceeds {input_limit}")
    if limits.output_tokens is not None and output_tokens > limits.output_tokens:
        failures.append(f"output budget {output_tokens} exceeds {limits.output_tokens}")
    context_gate = input_reserve + limits.safety_tokens if limits.output_version == 7 else context_tokens
    if not limits.context_unlimited and context_gate > limits.context_tokens:
        failures.append(f"context budget {context_gate} exceeds {limits.context_tokens}")

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
            version=limits.output_version,
            tokenizer=tokenizer_name,
            tokenizer_version=tiktoken.__version__,
            tokenizer_model=tokenizer_model,
            tokenizer_fallback=fallback,
            strategy=BUDGET_STRATEGY,
            margin_percent=TOKENIZER_MARGIN_PERCENT,
            wrapper_tokens=WRAPPER_HEADROOM_TOKENS,
            safety_tokens=limits.safety_tokens,
            target_ratio=limits.target_ratio,
            source_limit=limits.source_ceiling,
            source_hard_limit=limits.source_hard_limit,
            input_limit=input_limit,
            output_limit=limits.output_tokens,
            context_limit=limits.context_tokens,
            context_unlimited=limits.context_unlimited,
        ),
        wire_hash=request_wire_hash(stage, payload, None if limits.output_version == 7 else output_tokens),
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


def _projection_parts(projection: str, tokenizer: Any) -> tuple[int, int]:
    text = 0
    markers = 0
    for event in parse_projection(projection):
        if event.kind == "text":
            text += _count(event.value, tokenizer)
        else:
            markers += _count(f"⟦{event.value}⟧", tokenizer)
    return text, markers


def _v4_output_tokens(
    stage: BudgetStage,
    items: Sequence[Mapping[str, Any]],
    payload: Mapping[str, Any],
    limits: BudgetLimits,
    config: PlannerConfig,
    tokenizer: Any,
) -> int:
    empty = tuple(
        {
            "item_id": _item_id(item),
            "source": "",
            "target": "",
            "base_revision": item.get("base_revision", 0),
        }
        for item in items
    )
    envelope = recommended_output_tokens(
        empty,
        config,
        stage="translation" if stage == "translate" else "review",
        request_id=str(payload.get("request_id", "r00000000000000000000000000000000")),
    )
    content = 0
    for item in items:
        target = _target(item)
        if stage == "review" and target is not None:
            content += _count(target, tokenizer)
            continue
        text, markers = _projection_parts(_source(item), tokenizer)
        content += markers + math.ceil(text * limits.target_ratio)
    complete = envelope + content
    reserved = complete + math.ceil(complete * TOKENIZER_MARGIN_PERCENT / 100) + WRAPPER_HEADROOM_TOKENS
    return reserved if limits.output_tokens is None else max(limits.output_tokens, reserved)


def _slotted_output_tokens(
    stage: BudgetStage,
    items: Sequence[Mapping[str, Any]],
    payload: Mapping[str, Any],
    limits: BudgetLimits,
    tokenizer: Any,
    *,
    formatted: bool,
) -> int:
    """Reserve only emitted short IDs/text slots; markers are restored locally."""
    envelope: list[dict[str, Any]] = []
    text_tokens = 0
    for number, item in enumerate(items, 1):
        events = parse_projection(_source(item))
        slots = [event.value for event in events if event.kind == "text" and event.value.strip()]
        target = _target(item)
        text_tokens += (
            _projection_parts(target, tokenizer)[0]
            if stage == "review" and target is not None
            else math.ceil(sum(_count(text, tokenizer) for text in slots) * limits.target_ratio)
        )
        value: dict[str, Any] = {
            "item_id": str(number),
            "target": {str(index): "" for index in range(1, len(slots) + 1)},
        }
        if stage == "review":
            value.update(
                base_revision=item.get("base_revision", 0),
                decision="replace",
                checks={key: "not_applicable" for key in ("accuracy", "fluency", "terminology", "bindings", "script")},
                issues=[],
            )
        envelope.append(value)
    if stage == "review":
        text_tokens += 160  # Shared allowance for concise issues, plus the 50% response margin below.
    response = {"protocol": payload["protocol"], "request_id": payload.get("request_id", ""), "items": envelope}
    encoded = json.dumps(response, ensure_ascii=False, sort_keys=True, indent=2) if formatted else _json(response)
    complete = text_tokens + _count(encoded, tokenizer)
    reserved = complete + math.ceil(complete * TOKENIZER_MARGIN_PERCENT / 100) + WRAPPER_HEADROOM_TOKENS
    return reserved if limits.output_tokens is None else max(limits.output_tokens, reserved)


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


__all__ = ["BUDGET_VERSION", "BudgetLimits", "BudgetResult", "measure_budget", "request_source_tokens"]
