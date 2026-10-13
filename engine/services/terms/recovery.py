"""Reconcile interrupted terminology HTTP attempts before resuming preparation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from engine.schemas.contracts import Attempt, RequestManifest, Usage, canonical_hash
from engine.services import state
from engine.services.atomic import IdentityMismatch

if TYPE_CHECKING:
    from engine.services.store import RunStore


def unfinished(attempt: Attempt) -> bool:
    return (
        attempt.state == "sent"
        or attempt.state == "unknown"
        and attempt.finished_at is None
        or attempt.state == "reserved"
        and any(value is not None for value in (attempt.sent_at, attempt.finished_at, attempt.usage, attempt.error))
    )


def reconcile_term_attempts(store: RunStore) -> tuple[RequestManifest, ...]:
    """Run before dispatch, under the run lock; never infer provider success.

    A response journal proves HTTP completion, independently of content acceptance.
    Without that evidence an interrupted send remains unknown, with an explicit
    local end time. Frozen glossary and extraction records are never rewritten.
    """
    recovered: list[RequestManifest] = []
    with store.lock(blocking=False), state.batch(store.root):
        finished_at = datetime.now(UTC).isoformat()
        for path in sorted(state.glob(store.root / "requests", "*.json")):
            original = request = store.read_request(path.stem)
            if request.stage not in {"terms", "resolution"}:
                continue
            for attempt in original.attempts:
                if attempt.state not in {"sent", "unknown"}:
                    continue
                response = store.read_model_response(request.stage, request.request_id, attempt.attempt_id)
                if response is None:
                    if not unfinished(attempt):
                        continue
                    request = store.finish_attempt(
                        request.request_id,
                        attempt.attempt_id,
                        state="unknown",
                        finished_at=finished_at,
                        error=attempt.error or "interrupted terminology request; provider outcome unknown",
                    )
                else:
                    request = store.finish_attempt(
                        request.request_id,
                        attempt.attempt_id,
                        state="succeeded",
                        finished_at=finished_at,
                        usage=Usage(
                            input_tokens=response.usage.input_tokens,
                            output_tokens=response.usage.output_tokens,
                            known_cost=response.usage.known_cost,
                        )
                        if response.usage is not None
                        else None,
                        metadata=attempt.metadata | response.metadata,
                    )
            if request != original:
                recovered.append(request)
    return tuple(recovered)


def require_terminal_requests(store: RunStore) -> None:
    requests = tuple(store.read_request(path.stem) for path in state.glob(store.root / "requests", "*.json"))
    succeeded = {
        (request.stage, request.owner_kind, request.owner_id, canonical_hash(request.input_hashes))
        for request in requests
        if any(attempt.state == "succeeded" for attempt in request.attempts)
    }
    for request in requests:
        if request.stage not in {"terms", "resolution"}:
            continue
        identity = (request.stage, request.owner_kind, request.owner_id, canonical_hash(request.input_hashes))
        if identity not in succeeded and any(unfinished(attempt) for attempt in request.attempts):
            raise IdentityMismatch(f"term request is not terminal: {request.request_id}")
