"""The single v2.5 translation and review executor."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from engine.item.inline import (
    Event,
    events_to_projection,
    parse_projection,
    validate_projection,
)
from engine.item.planner import (
    MAX_SOURCE_TOKENS,
    PlannerConfig,
)
from engine.schemas.contracts import (
    DocumentPlan,
    FrozenTerm,
    ItemRecord,
    ItemStatus,
    JsonValue,
    Segment,
    SourceRef,
    SourceTextView,
    Unit,
    UnitRecord,
    canonical_hash,
)
from engine.services import state
from engine.services.store import RunStore


def _recoverable_failure(
    item: ItemRecord,
) -> tuple[Literal["translate", "review"], str, list[dict[str, JsonValue]]] | None:
    failure = item.failure or {}
    stage = failure.get("stage")
    code = failure.get("code")
    message = failure.get("message")
    if not isinstance(message, str):
        return None
    if stage == "translate":
        if item.target_projection is not None:
            return None
        if code == "model_response_truncated" or (
            code == "request_failed" and message == "model response was truncated"
        ):
            return "translate", "truncated", []
        typed = {"translation_protocol_rejected", "projection_protocol_rejected", "translation_output_oversized"}
        prefixes = (
            "unknown projection marker:",
            "marker inventory mismatch",
            "crossed or unmatched target close marker:",
            "unclosed target reference:",
            "duplicate target reference:",
            "text moved across a protected range or boundary",
        )
        if code in typed or (code == "request_failed" and message.startswith(prefixes)):
            return "translate", "protocol", []
        return None
    if stage != "review" or item.target_projection is None:
        return None
    if code == "replacement_review_limit_exhausted" or (
        code == "request_failed" and message == "replacement review limit exhausted"
    ):
        return "review", "replacement", [{"code": "replacement_limit", "severity": "major", "message": message}]
    if code == "review_needs_attention":
        issues = failure.get("issues")
    elif code == "request_failed":
        try:
            issues = ast.literal_eval(message)
        except (SyntaxError, ValueError):
            return None
    else:
        return None
    normalized = _normalized_review_issues(issues)
    return ("review", "needs_attention", normalized) if normalized else None


def _normalized_review_issues(issues: object) -> list[dict[str, JsonValue]]:
    if not isinstance(issues, list) or not issues:
        return []
    normalized: list[dict[str, JsonValue]] = []
    for issue in issues:
        if (
            not isinstance(issue, dict)
            or not all(isinstance(issue.get(key), str) and issue.get(key) for key in ("code", "severity", "message"))
            or issue.get("severity") not in {"minor", "major", "critical"}
        ):
            return []
        normalized.append({key: issue[key] for key in ("code", "severity", "message")})
    return normalized


def _segment(record: UnitRecord, item_id: str) -> Segment:
    if record.cut_plan is None:
        raise ValueError("Unit has no CutPlan")
    return next(segment for segment in record.cut_plan.segments if segment.item_id == item_id)


def _unit_limit(record: UnitRecord) -> int:
    planned = len(record.cut_plan.segments) if record.cut_plan is not None else 1
    return record.counters.get("unit_http_limit", 24 * max(1, planned))


def _journal_spent_for_unit(store: RunStore, unit_id: str) -> int:
    return sum(
        len(request.attempts)
        for path in state.glob(store.root / "requests", "*.json")
        for request in (store.read_request(path.stem),)
        if any(unit_id in request.item_unit_ids.get(item_id, ()) for item_id in request.item_ids)
    )


def _record_needs_attention(record: UnitRecord) -> bool:
    if record.derived is not None:
        return False
    return (
        record.cut_plan is None
        or any(item.status == ItemStatus.NEEDS_ATTENTION for item in record.items.values())
        or any(issue.get("code") in {"blocking_review", "blocking_coherence"} for issue in record.unresolved_issues)
    )


def _merge_candidate(unit: Unit, record: UnitRecord) -> str:
    if record.cut_plan is None:
        raise ValueError("Unit has no CutPlan")
    merged: list[Event] = []
    for segment in record.cut_plan.segments:
        target = record.items[segment.item_id].target_projection
        if target is None:
            raise ValueError("candidate is missing a Segment target")
        target_events = list(validate_projection(segment.source_projection, target, unit.registry))
        prefix = [value for value in segment.virtual_boundaries if value.startswith("+")]
        suffix = [value for value in segment.virtual_boundaries if value.startswith("-")]
        if [event.value for event in target_events[: len(prefix)]] != prefix:
            raise ValueError("virtual opening boundary moved")
        if suffix and [event.value for event in target_events[-len(suffix) :]] != suffix:
            raise ValueError("virtual closing boundary moved")
        merged.extend(target_events[len(prefix) : len(target_events) - len(suffix) if suffix else None])
    projection = events_to_projection(merged)
    validate_projection(unit, projection)
    return projection


def _term_payload(term: FrozenTerm, role: str) -> dict[str, JsonValue]:
    return {
        "term_id": term.term_id,
        "source": term.source,
        "target": term.target,
        "aliases": list(term.aliases),
        "scope": term.scope.model_dump(mode="json"),
        "mode": term.mode,
        "match_policy": term.match_policy,
        "note": term.note,
        "role": role,
    }


def _ranges(projection: str) -> dict[str, str]:
    result: dict[str, list[str]] = {}
    stack: list[str] = []
    for event in parse_projection(projection):
        if event.kind == "text":
            for ref in stack:
                result[ref].append(event.value)
        elif event.value.startswith("+g"):
            stack.append(event.value[1:])
            result.setdefault(event.value[1:], [])
        elif event.value.startswith("-g"):
            stack.pop()
    return {ref: "".join(text) for ref, text in result.items()}


def _bindings(source: str, target: str, unit: Unit) -> list[dict[str, str]]:
    source_ranges, target_ranges = _ranges(source), _ranges(target)
    bindings = [
        {"ref": ref, "source": text, "target": target_ranges.get(ref, "")} for ref, text in source_ranges.items()
    ]
    bindings.extend(
        {"ref": ref, "source": entry.source_text[:400], "target_context": ""}
        for ref, entry in unit.registry.items()
        if entry.boundary_type == "footnote" and f"⟦={ref}⟧" in source
    )
    return bindings


def _feedback(
    record: UnitRecord,
    item_id: str,
    request_id: str,
    suggestions: Sequence[Mapping[str, Any]],
    rejected: tuple[str, ...],
    document: DocumentPlan,
    unit: Unit,
) -> tuple[dict[str, JsonValue], ...]:
    views = {view_id: document.source_views[view_id] for view_id in unit.source_view_ids}
    feedback: list[dict[str, JsonValue]] = [
        {"kind": "rejected_term_suggestion", "item_id": item_id, "request_id": request_id, "message": message}
        for message in rejected
    ]
    for suggestion in suggestions:
        evidence = suggestion.get("evidence", ())
        source = suggestion.get("source")
        verified = (
            [_verified_feedback_evidence(document, views, source, citation) for citation in evidence]
            if isinstance(source, str) and isinstance(evidence, list)
            else []
        )
        if not evidence or not verified or any(citation is None for citation in verified):
            feedback.append(
                {
                    "kind": "rejected_term_suggestion",
                    "item_id": item_id,
                    "request_id": request_id,
                    "message": "term suggestion evidence does not match the frozen Unit source views",
                }
            )
            continue
        saved = dict(suggestion)
        saved["evidence"] = [citation for citation in verified if citation is not None]
        feedback.append(
            {
                "kind": "term_suggestion",
                "unit_id": record.unit_id,
                "item_id": item_id,
                "request_id": request_id,
                "base_revision": record.revision,
                "suggestion": saved,
            }
        )
    return tuple(feedback)


def _verified_feedback_evidence(
    document: DocumentPlan,
    views: Mapping[str, SourceTextView],
    source: str,
    citation: object,
) -> dict[str, JsonValue] | None:
    if not isinstance(citation, dict):
        return None
    view_id, quote = citation.get("view_id"), citation.get("source_quote")
    if not isinstance(view_id, str) or not isinstance(quote, str) or view_id not in views:
        return None
    view = views[view_id]
    starts = _occurrences(view.text, quote)
    if len(starts) != 1 or not _has_bounded_occurrence(quote, source):
        return None
    refs = _slice_view_refs(document, view.source_refs, starts[0], starts[0] + len(quote), quote)
    if refs is None:
        return None
    result: dict[str, JsonValue] = {
        "view_id": view_id,
        "source_quote": quote,
        "view_hash": view.view_hash,
        "source_refs": refs,
    }
    return result


def _slice_view_refs(
    document: DocumentPlan,
    refs: Sequence[SourceRef],
    start: int,
    end: int,
    expected: str,
) -> list[JsonValue] | None:
    result: list[JsonValue] = []
    rebuilt: list[str] = []
    cursor = 0
    for ref in refs:
        length = ref.end - ref.start
        overlap_start = max(start, cursor)
        overlap_end = min(end, cursor + length)
        if overlap_start < overlap_end:
            source_start = ref.start + overlap_start - cursor
            source_end = ref.start + overlap_end - cursor
            result.append({"slot_id": ref.slot_id, "start": source_start, "end": source_end})
            rebuilt.append(document.source_slots[ref.slot_id].source_value[source_start:source_end])
        cursor += length
    return result if cursor >= end and "".join(rebuilt) == expected else None


def _occurrences(text: str, phrase: str) -> tuple[int, ...]:
    if not phrase:
        return ()
    result: list[int] = []
    start = 0
    while (position := text.find(phrase, start)) >= 0:
        result.append(position)
        start = position + 1
    return tuple(result)


def _has_bounded_occurrence(text: str, phrase: str) -> bool:
    if not phrase:
        return False
    return any(
        (not _word_char(phrase[0]) or start == 0 or not _word_char(text[start - 1]))
        and (
            not _word_char(phrase[-1]) or start + len(phrase) == len(text) or not _word_char(text[start + len(phrase)])
        )
        for start in _occurrences(text, phrase)
    )


def _word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _dedupe_feedback(values: Sequence[dict[str, JsonValue]]) -> tuple[dict[str, JsonValue], ...]:
    unique: dict[str, dict[str, JsonValue]] = {}
    for value in values:
        unique.setdefault(canonical_hash(value), value)
    return tuple(unique.values())


def _planner_config(config: Mapping[str, JsonValue]) -> PlannerConfig:
    context = _positive_int(config.get("context_tokens", config.get("max_context_tokens")), 8192)
    output = _positive_int(config.get("max_output_tokens"), 2048)
    return PlannerConfig(
        context_tokens=context,
        max_source_tokens=_positive_int(config.get("max_source_tokens"), MAX_SOURCE_TOKENS),
        max_input_tokens=_optional_positive_int(config.get("max_input_tokens")),
        max_output_tokens=output,
        review_output_tokens=_positive_int(config.get("review_output_tokens"), min(768, output)),
        safety_margin=_positive_int(config.get("safety_margin"), 256),
        translation_overhead=_positive_int(config.get("translation_overhead"), 256),
        review_overhead=_positive_int(config.get("review_overhead"), 512),
        target_ratio=_positive_number(config.get("target_ratio"), 1.6),
        max_batch_items=_positive_int(config.get("max_batch_items"), 8),
    )


def _nonnegative_int(value: Any, default: int) -> int:
    if value is None:
        return default
    if type(value) is not int or value < 0:
        raise ValueError("configuration value must be a non-negative integer")
    return value


def _positive_int(value: Any, default: int) -> int:
    result = _nonnegative_int(value, default)
    if result < 1:
        raise ValueError("configuration value must be positive")
    return result


def _optional_positive_int(value: Any) -> int | None:
    return None if value is None else _positive_int(value, 1)


def _positive_number(value: Any, default: float) -> float:
    if value is None:
        return default
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError("configuration value must be a positive number")
    return float(value)
