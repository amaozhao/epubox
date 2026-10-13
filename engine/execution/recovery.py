"""Classify structural failures and preserve their retry boundaries."""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence

from engine.schemas.contracts import ItemStatus
from engine.services.failures import failure_request, isolate_retry

STRUCTURAL_RETRY_MAX_ITEMS = 32


def retry_message(record) -> str | None:
    message = (record.failure or {}).get("message")
    if isinstance(message, str):
        return message
    feedback = record.checks.get("retry_feedback")
    return feedback if isinstance(feedback, str) else None


def structural_failure(record) -> bool:
    message = retry_message(record)
    if not isinstance(message, str):
        return False
    if message.endswith(("translation response was truncated", "review response was truncated")):
        return True
    detail = re.sub(r"^(?:translate|review):tx-[^:]+:\s*", "", message)
    return detail.startswith(
        (
            "text moved across",
            "reference moved across",
            "locked reference order changed",
            "plain text and metadata units",
            "marker inventory mismatch",
            "crossed or unmatched",
            "unclosed target",
            "duplicate target",
            "unknown projection marker",
            "unclosed projection marker",
            "literal closing delimiter",
            "unsupported projection escape",
            "dangling projection escape",
            "target must be",
            "target decoding failed:",
            "target contains invalid XML",
            "translation item missing",
            "review item missing",
            "duplicate item_id",
            "response root must",
        )
    )


def retry_partitions(
    journal, selected: Collection[str], *, output_unlimited: bool = False
) -> tuple[tuple[str, ...], ...]:
    wanted = set(selected)
    records = journal.records(tuple(wanted))
    grouped: dict[str, list[str]] = {}
    truncated: set[str] = set()
    for item_id, record in records.items():
        message = retry_message(record)
        if not isinstance(message, str):
            continue
        request = failure_request(journal, record)
        if request is None:
            continue
        request_id = getattr(request, "request_id", None)
        if not isinstance(request_id, str):
            request_id = next(
                (identifier for identifier, candidate in journal._requests.items() if candidate is request), None
            )
        if request_id is None:
            continue
        grouped.setdefault(request_id, []).append(item_id)
        if message.endswith("response was truncated"):
            truncated.add(request_id)
    partitions: list[tuple[str, ...]] = []
    for request_id, failed_ids in grouped.items():
        request = journal._requests[request_id]
        if output_unlimited and not any(
            attempt.metadata.get("output_unlimited") == "true" for attempt in request.attempts
        ):
            continue
        members = tuple(item_id for item_id in request.item_ids if item_id in wanted and item_id in failed_ids)
        if not members:
            continue
        if request_id in truncated:
            size = max(1, len(members) // 2)
            partitions.extend(tuple(members[start : start + size]) for start in range(0, len(members), size))
            continue
        singletons = {item_id for item_id in members if isolate_retry(journal, records[item_id])}
        combined = tuple(item_id for item_id in members if item_id not in singletons)
        if combined:
            partitions.append(combined)
        partitions.extend((item_id,) for item_id in members if item_id in singletons)
    return tuple(partitions)


def selected_partitions(
    partitions: Sequence[Collection[str]], selected: Collection[str]
) -> tuple[tuple[str, ...], ...]:
    wanted = set(selected)
    return tuple(values for partition in partitions if (values := tuple(item for item in partition if item in wanted)))


def retry_items(records) -> set[str]:
    return {
        item_id
        for item_id, record in records.items()
        if record.status != ItemStatus.REVIEWED and retry_message(record)
    }
