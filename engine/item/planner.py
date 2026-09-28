"""Budget-aware v2.3 Unit planning. Batch membership never changes Unit identity."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise
from typing import Any, Literal

import regex
import tiktoken

from engine.agents.runtime_v23 import request_messages
from engine.item.inline import Event, events_to_projection, parse_projection, validate_projection
from engine.schemas.v23 import CutPlan, DocumentPlan, Segment, Unit, UnitRecord, canonical_hash


class PlanningError(ValueError):
    """A Unit cannot be represented within the configured request limits."""


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    context_tokens: int
    max_input_tokens: int | None = None
    max_output_tokens: int = 2048
    review_output_tokens: int = 768
    safety_margin: int = 256
    translation_overhead: int = 256
    review_overhead: int = 512
    target_ratio: float = 1.6
    max_batch_items: int = 8

    def __post_init__(self) -> None:
        integer_fields = (
            self.context_tokens,
            self.max_output_tokens,
            self.review_output_tokens,
            self.safety_margin,
            self.translation_overhead,
            self.review_overhead,
            self.max_batch_items,
        )
        if any(value < 1 for value in integer_fields) or (
            self.max_input_tokens is not None and self.max_input_tokens < 1
        ):
            raise ValueError("planner token limits must be positive")
        if self.target_ratio <= 0:
            raise ValueError("target_ratio must be positive")
        if self.review_output_tokens > self.max_output_tokens:
            raise ValueError("review output reserve cannot exceed the provider output limit")


def plan_unit(unit: Unit, config: PlannerConfig, epoch: int = 0) -> CutPlan:
    """Create a single Segment when possible, otherwise split one immutable Unit."""
    if epoch < 0:
        raise ValueError("plan epoch must be non-negative")
    validate_projection(unit)
    parsed = parse_projection(unit.source_projection)
    atoms = _atomize(parsed)
    if not atoms or not any(event.kind == "text" and event.value for event in atoms):
        raise PlanningError(f"Unit {unit.unit_id} has no translatable text")
    stacks = _range_stacks(atoms)

    full_events = _compact(atoms)
    full_projection = events_to_projection(full_events)
    if _fits_projection(unit, full_projection, config, f"{unit.unit_id}:e{epoch}:s0"):
        segments = (_make_segment(unit, epoch, 0, 0, len(atoms), full_events),)
        return _make_plan(epoch, segments)

    spans: list[tuple[int, int]] = []
    start = 0
    while start < len(atoms):
        fitting: list[tuple[int, int]] = []
        # ponytail: candidate scoring is quadratic only within one oversized Unit;
        # replace with a prefix-token index if profiling finds book-scale outliers.
        for end in range(start + 1, len(atoms) + 1):
            candidate = _segment_events(atoms, stacks, start, end)
            if _fits_projection(
                unit, events_to_projection(candidate), config, f"{unit.unit_id}:e{epoch}:s{len(spans)}"
            ):
                fitting.append((end, _boundary_score(atoms, end)))
            elif fitting:
                break
        if not fitting:
            raise PlanningError(f"Unit {unit.unit_id} contains an atom that cannot fit the request budget")
        end = _choose_cut(start, fitting)
        spans.append((start, end))
        start = end

    segments = tuple(
        _make_segment(unit, epoch, index, start, end, _segment_events(atoms, stacks, start, end))
        for index, (start, end) in enumerate(spans)
    )
    return _make_plan(epoch, segments)


def merge_segments(unit: Unit, plan: CutPlan, targets: Mapping[str, str] | Sequence[str]) -> str:
    """Merge a complete target set, removing only CutPlan-declared virtual markers."""
    target_map = _target_map(plan, targets)
    merged: list[Event] = []
    for segment in plan.segments:
        try:
            target = target_map[segment.item_id]
        except KeyError as exc:
            raise PlanningError(f"missing target for {segment.item_id}") from exc
        target_events = list(validate_projection(segment.source_projection, target, unit.registry))
        prefix, suffix = _virtual_edges(segment.events)
        if [event.value for event in target_events[: len(prefix)]] != prefix:
            raise PlanningError(f"virtual opening boundary moved in {segment.item_id}")
        if suffix and [event.value for event in target_events[-len(suffix) :]] != suffix:
            raise PlanningError(f"virtual closing boundary moved in {segment.item_id}")
        body_end = len(target_events) - len(suffix) if suffix else len(target_events)
        merged.extend(target_events[len(prefix) : body_end])

    projection = events_to_projection(_compact(merged))
    validate_projection(unit, projection)
    return projection


def batch_request(
    items: Sequence[Any], config: PlannerConfig, *, stage: Literal["translation", "review"] = "translation"
) -> tuple[tuple[Any, ...], ...]:
    """Greedily shrink transport batches while preserving the stable item order."""
    batches: list[tuple[Any, ...]] = []
    current: list[Any] = []
    for item in items:
        if not _fits_batch((*current, item), config, stage):
            if not current:
                raise PlanningError(f"item {_item_id(item)} does not fit a {stage} request")
            batches.append(tuple(current))
            current = []
        if not _fits_batch((item,), config, stage):
            raise PlanningError(f"item {_item_id(item)} does not fit a {stage} request")
        current.append(item)
        if len(current) >= config.max_batch_items:
            batches.append(tuple(current))
            current = []
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def input_hash(unit: Unit, plan: CutPlan) -> str:
    """The only v2.3 input hash composition: logical identity plus current plan."""
    return canonical_hash({"logical_hash": unit.logical_hash, "plan_hash": plan.plan_hash})


def validate_cut_plan(unit: Unit, plan: CutPlan) -> None:
    """Recompute every persisted CutPlan invariant before ready/resume use."""
    validate_projection(unit)
    atoms = _atomize(parse_projection(unit.source_projection))
    stacks = _range_stacks(atoms)
    cursor = 0
    for index, segment in enumerate(plan.segments):
        if segment.source_start != cursor or segment.source_end <= segment.source_start:
            raise PlanningError("CutPlan ranges must be ordered, non-empty, and contiguous")
        if segment.source_end > len(atoms):
            raise PlanningError("CutPlan range exceeds the source event stream")
        expected_events = _segment_events(atoms, stacks, segment.source_start, segment.source_end)
        if segment.events != expected_events:
            raise PlanningError(f"segment events do not match source range: {segment.segment_id}")
        if segment.source_projection != events_to_projection(segment.events):
            raise PlanningError(f"segment projection does not match events: {segment.segment_id}")
        if segment.virtual_boundaries != tuple(event.value for event in segment.events if event.virtual):
            raise PlanningError(f"segment virtual boundaries do not match events: {segment.segment_id}")
        expected = _make_segment(
            unit,
            plan.plan_epoch,
            index,
            segment.source_start,
            segment.source_end,
            expected_events,
        )
        if segment != expected:
            raise PlanningError(f"segment identity or hash is invalid: {segment.segment_id}")
        cursor = segment.source_end
    if cursor != len(atoms):
        raise PlanningError("CutPlan does not cover the complete source event stream")

    expected_plan_hash = canonical_hash(
        {"plan_epoch": plan.plan_epoch, "segments": [segment.model_dump(mode="json") for segment in plan.segments]}
    )
    if plan.plan_hash != expected_plan_hash:
        raise PlanningError("CutPlan plan hash is invalid")
    rebuilt = merge_segments(unit, plan, {segment.item_id: segment.source_projection for segment in plan.segments})
    if rebuilt != unit.source_projection:
        raise PlanningError("CutPlan segments do not rebuild the exact source projection")


def initial_coherence_windows(document: DocumentPlan, records: Mapping[str, UnitRecord]) -> tuple[dict[str, Any], ...]:
    """Build the frozen source relationships that receive the initial check budget."""
    units = [unit for unit in document.units if _coherence_lane(unit) is not None]
    windows: list[dict[str, Any]] = []
    for left, right in pairwise(units):
        left_lane, right_lane = _coherence_lane(left), _coherence_lane(right)
        if left.context.get("section") != right.context.get("section") or left_lane != right_lane:
            continue
        target = _candidate_pair(records.get(left.unit_id), records.get(right.unit_id))
        windows.append(
            {
                "item_id": "w" + canonical_hash([document.document_id, "adjacent", left.unit_id, right.unit_id])[:24],
                "unit_ids": [left.unit_id, right.unit_id],
                "source": [_snippet(left.source_projection, tail=True), _snippet(right.source_projection)],
                "target": target,
            }
        )

    for unit in units:
        record = records.get(unit.unit_id)
        if record is None or record.cut_plan is None:
            continue
        for index, (left, right) in enumerate(zip(record.cut_plan.segments, record.cut_plan.segments[1:])):
            left_item, right_item = record.items.get(left.item_id), record.items.get(right.item_id)
            target = (
                [_snippet(left_item.target_projection, tail=True), _snippet(right_item.target_projection)]
                if left_item is not None
                and right_item is not None
                and left_item.target_projection is not None
                and right_item.target_projection is not None
                else []
            )
            windows.append(
                {
                    "item_id": "w" + canonical_hash([document.document_id, "seam", unit.unit_id, index])[:24],
                    "unit_ids": [unit.unit_id],
                    "source": [_snippet(left.source_projection, tail=True), _snippet(right.source_projection)],
                    "target": target,
                }
            )
    return tuple(windows)


def estimate_request_tokens(
    items: Sequence[Any], config: PlannerConfig, *, stage: Literal["translation", "review"] = "translation"
) -> tuple[int, int]:
    """Return conservative input and output reservations for an actual request shape."""
    payload = _request_payload(items, stage)
    runtime_stage = "translate" if stage == "translation" else "review"
    messages = request_messages(runtime_stage, payload)
    source_tokens = _count_tokens(json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if stage == "translation":
        input_tokens = config.translation_overhead + source_tokens
    else:
        missing_target_reserve = 0
        for item, wire_item in zip(items, payload["items"], strict=True):
            if _target(item):
                continue
            missing_target_reserve += math.ceil(_count_tokens(str(wire_item["source"])) * config.target_ratio)
            binding_tokens = sum(
                _count_tokens(str(binding.get("source", "")))
                for binding in wire_item.get("bindings", ())
                if isinstance(binding, Mapping)
            )
            missing_target_reserve += math.ceil(binding_tokens * max(0.0, config.target_ratio - 1))
        input_tokens = config.review_overhead + source_tokens + missing_target_reserve
    return input_tokens, recommended_output_tokens(items, config, stage=stage)


def recommended_output_tokens(
    items: Sequence[Any], config: PlannerConfig, *, stage: Literal["translation", "review"] = "translation"
) -> int:
    """Return the per-request provider output cap used by planning and runtime."""
    request_id = "r00000000000000000000000000000000"
    if stage == "translation":
        response = {
            "protocol": "epubox-text-1",
            "request_id": request_id,
            "items": [{"item_id": _item_id(item), "target": ""} for item in items],
        }
        envelope = _count_tokens(json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        target = math.ceil(sum(_count_tokens(_projection(item)) for item in items) * config.target_ratio)
        return max(config.max_output_tokens, envelope + target)
    response = {
        "protocol": "epubox-review-1",
        "request_id": request_id,
        "items": [
            {
                "item_id": _item_id(item),
                "base_revision": 0,
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
            for item in items
        ],
    }
    envelope = _count_tokens(json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    complete_target = sum(
        _count_tokens(_target(item))
        if _target(item)
        else math.ceil(_count_tokens(_projection(item)) * config.target_ratio)
        for item in items
    )
    return max(config.max_output_tokens, config.review_output_tokens, envelope + complete_target)


def _fits_projection(unit: Unit, projection: str, config: PlannerConfig, item_id: str) -> bool:
    item = {
        "item_id": item_id,
        "source_projection": projection,
        "context": unit.context,
        "terms": unit.terms,
        "hints": {ref: _request_hint(entry) for ref, entry in unit.registry.items()},
        "constraints": {
            ref: {
                "parent_ref": entry.parent_ref,
                "movement": entry.movement,
                "fixed_order": list(entry.fixed_order),
            }
            for ref, entry in unit.registry.items()
        },
        "applicability": {"terminology": bool(unit.terms), "bindings": bool(unit.registry)},
        "bindings": _segment_bindings(projection, unit.registry),
    }
    return _fits_batch((item,), config, "translation") and _fits_batch((item,), config, "review")


@lru_cache(maxsize=1)
def _planner_tokenizer() -> Any | None:
    try:
        return tiktoken.encoding_for_model("gpt-3.5-turbo")
    except (KeyError, OSError, RuntimeError, ValueError):
        try:
            return tiktoken.get_encoding("cl100k_base")
        except (KeyError, OSError, RuntimeError, ValueError):
            return None


def _count_tokens(text: str) -> int:
    tokenizer = _planner_tokenizer()
    if tokenizer is not None:
        return len(tokenizer.encode(text))
    return max(1, len(text.encode("utf-8")))


def _coherence_lane(unit: Unit) -> tuple[str, ...] | None:
    kind = unit.kind.lower()
    excluded = {
        "attribute",
        "metadata",
        "metadata_title",
        "metadata_description",
        "opf_title",
        "opf_description",
        "head_title",
        "nav",
        "navigation",
    }
    if kind in excluded or unit.region.get("attribute_name"):
        return None
    if kind == "table_cell" or "table" in kind:
        identity = tuple(
            f"{key}={unit.context[key]}"
            for key in ("table", "table_id", "table_group", "group", "row", "row_id")
            if unit.context.get(key)
        )
        return ("table", *identity) if identity else ("table", unit.node_key)
    if "footnote" in kind or kind in {"note", "endnote"}:
        identity = tuple(
            f"{key}={unit.context[key]}"
            for key in ("footnote_group", "note_group", "group", "footnote_id", "note_id")
            if unit.context.get(key)
        )
        return ("footnote", *identity) if identity else ("footnote", unit.node_key)
    return ("narrative", unit.context.get("section", ""))


def _request_hint(entry: Any) -> dict[str, str]:
    hints = dict(entry.hints)
    if entry.kind == "g":
        excerpt = _excerpt(entry.source_text)
        hints.update(
            {
                "excerpt": excerpt,
                "excerpt_truncated": "true" if len(excerpt) < len(entry.source_text) else "false",
            }
        )
    return hints


def _segment_bindings(projection: str, registry: Mapping[str, Any]) -> list[dict[str, str]]:
    ranges: dict[str, list[str]] = {}
    stack: list[str] = []
    footnotes: list[str] = []
    for event in parse_projection(projection):
        if event.kind == "text":
            for ref in stack:
                ranges[ref].append(event.value)
            continue
        edge, ref = event.value[0], event.value[1:]
        if edge == "+" and ref.startswith("g"):
            stack.append(ref)
            ranges.setdefault(ref, [])
        elif edge == "-" and ref.startswith("g"):
            if not stack or stack[-1] != ref:
                raise PlanningError(f"invalid segment binding range: {event.value}")
            stack.pop()
        elif edge == "=" and _field(registry.get(ref), "boundary_type") == "footnote":
            footnotes.append(ref)
    bindings = [{"ref": ref, "source": "".join(text), "target": "".join(text)} for ref, text in ranges.items()]
    bindings.extend(
        {"ref": ref, "source": _excerpt(str(_field(registry.get(ref), "source_text", default=""))), "target": ""}
        for ref in footnotes
    )
    return bindings


def _excerpt(value: str, limit: int = 400) -> str:
    selected: list[str] = []
    size = 0
    for match in regex.finditer(r"\X", value):
        cluster = match.group()
        if size + len(cluster) > limit:
            break
        selected.append(cluster)
        size += len(cluster)
    return "".join(selected)


def _field(value: Any, name: str, *, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _candidate_pair(left: UnitRecord | None, right: UnitRecord | None) -> list[str]:
    if left is None or right is None or left.candidate is None or right.candidate is None:
        return []
    return [_snippet(left.candidate, tail=True), _snippet(right.candidate)]


def _snippet(value: str, *, tail: bool = False, limit: int = 800) -> str:
    return value[-limit:] if tail else value[:limit]


def _fits_batch(items: Sequence[Any], config: PlannerConfig, stage: Literal["translation", "review"]) -> bool:
    input_tokens, output_tokens = estimate_request_tokens(items, config, stage=stage)
    if config.max_input_tokens is not None and input_tokens > config.max_input_tokens:
        return False
    if output_tokens > config.max_output_tokens:
        return False
    return input_tokens + output_tokens + config.safety_margin <= config.context_tokens


def _request_payload(items: Sequence[Any], stage: Literal["translation", "review"]) -> dict[str, Any]:
    protocol = "epubox-text-1" if stage == "translation" else "epubox-review-1"
    payload: dict[str, Any] = {
        "protocol": protocol,
        "request_id": "r00000000000000000000000000000000",
        "items": [_payload(item, stage) for item in items],
    }
    if stage == "translation":
        payload["target_language"] = "zh-Hans"
    return payload


def _payload(item: Any, stage: Literal["translation", "review"]) -> dict[str, Any]:
    if isinstance(item, Mapping):
        data: dict[str, Any] = {
            key: value
            for key, value in item.items()
            if key not in {"source_projection", "target_projection", "unit_id"}
        }
        data.setdefault("item_id", _item_id(item))
        data.setdefault("source", _projection(item))
    else:
        data = {"item_id": _item_id(item), "source": _projection(item)}
        for name in ("context", "terms", "hints", "constraints"):
            value = getattr(item, name, None)
            if value:
                data[name] = value
    if stage == "review":
        data.setdefault("target", _target(item))
        data.setdefault("base_revision", 0)
        data.setdefault(
            "applicability",
            {"terminology": bool(data.get("terms")), "bindings": "⟦" in data["source"]},
        )
        data.setdefault("bindings", ())
    else:
        data.pop("applicability", None)
        data.pop("bindings", None)
    return data


def _projection(item: Any) -> str:
    if isinstance(item, Mapping):
        value = item.get("source_projection", item.get("source", ""))
    else:
        value = getattr(item, "source_projection", getattr(item, "source", ""))
    return str(value)


def _target(item: Any) -> str:
    if isinstance(item, Mapping):
        return str(item.get("target_projection", item.get("target", "")))
    return str(getattr(item, "target_projection", getattr(item, "target", "")))


def _item_id(item: Any) -> str:
    if isinstance(item, Mapping):
        return str(item.get("item_id", item.get("unit_id", "unknown")))
    return str(getattr(item, "item_id", getattr(item, "unit_id", "unknown")))


def _atomize(events: Sequence[Event]) -> tuple[Event, ...]:
    atoms: list[Event] = []
    for event in events:
        if event.kind == "marker":
            atoms.append(event)
        else:
            atoms.extend(Event(kind="text", value=cluster) for cluster in regex.findall(r"\X", event.value))
    return tuple(atoms)


def _range_stacks(events: Sequence[Event]) -> tuple[tuple[str, ...], ...]:
    stacks: list[tuple[str, ...]] = []
    stack: list[str] = []
    for event in events:
        stacks.append(tuple(stack))
        if event.kind != "marker":
            continue
        marker = event.value
        if marker.startswith(("+g", "+b")):
            stack.append(marker[1:])
        elif marker.startswith(("-g", "-b")):
            if not stack or stack[-1] != marker[1:]:
                raise PlanningError(f"invalid source range stack at {marker}")
            stack.pop()
    stacks.append(tuple(stack))
    if stack:
        raise PlanningError(f"unclosed source range {stack[-1]}")
    return tuple(stacks)


def _segment_events(
    atoms: Sequence[Event], stacks: Sequence[tuple[str, ...]], start: int, end: int
) -> tuple[Event, ...]:
    prefix = [Event(kind="marker", value=f"+{ref}", virtual=True) for ref in stacks[start]]
    suffix = [Event(kind="marker", value=f"-{ref}", virtual=True) for ref in reversed(stacks[end])]
    return _compact((*prefix, *atoms[start:end], *suffix))


def _compact(events: Sequence[Event]) -> tuple[Event, ...]:
    compact: list[Event] = []
    for event in events:
        if event.kind == "text" and compact and compact[-1].kind == "text" and compact[-1].virtual == event.virtual:
            compact[-1] = Event(kind="text", value=compact[-1].value + event.value, virtual=event.virtual)
        elif event.kind != "text" or event.value:
            compact.append(event)
    return tuple(compact)


def _boundary_score(atoms: Sequence[Event], end: int) -> int:
    if end >= len(atoms):
        return 4
    previous = atoms[end - 1]
    if previous.kind != "text":
        return 0
    char = previous.value
    before = "".join(event.value for event in atoms[max(0, end - 16) : end] if event.kind == "text")
    after = atoms[end].value if atoms[end].kind == "text" else ""
    if char in ".!?。！？" and not _false_sentence_boundary(before, after):
        return 3
    if char in ",;:，；：":
        return 2
    if char.isspace():
        return 1
    return 0


def _false_sentence_boundary(before: str, after: str) -> bool:
    if before.endswith(".") and before[:-1].endswith(("Mr", "Mrs", "Ms", "Dr", "Prof", "e.g", "i.e")):
        return True
    if len(before) >= 2 and before[-2].isdigit() and after[:1].isdigit():
        return True
    return bool(regex.search(r"(?:\b[A-Za-z]\.){2,}$", before))


def _choose_cut(start: int, fitting: Sequence[tuple[int, int]]) -> int:
    farthest = fitting[-1][0]
    threshold = start + max(1, (farthest - start) * 3 // 5)
    preferred = [candidate for candidate in fitting if candidate[0] >= threshold and candidate[1] > 0]
    if not preferred:
        return farthest
    best_score = max(score for _, score in preferred)
    return max(end for end, score in preferred if score == best_score)


def _make_segment(unit: Unit, epoch: int, index: int, start: int, end: int, events: Sequence[Event]) -> Segment:
    segment_id = f"{unit.unit_id}:e{epoch}:s{index}"
    item_id = segment_id
    projection = events_to_projection(events)
    virtual = tuple(event.value for event in events if event.virtual)
    data = {
        "segment_id": segment_id,
        "item_id": item_id,
        "source_start": start,
        "source_end": end,
        "source_projection": projection,
        "events": [event.model_dump(mode="json") for event in events],
        "virtual_boundaries": virtual,
    }
    return Segment(**data, segment_hash=canonical_hash(data))


def _make_plan(epoch: int, segments: tuple[Segment, ...]) -> CutPlan:
    plan_hash = canonical_hash(
        {"plan_epoch": epoch, "segments": [segment.model_dump(mode="json") for segment in segments]}
    )
    return CutPlan(plan_epoch=epoch, plan_hash=plan_hash, segments=segments)


def _target_map(plan: CutPlan, targets: Mapping[str, str] | Sequence[str]) -> dict[str, str]:
    if isinstance(targets, Mapping):
        result: dict[str, str] = {}
        for segment in plan.segments:
            value = targets.get(segment.item_id, targets.get(segment.segment_id))
            if value is not None:
                result[segment.item_id] = value
        return result
    if len(targets) != len(plan.segments):
        raise PlanningError("target sequence length does not match CutPlan")
    return {segment.item_id: target for segment, target in zip(plan.segments, targets, strict=True)}


def _virtual_edges(events: Sequence[Event]) -> tuple[list[str], list[str]]:
    prefix: list[str] = []
    for event in events:
        if not event.virtual:
            break
        prefix.append(event.value)
    suffix: list[str] = []
    for event in reversed(events):
        if not event.virtual:
            break
        suffix.append(event.value)
    suffix.reverse()
    return prefix, suffix
