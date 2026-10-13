"""Durable content-failure counters kept apart from immutable request proofs."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Mapping
from typing import Literal

from pydantic import Field, model_validator

from engine.schemas.contracts import (
    FrozenModel,
    ItemRecord,
    ItemStatus,
    canonical_hash,
    canonical_json_bytes,
    strict_json_loads,
)
from engine.services import state
from engine.services.atomic import IdentityMismatch, safe_id

CONTENT_FAILURE_LIMIT = 10
CONTENT_FAILURE_BUDGET = 60
IDENTICAL_CONTENT_FAILURE_LIMIT = 3
MERGED_CONTENT_FAILURE_LIMIT = 3


class FailureGeneration(FrozenModel):
    item_id: str = Field(min_length=1)
    stage: Literal["translate", "review"]
    generation: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_hash: str | None = None
    events: tuple[str, ...] = Field(default=(), max_length=CONTENT_FAILURE_BUDGET)
    fingerprints: tuple[str, ...] = Field(default=(), max_length=CONTENT_FAILURE_BUDGET)
    merged_events: tuple[str, ...] = Field(default=(), max_length=CONTENT_FAILURE_BUDGET)
    singleton_events: tuple[str, ...] = Field(default=(), max_length=CONTENT_FAILURE_BUDGET)
    merged_fingerprints: tuple[str, ...] = Field(default=(), max_length=CONTENT_FAILURE_BUDGET)
    singleton_fingerprints: tuple[str, ...] = Field(default=(), max_length=CONTENT_FAILURE_BUDGET)

    @model_validator(mode="after")
    def validate_fingerprints(self) -> FailureGeneration:
        if len(self.fingerprints) > len(self.events):
            raise ValueError("failure fingerprints cannot outnumber events")
        if set(self.merged_events) & set(self.singleton_events) or not {
            *self.merged_events,
            *self.singleton_events,
        }.issubset(self.events):
            raise ValueError("failure retry modes must classify existing events without overlap")
        classified = Counter((*self.merged_fingerprints, *self.singleton_fingerprints))
        if classified - Counter(self.fingerprints):
            raise ValueError("failure retry modes must classify existing fingerprints")
        return self


class FailureLedger(FrozenModel):
    format: Literal["epubox-content-failures-1"] = "epubox-content-failures-1"
    item_id: str = Field(min_length=1)
    entries: tuple[FailureGeneration, ...] = ()


def failure_count(journal, record: ItemRecord, stage: str) -> int:
    member = journal.session.index.members_by_id[record.item_id]
    generation = _generation(record, stage, canonical_hash(member))
    ledger = _read(journal, record.item_id)
    entry = next(
        (
            value
            for value in ledger.entries
            if value.item_id == record.item_id and value.stage == stage and value.generation == generation
        ),
        None,
    )
    return len(entry.events) if entry is not None else 0


def record_content_failure(journal, record: ItemRecord) -> int:
    """Record one model-content failure and return its generation count.

    A provider/network failure has no persisted response, so it is ignored. The
    event identity makes a recovery replay idempotent.
    """
    if record.status != ItemStatus.NEEDS_ATTENTION or record.stage not in {"translate", "review"}:
        return 0
    request = failure_request(journal, record)
    if request is None:
        return failure_count(journal, record, record.stage)
    completed = next(
        (
            (attempt, response)
            for attempt in reversed(request.attempts)
            if (response := journal.store.read_model_response(request.stage, request.request_id, attempt.attempt_id))
            is not None
        ),
        None,
    )
    if completed is None:
        return failure_count(journal, record, record.stage)
    attempt, response = completed
    member = journal.session.index.members_by_id[record.item_id]
    source_hash = canonical_hash(member)
    generation = _generation(record, request.stage, source_hash)
    root_id = _root_item(record.item_id)
    if tree_failure_count(journal, root_id) >= CONTENT_FAILURE_BUDGET:
        return failure_count(journal, record, record.stage)
    event_id = canonical_hash(
        {
            "stage": request.stage,
            "item_id": record.item_id,
            "generation": generation,
            "request_id": request.request_id,
            "attempt_id": attempt.attempt_id,
        }
    )
    ledger = _read(journal, record.item_id)
    entries = list(ledger.entries)
    position = next(
        (
            index
            for index, value in enumerate(entries)
            if value.item_id == record.item_id and value.stage == request.stage and value.generation == generation
        ),
        None,
    )
    current = (
        entries[position]
        if position is not None
        else FailureGeneration(
            item_id=record.item_id,
            stage=request.stage,
            generation=generation,
            source_hash=source_hash,
            target_hash=record.target_hash if request.stage == "review" else None,
        )
    )
    if event_id in current.events:
        return len(current.events)
    if len(current.events) >= CONTENT_FAILURE_BUDGET:
        return len(current.events)
    fingerprint = _failure_fingerprint(record, request.stage, source_hash, response.raw)
    singleton = len(request.item_ids) == 1
    updated = current.model_copy(
        update={
            "events": (*current.events, event_id),
            "merged_events": current.merged_events if singleton else (*current.merged_events, event_id),
            "singleton_events": (*current.singleton_events, event_id) if singleton else current.singleton_events,
            "fingerprints": (
                (*current.fingerprints, fingerprint) if fingerprint is not None else current.fingerprints
            ),
            "merged_fingerprints": (
                current.merged_fingerprints
                if singleton or fingerprint is None
                else (*current.merged_fingerprints, fingerprint)
            ),
            "singleton_fingerprints": (
                (*current.singleton_fingerprints, fingerprint)
                if singleton and fingerprint is not None
                else current.singleton_fingerprints
            ),
        }
    )
    if position is None:
        entries.append(updated)
    else:
        entries[position] = updated
    state.write(
        _path(journal, record.item_id), canonical_json_bytes(ledger.model_copy(update={"entries": tuple(entries)}))
    )
    return len(updated.events)


def failure_request(journal, record: ItemRecord):
    stage = "translate" if record.stage == "translation" else record.stage
    request_id = record.request_id if stage == "translate" else record.checks.get("review_request_id")
    if not isinstance(request_id, str):
        request_id = None
    message = (record.failure or {}).get("message")
    if not isinstance(message, str):
        message = record.checks.get("retry_feedback")
    if request_id is None and isinstance(message, str):
        matched = re.match(r"^(?:translate|review):(tx-[^:]+):", message)
        request_id = matched.group(1) if matched is not None else None
    if request_id is not None:
        request = journal._requests.get(request_id)
        if request is not None and getattr(request, "stage", stage) == stage and record.item_id in request.item_ids:
            return request
    candidates = tuple(
        request
        for request in journal._requests.values()
        if getattr(request, "stage", stage) == stage and record.item_id in request.item_ids
    )
    return candidates[0] if len(candidates) == 1 else None


def _failure_fingerprint(record: ItemRecord, stage: str, source_hash: str, raw: str) -> str | None:
    target = _response_target(record, stage, raw)
    if target is None:
        return None
    failure = record.failure or {}
    return canonical_hash(
        {
            "source_hash": source_hash,
            "target_hash": canonical_hash(target),
            "error": {
                "stage": failure.get("stage"),
                "code": failure.get("code"),
                "message": _stable_failure_message(stage, failure.get("message")),
            },
        }
    )


def _stable_failure_message(stage: str, message: object) -> object:
    if stage == "review" and isinstance(message, str):
        return re.sub(r"^review:tx-[^:]+:\s*", "", message)
    return message


def _response_target(record: ItemRecord, stage: str, raw: str) -> str | None:
    try:
        payload = strict_json_loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    items = payload.get("items")
    if not isinstance(items, list):
        return None
    item = next(
        (value for value in items if isinstance(value, dict) and value.get("item_id") == record.item_id),
        None,
    )
    if item is None:
        return None
    if "decode_error" in item:
        return None
    if stage == "review" and item.get("decision") == "needs_attention":
        return None
    target = item.get("target")
    if isinstance(target, str) and target:
        return target
    if stage == "review" and record.target_projection is not None:
        return record.target_projection
    return None


def _generation(record: ItemRecord, stage: str, source_hash: str) -> str:
    return canonical_hash(
        {"source_hash": source_hash, "target_hash": record.target_hash if stage == "review" else None}
    )


def _read(journal, item_id: str) -> FailureLedger:
    root_id = _root_item(item_id)
    path = _path(journal, root_id)
    if not state.exists(path):
        return FailureLedger(item_id=root_id)
    try:
        raw = strict_json_loads(state.read(path))
        raw_entries = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(raw, dict) or not isinstance(raw_entries, list):
            raise TypeError("failure ledger must contain an entries array")
        entries = [
            entry | {"item_id": root_id} if isinstance(entry, dict) and "item_id" not in entry else entry
            for entry in raw_entries
        ]
        value = FailureLedger.model_validate(raw | {"entries": entries})
    except Exception as error:
        raise IdentityMismatch(f"invalid content failure ledger for {item_id}: {error}") from error
    if value.item_id != root_id or any(_root_item(entry.item_id) != root_id for entry in value.entries):
        raise IdentityMismatch("content failure ledger ownership changed")
    return value


def _path(journal, item_id: str):
    return journal.store.root / "failures" / f"{safe_id(_root_item(item_id))}.json"


def tree_failure_count(journal, root_id: str) -> int:
    ledger = _read(journal, root_id)
    return sum(len(entry.events) for entry in ledger.entries)


def failure_budget_available(journal, item_id: str) -> bool:
    return tree_failure_count(journal, _root_item(item_id)) < CONTENT_FAILURE_BUDGET


def deterministic_retry_available(journal, record: ItemRecord, stage: str) -> bool:
    """Allow bounded correction attempts for one exact source/target/error result.

    Legacy ledgers have no fingerprints and continue to obey their existing
    generation and tree limits without resetting historical counts.
    """
    message = (record.failure or {}).get("message")
    if (
        stage == "review"
        and isinstance(message, str)
        and re.match(r"^(?:review:tx-[^:]+: )?target decoding failed:", message)
    ):
        # Older ledgers fingerprinted the input draft when a replacement failed
        # decoding. Those fingerprints cannot prove identical model results.
        # The ten-response generation limit and root budget still apply.
        return True
    entry = _entry(journal, record, stage)
    if entry is None or not entry.fingerprints:
        return True
    request = failure_request(journal, record)
    merged_events, singleton_events = _mode_events(entry, request)
    singleton = bool(singleton_events) or len(merged_events) >= MERGED_CONTENT_FAILURE_LIMIT
    fingerprints = _mode_fingerprints(entry, request, singleton=singleton)
    if not fingerprints:
        return True
    latest = fingerprints[-1]
    return Counter(fingerprints)[latest] < IDENTICAL_CONTENT_FAILURE_LIMIT


def isolate_retry(journal, record: ItemRecord) -> bool:
    """Keep a singleton singleton, or split a merged content failure after three completed responses."""
    if record.stage not in {"translate", "review"}:
        return False
    entry = _entry(journal, record, record.stage)
    if entry is None:
        return False
    merged, singleton = _mode_events(entry, failure_request(journal, record))
    return bool(singleton) or len(merged) >= MERGED_CONTENT_FAILURE_LIMIT


def _entry(journal, record: ItemRecord, stage: str) -> FailureGeneration | None:
    member = journal.session.index.members_by_id[record.item_id]
    generation = _generation(record, stage, canonical_hash(member))
    return next(
        (
            value
            for value in _read(journal, record.item_id).entries
            if value.item_id == record.item_id and value.stage == stage and value.generation == generation
        ),
        None,
    )


def _mode_events(entry: FailureGeneration, request) -> tuple[tuple[str, ...], tuple[str, ...]]:
    merged = entry.merged_events
    singleton = entry.singleton_events
    classified = {*merged, *singleton}
    legacy = tuple(event for event in entry.events if event not in classified)
    # Pre-classification ledgers cannot prove whether a response came from a
    # merged or singleton request. Count that history toward the merge limit so
    # an old exhausted checkpoint still isolates, without consuming the new
    # singleton retry budget.
    merged = (*legacy, *merged)
    return merged, singleton


def _mode_fingerprints(entry: FailureGeneration, request, *, singleton: bool) -> tuple[str, ...]:
    merged = entry.merged_fingerprints
    singles = entry.singleton_fingerprints
    classified = Counter((*merged, *singles))
    remaining = Counter(entry.fingerprints) - classified
    legacy = tuple(fingerprint for fingerprint in entry.fingerprints for _ in range(remaining.pop(fingerprint, 0)))
    # Legacy fingerprints follow legacy events into the merged phase. They must
    # not make the first classified singleton response look pre-exhausted.
    merged = (*legacy, *merged)
    return singles if singleton else merged


def content_retry_available(journal, record: ItemRecord) -> bool:
    return (
        record.stage in {"translate", "review"}
        and 0 < failure_count(journal, record, record.stage) < CONTENT_FAILURE_LIMIT
        and failure_budget_available(journal, record.item_id)
        and deterministic_retry_available(journal, record, record.stage)
    )


def retryable_units(
    journal,
    unit_members: Mapping[str, tuple[str, ...]],
    records: Mapping[str, ItemRecord],
    unit_ids: Collection[str],
) -> set[str]:
    """Keep only units whose every failed member is below the content threshold."""
    result: set[str] = set()
    for unit_id in unit_ids:
        failed = tuple(
            records[item_id]
            for item_id in unit_members[unit_id]
            if records[item_id].status == ItemStatus.NEEDS_ATTENTION
        )
        if failed and all(content_retry_available(journal, record) for record in failed):
            result.add(unit_id)
    return result


def automatically_reopenable_units(
    journal,
    unit_members: Mapping[str, tuple[str, ...]],
    records: Mapping[str, ItemRecord],
    unit_ids: Collection[str],
) -> set[str]:
    """Exclude exhausted content generations from automatic command re-entry."""
    result: set[str] = set()
    for unit_id in unit_ids:
        failed = tuple(
            records[item_id]
            for item_id in unit_members[unit_id]
            if records[item_id].status == ItemStatus.NEEDS_ATTENTION
        )
        if failed and all(
            _obsolete_item_limit_failure(journal, record)
            or (
                failure_count(journal, record, record.stage) < CONTENT_FAILURE_LIMIT
                and failure_budget_available(journal, record.item_id)
                and deterministic_retry_available(journal, record, record.stage)
            )
            for record in failed
        ):
            result.add(unit_id)
    return result


def _obsolete_item_limit_failure(journal, record: ItemRecord) -> bool:
    """Allow one recovery from the old local 256-item body validator.

    The fixed validator cannot emit this error for a request above 256 items.
    Preserve the failure ledger; any subsequent content error uses normal limits.
    """
    message = (record.failure or {}).get("message")
    if not isinstance(message, str) or record.stage not in {"translate", "review"}:
        return False
    match = re.fullmatch(
        r"(translate|review):(tx-[^:]+): (items exceeds 256|items must be an array of at most 256 entries)",
        message,
    )
    if match is None or match.group(1) != record.stage:
        return False
    request = journal._requests.get(match.group(2))
    return (
        request is not None
        and request.stage == record.stage
        and record.item_id in request.item_ids
        and len(request.item_ids) > 256
    )


def _root_item(item_id: str) -> str:
    from engine.services.partition import parent_id

    current = item_id
    while (owner := parent_id(current)) is not None:
        current = owner
    return current


__all__ = [
    "CONTENT_FAILURE_BUDGET",
    "CONTENT_FAILURE_LIMIT",
    "IDENTICAL_CONTENT_FAILURE_LIMIT",
    "MERGED_CONTENT_FAILURE_LIMIT",
    "FailureGeneration",
    "FailureLedger",
    "automatically_reopenable_units",
    "content_retry_available",
    "deterministic_retry_available",
    "failure_budget_available",
    "failure_count",
    "failure_request",
    "isolate_retry",
    "record_content_failure",
    "retryable_units",
    "tree_failure_count",
]
