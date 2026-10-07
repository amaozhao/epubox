"""Small validation helpers shared by the durable body journal."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from engine.schemas.contracts import ItemRecord, ItemStatus, RequestManifest, Usage, canonical_hash
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


def retry_checks(record: ItemRecord, review_epoch: int) -> dict[str, Any]:
    translation_epoch = epoch(record, "translation_epoch")
    return {
        "translation_frame": record.checks["translation_frame"],
        "review_epoch": review_epoch,
        **({"translation_epoch": translation_epoch} if translation_epoch else {}),
        **(
            {"retry_feedback": str(record.failure["message"])[:1200]}
            if record.failure and record.failure.get("message")
            else {}
        ),
    }


def review_draft(record: ItemRecord, target: str, review_epoch: int) -> ItemRecord:
    """Reconstruct the exact persisted draft used to identify a review request."""
    return record.model_copy(
        update={
            "stage": "proofread",
            "status": ItemStatus.LOCAL_VALID,
            "target_projection": target,
            "target_hash": canonical_hash(target),
            "checks": {
                "translation_frame": record.checks["translation_frame"],
                **({"review_epoch": review_epoch} if review_epoch else {}),
                **(
                    {"translation_epoch": record.checks["translation_epoch"]}
                    if "translation_epoch" in record.checks
                    else {}
                ),
            },
            "failure": None,
            "next_action": "review",
        }
    )


def review_feedback(records: Mapping[str, ItemRecord], feedback: Mapping[str, str]) -> dict[str, ItemRecord]:
    """Restore the diagnostic snapshot used to identify a historical review draft."""
    return {
        item_id: record.model_copy(
            update={
                "checks": {
                    **{key: value for key, value in record.checks.items() if key != "retry_feedback"},
                    **({"retry_feedback": feedback[item_id]} if item_id in feedback else {}),
                }
            }
        )
        for item_id, record in records.items()
    }


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


__all__ = [
    "bounded",
    "epoch",
    "manifest",
    "number",
    "optional",
    "persisted_response",
    "positive",
    "retry_checks",
    "text",
]
