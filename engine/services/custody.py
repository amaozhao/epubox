"""Small validation helpers shared by the durable body journal."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from engine.schemas.contracts import ItemRecord, RequestManifest, Usage
from engine.services.atomic import IdentityMismatch
from engine.services.store import RunStore


def manifest(context: Mapping[str, Any]) -> RequestManifest:
    fields = {name: context[name] for name in RequestManifest.model_fields if name in context and name != "attempts"}
    return RequestManifest.model_validate(fields)


def text(config: Mapping[str, Any], name: str) -> str:
    value = config.get(name)
    if not isinstance(value, str) or not value:
        raise IdentityMismatch(f"frozen body {name} must be a non-empty string")
    return value


def positive(config: Mapping[str, Any], name: str, default: int) -> int:
    value = config.get(name, default)
    if type(value) is not int or value < 1:
        raise IdentityMismatch(f"frozen body {name} must be a positive integer")
    return value


def optional(config: Mapping[str, Any], name: str) -> int | None:
    return positive(config, name, 1) if name in config else None


def bounded(config: Mapping[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = config.get(name, default)
    if type(value) is not int or not low <= value <= high:
        raise IdentityMismatch(f"frozen body {name} is outside its supported range")
    return value


def number(config: Mapping[str, Any], name: str, default: float) -> float:
    value = config.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise IdentityMismatch(f"frozen body {name} must be positive seconds")
    return float(value)


def epoch(record: ItemRecord, name: str) -> int:
    value = record.checks.get(name, 0)
    if type(value) is not int or value < 0:
        raise IdentityMismatch(f"saved {name.replace('_', ' ')} must be a non-negative integer")
    return value


def persisted_response(
    store: RunStore,
    request: RequestManifest,
    *,
    reconcile: bool = False,
    finish: Callable[..., None] | None = None,
):
    for attempt in reversed(request.attempts):
        response = store.read_model_response(request.stage, request.request_id, attempt.attempt_id)
        if response is None:
            continue
        if reconcile and attempt.state in {"reserved", "sent", "unknown"}:
            if finish is None:
                raise TypeError("response reconciliation requires a finish callback")
            usage = (
                Usage(
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    known_cost=response.usage.known_cost,
                )
                if response.usage is not None
                else None
            )
            finish(
                request.request_id,
                attempt.attempt_id,
                state="succeeded",
                usage=usage,
                finished_at=attempt.finished_at or attempt.sent_at or attempt.created_at,
                metadata=response.metadata,
            )
        return response
    return None


__all__ = ["bounded", "epoch", "manifest", "number", "optional", "persisted_response", "positive", "text"]
