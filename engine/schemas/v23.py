"""Versioned on-disk contracts for the v2.3 translation engine."""

from __future__ import annotations

import hashlib
import json
import math
from enum import StrEnum
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

DOCUMENT_FORMAT = "epubox-document-1"
UNIT_FORMAT = "epubox-unit-1"
BOOK_FORMAT = "epubox-book-1"
REQUEST_FORMAT = "epubox-request-1"
CHECK_FORMAT = "epubox-check-1"
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_JSON_DEPTH = 64


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ItemStatus(StrEnum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    CANDIDATE = "candidate"
    LOCAL_VALID = "local_valid"
    REVIEWED = "reviewed"
    RETRY_WAIT = "retry_wait"
    NEEDS_ATTENTION = "needs_attention"
    BLOCKED_DEPENDENCY = "blocked_dependency"


class ResourceRecord(FrozenModel):
    path: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    source_sha256: str = Field(min_length=1)
    properties: tuple[str, ...] = ()
    linear: bool | None = None


class NodeRecord(FrozenModel):
    node_key: str = Field(min_length=1)
    element_path: tuple[int, ...]
    kind: str = "element"
    qname: str = Field(min_length=1)


class SlotRange(FrozenModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    owner_kind: Literal["unit", "protected", "whitespace", "out_of_scope"]
    owner_unit_id: str | None = None

    @model_validator(mode="after")
    def validate_range(self) -> SlotRange:
        if self.end < self.start:
            raise ValueError("slot range end must not precede start")
        if self.owner_kind == "unit" and not self.owner_unit_id:
            raise ValueError("unit-owned ranges require owner_unit_id")
        if self.owner_kind != "unit" and self.owner_unit_id is not None:
            raise ValueError("only unit-owned ranges may name owner_unit_id")
        return self


class SourceSlot(FrozenModel):
    slot_id: str = Field(min_length=1)
    node_key: str = Field(min_length=1)
    field: Literal["text", "tail", "attribute"]
    source_value: str
    ranges: tuple[SlotRange, ...] = ()
    owner_kind: Literal["unit", "protected", "whitespace", "out_of_scope"] | None = None
    owner_unit_id: str | None = None
    attribute_name: str | None = None

    @model_validator(mode="after")
    def validate_attribute(self) -> SourceSlot:
        if (self.field == "attribute") != (self.attribute_name is not None):
            raise ValueError("attribute slots require attribute_name, other slots forbid it")
        if self.owner_kind == "unit" and not self.owner_unit_id:
            raise ValueError("unit-owned slots require owner_unit_id")
        if self.owner_kind != "unit" and self.owner_unit_id is not None:
            raise ValueError("only unit-owned slots may name owner_unit_id")
        if not self.ranges:
            if self.source_value:
                raise ValueError("non-empty source slots require complete ownership ranges")
            return self
        expected_start = 0
        for interval in self.ranges:
            if interval.start != expected_start:
                raise ValueError("source slot ranges must be ordered, continuous, and non-overlapping")
            if interval.end > len(self.source_value):
                raise ValueError("source slot range exceeds source_value")
            if interval.end == interval.start and self.source_value:
                raise ValueError("non-empty source slots cannot contain empty ownership ranges")
            expected_start = interval.end
        if expected_start != len(self.source_value):
            raise ValueError("source slot ranges must cover the complete source_value")
        if len(self.ranges) == 1:
            interval = self.ranges[0]
            if self.owner_kind != interval.owner_kind or self.owner_unit_id != interval.owner_unit_id:
                raise ValueError("single-range slot ownership summary must match its range")
        elif self.owner_kind is not None or self.owner_unit_id is not None:
            raise ValueError("split slots cannot declare a single ownership summary")
        return self


class RegistryEntry(FrozenModel):
    ref_id: str = Field(min_length=1)
    kind: Literal["g", "x", "b"]
    source_node_key: str = Field(min_length=1)
    parent_ref: str = Field(min_length=1)
    movement: Literal["same_parent", "locked", "fixed"] = "locked"
    reorder_allowed: bool = False
    fixed_order: tuple[str, ...] = ()
    source_text: str = ""
    hints: dict[str, str] = Field(default_factory=dict)
    boundary_type: str | None = None


class Unit(FrozenModel):
    unit_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    source_projection: str
    node_key: str = Field(min_length=1)
    slot_ids: tuple[str, ...]
    registry: dict[str, RegistryEntry] = Field(default_factory=dict)
    context: dict[str, str] = Field(default_factory=dict)
    terms: tuple[dict[str, JsonValue], ...] = ()
    checks: tuple[str, ...] = ()
    region: dict[str, JsonValue] = Field(default_factory=dict)
    logical_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_registry(self) -> Unit:
        if len(set(self.slot_ids)) != len(self.slot_ids):
            raise ValueError("Unit slot_ids must be unique")
        if any(key != entry.ref_id for key, entry in self.registry.items()):
            raise ValueError("registry keys must match ref_id")
        return self


class DocumentPlan(FrozenModel):
    format: Literal["epubox-document-1"] = DOCUMENT_FORMAT
    document_id: str = Field(min_length=1)
    source_hash: str = Field(min_length=1)
    resource: ResourceRecord
    adapter_version: str = Field(min_length=1)
    extractor_version: str = Field(min_length=1)
    source_markup: str
    nodes: dict[str, NodeRecord] = Field(default_factory=dict)
    source_slots: dict[str, SourceSlot] = Field(default_factory=dict)
    units: tuple[Unit, ...] = ()
    boundaries: tuple[dict[str, JsonValue], ...] = ()
    derived_bindings: tuple[dict[str, JsonValue], ...] = ()
    preparation_issues: tuple[dict[str, JsonValue], ...] = ()

    @model_validator(mode="after")
    def validate_identity_maps(self) -> DocumentPlan:
        if any(key != node.node_key for key, node in self.nodes.items()):
            raise ValueError("node map keys must match node_key")
        if any(key != slot.slot_id for key, slot in self.source_slots.items()):
            raise ValueError("slot map keys must match slot_id")
        if len({node.element_path for node in self.nodes.values()}) != len(self.nodes):
            raise ValueError("node element_path values must be unique")
        if any(unit.document_id != self.document_id for unit in self.units):
            raise ValueError("all units must belong to this document")
        if len({unit.unit_id for unit in self.units}) != len(self.units):
            raise ValueError("unit_id must be unique within a document")
        unit_ids = {unit.unit_id for unit in self.units}
        slot_owners: dict[str, set[str]] = {}
        for slot_id, slot in self.source_slots.items():
            if slot.node_key not in self.nodes:
                raise ValueError(f"source slot references unknown node: {slot_id}")
            owners = {interval.owner_unit_id for interval in slot.ranges if interval.owner_unit_id is not None}
            if not owners.issubset(unit_ids):
                raise ValueError(f"source slot references unknown unit: {slot_id}")
            slot_owners[slot_id] = owners
        for unit in self.units:
            if unit.node_key not in self.nodes:
                raise ValueError(f"unit references unknown node: {unit.unit_id}")
            if not set(unit.slot_ids).issubset(self.source_slots):
                raise ValueError(f"unit references unknown source slot: {unit.unit_id}")
            owned_slots = {slot_id for slot_id, owners in slot_owners.items() if unit.unit_id in owners}
            if set(unit.slot_ids) != owned_slots:
                raise ValueError(f"unit/source slot ownership is not bidirectional: {unit.unit_id}")
            for ref_id, entry in unit.registry.items():
                if entry.source_node_key not in self.nodes:
                    raise ValueError(f"registry entry references unknown source node: {unit.unit_id}/{ref_id}")
                if entry.parent_ref not in self.nodes and entry.parent_ref not in unit.registry:
                    raise ValueError(f"registry entry references unknown parent: {unit.unit_id}/{ref_id}")
        return self


class BookPlan(FrozenModel):
    format: Literal["epubox-book-1"] = BOOK_FORMAT
    source_hash: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    source_epub_version: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    preparation_state: Literal["building", "ready"] = "building"
    resources: dict[str, ResourceRecord] = Field(default_factory=dict)
    reading_order: tuple[str, ...] = ()
    document_hashes: dict[str, str] = Field(default_factory=dict)
    unit_ids: tuple[str, ...] = ()
    unit_documents: dict[str, str] = Field(default_factory=dict)
    required_unit_count: int = Field(ge=0)
    initial_coherence_limits: dict[str, int] = Field(default_factory=dict)
    initial_coherence_windows: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    preparation_issues: tuple[dict[str, JsonValue], ...] = ()
    frozen_config: dict[str, JsonValue] = Field(default_factory=dict)
    output_policy_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_fixed_inventory(self) -> BookPlan:
        if len(set(self.unit_ids)) != len(self.unit_ids):
            raise ValueError("BookPlan unit_ids must be unique")
        if self.required_unit_count != len(self.unit_ids):
            raise ValueError("required_unit_count must match the fixed unit inventory")
        if self.preparation_state == "ready" and set(self.unit_documents) != set(self.unit_ids):
            raise ValueError("ready BookPlan requires a document owner for every unit")
        if self.preparation_state == "ready" and not set(self.unit_documents.values()).issubset(self.document_hashes):
            raise ValueError("ready BookPlan unit owners must name hashed documents")
        if self.preparation_state == "ready" and not self.document_hashes and self.unit_ids:
            raise ValueError("ready BookPlan requires document hashes")
        if any(limit < 0 for limit in self.initial_coherence_limits.values()):
            raise ValueError("initial coherence limits cannot be negative")
        if self.initial_coherence_limits and set(self.initial_coherence_limits) != set(self.document_hashes):
            raise ValueError("initial coherence limits must cover every planned document")
        if self.initial_coherence_windows and set(self.initial_coherence_windows) != set(self.document_hashes):
            raise ValueError("initial coherence windows must cover every planned document")
        if any(len(set(windows)) != len(windows) for windows in self.initial_coherence_windows.values()):
            raise ValueError("initial coherence window ids must be unique per document")
        return self


class Event(FrozenModel):
    kind: Literal["text", "marker"]
    value: str
    virtual: bool = False


class Segment(FrozenModel):
    segment_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    source_start: int = Field(ge=0)
    source_end: int = Field(ge=0)
    source_projection: str
    events: tuple[Event, ...] = ()
    virtual_boundaries: tuple[str, ...] = ()
    segment_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_bounds(self) -> Segment:
        if self.source_end < self.source_start:
            raise ValueError("segment end must not precede start")
        return self


class CutPlan(FrozenModel):
    plan_epoch: int = Field(ge=0)
    plan_hash: str = Field(min_length=1)
    segments: tuple[Segment, ...]

    @model_validator(mode="after")
    def validate_segments(self) -> CutPlan:
        if not self.segments:
            raise ValueError("CutPlan requires at least one segment")
        if len({segment.segment_id for segment in self.segments}) != len(self.segments):
            raise ValueError("segment_id must be unique")
        if len({segment.item_id for segment in self.segments}) != len(self.segments):
            raise ValueError("item_id must be unique")
        return self


class FailureRecord(FrozenModel):
    scope: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    code: str = Field(min_length=1)
    message: str
    request_id: str | None = None
    item_id: str | None = None
    plan_epoch: int = Field(ge=0)
    revision: int = Field(ge=0)
    retry_action: Literal["automatic", "explicit_retry", "repair", "dependency", "none"] = "none"


class ItemRecord(FrozenModel):
    item_id: str = Field(min_length=1)
    segment_id: str = Field(min_length=1)
    stage: str = "translation"
    status: ItemStatus = ItemStatus.PENDING
    target_projection: str | None = None
    target_hash: str | None = None
    checks: dict[str, JsonValue] = Field(default_factory=dict)
    request_id: str | None = None
    attempt_id: str | None = None
    failure: FailureRecord | None = None
    next_action: str | None = None
    inherited_from_revision: int | None = Field(default=None, ge=0)
    attempts: dict[str, int] = Field(default_factory=dict)

    @field_validator("attempts")
    @classmethod
    def validate_attempts(cls, attempts: dict[str, int]) -> dict[str, int]:
        if any(count < 0 for count in attempts.values()):
            raise ValueError("item attempt counters cannot be negative")
        return attempts

    @model_validator(mode="after")
    def validate_target(self) -> ItemRecord:
        if self.target_projection is None and self.target_hash is not None:
            raise ValueError("target_hash requires target_projection")
        if self.target_projection is not None and self.target_hash is None:
            raise ValueError("target_projection requires target_hash")
        return self


class Counters(FrozenModel):
    http_attempts: int = Field(default=0, ge=0)
    translation_attempts: int = Field(default=0, ge=0)
    repair_attempts: int = Field(default=0, ge=0)
    review_attempts: int = Field(default=0, ge=0)
    replan_attempts: int = Field(default=0, ge=0)
    coherence_revision_rounds: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    unit_http_limit: int = Field(default=24, ge=0)


class UnitRecord(FrozenModel):
    format: Literal["epubox-unit-1"] = UNIT_FORMAT
    unit_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    source_hash: str = Field(min_length=1)
    logical_hash: str = Field(min_length=1)
    input_hash: str | None = None
    plan_epoch: int = Field(ge=0)
    record_version: int = Field(default=0, ge=0)
    revision: int = Field(default=0, ge=0)
    cut_plan: CutPlan | None = None
    items: dict[str, ItemRecord] = Field(default_factory=dict)
    candidate: str | None = None
    target_hash: str | None = None
    accepted_revision: int | None = Field(default=None, ge=0)
    accepted_target_hash: str | None = None
    local_checks: dict[str, JsonValue] = Field(default_factory=dict)
    review: dict[str, JsonValue] | None = None
    derived: dict[str, JsonValue] | None = None
    history: tuple[dict[str, JsonValue], ...] = ()
    unresolved_issues: tuple[FailureRecord, ...] = ()
    counters: Counters = Field(default_factory=Counters)
    record_hash: str | None = None

    @model_validator(mode="after")
    def validate_versions(self) -> UnitRecord:
        if self.cut_plan is not None and self.cut_plan.plan_epoch != self.plan_epoch:
            raise ValueError("CutPlan plan_epoch must match UnitRecord plan_epoch")
        if any(key != item.item_id for key, item in self.items.items()):
            raise ValueError("item map keys must match item_id")
        if self.accepted_revision is not None and self.accepted_revision > self.revision:
            raise ValueError("accepted_revision cannot exceed revision")
        if (self.accepted_revision is None) != (self.accepted_target_hash is None):
            raise ValueError("accepted revision and hash must be set together")
        return self


class Usage(FrozenModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    known_cost: float | None = Field(default=None, ge=0)


class Attempt(FrozenModel):
    attempt_id: str = Field(min_length=1)
    affected_items: tuple[str, ...]
    reservation: dict[str, int] = Field(default_factory=dict)
    state: Literal["reserved", "sent", "succeeded", "failed", "unknown"] = "reserved"
    created_at: str = Field(min_length=1)
    sent_at: str | None = None
    finished_at: str | None = None
    usage: Usage | None = None
    error: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class RequestManifest(FrozenModel):
    format: Literal["epubox-request-1"] = REQUEST_FORMAT
    request_id: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    document_id: str | None = None
    unit_ids: tuple[str, ...]
    item_ids: tuple[str, ...]
    plan_epochs: dict[str, int]
    revisions: dict[str, int]
    input_hashes: dict[str, str]
    target_hashes: dict[str, str] = Field(default_factory=dict)
    wire_hash: str = Field(min_length=1)
    attempts: tuple[Attempt, ...] = ()

    @model_validator(mode="after")
    def validate_request(self) -> RequestManifest:
        if len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("request item_ids must be unique")
        if len({attempt.attempt_id for attempt in self.attempts}) != len(self.attempts):
            raise ValueError("attempt_id must be unique within a request")
        return self


class DocumentStatus(FrozenModel):
    format: Literal["epubox-check-1"] = CHECK_FORMAT
    document_id: str = Field(min_length=1)
    candidate_versions: dict[str, int] = Field(default_factory=dict)
    dependency_ids: tuple[str, ...] = ()
    windows: tuple[dict[str, JsonValue], ...] = ()
    summary_hash: str | None = None
    http_limit: int = Field(default=0, ge=0)
    http_attempts: int = Field(default=0, ge=0)
    repair_rounds: int = Field(default=0, ge=0)
    extra_http_limit: int = Field(default=0, ge=0)
    retry_history: tuple[dict[str, JsonValue], ...] = ()
    status: Literal["pending", "blocked_dependency", "valid", "needs_attention"] = "pending"
    checks: dict[str, JsonValue] = Field(default_factory=dict)
    issues: tuple[FailureRecord, ...] = ()


class RunConfig(FrozenModel):
    target_language: str = "zh-Hans"
    model: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    protocol_version: str = "epubox-text-1"
    extractor_version: str = Field(min_length=1)
    max_concurrency: int = Field(default=2, ge=1)
    run_http_limit: int = Field(ge=0)
    coherence_http_limit: int = Field(default=0, ge=0)
    max_context_tokens: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    rpm: int | None = Field(default=None, ge=1)
    tpm: int | None = Field(default=None, ge=1)
    request_timeout_seconds: float | None = Field(default=None, gt=0)
    generation: dict[str, JsonValue] = Field(default_factory=dict)


class RunResult(FrozenModel):
    outcome: Literal["completed", "needs_attention", "paused", "failed"] = Field(
        validation_alias=AliasChoices("outcome", "status"), serialization_alias="status"
    )
    run_id: str = Field(min_length=1)
    execution_state: Literal["running", "draining", "stopped"] = "stopped"
    work_dir: str
    output_path: str | None = None
    output_hash: str | None = Field(
        default=None,
        validation_alias=AliasChoices("output_hash", "output_sha256"),
        serialization_alias="output_sha256",
    )
    report_path: str | None = None
    structural_check: dict[str, JsonValue] | None = None
    semantic_review: dict[str, JsonValue] | None = None
    coherence_check: dict[str, JsonValue] | None = None
    epubcheck: dict[str, JsonValue] | None = None
    reader_check: dict[str, JsonValue] | None = None
    issues: tuple[FailureRecord, ...] = ()

    @property
    def status(self) -> str:
        return self.outcome

    @property
    def output_sha256(self) -> str | None:
        return self.output_hash

    @model_validator(mode="after")
    def validate_completion(self) -> RunResult:
        if self.outcome == "completed" and (not self.output_path or not self.output_hash):
            raise ValueError("completed result requires a real output path and hash")
        if self.outcome != "completed" and (self.output_path is not None or self.output_hash is not None):
            raise ValueError("non-completed result cannot expose an official output")
        return self


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json_depth(value: Any, depth: int = 0) -> int:
    if depth > MAX_JSON_DEPTH:
        raise ValueError(f"JSON nesting exceeds {MAX_JSON_DEPTH}")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            _json_depth(child, depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _json_depth(child, depth + 1)
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise TypeError(f"not a JSON value: {type(value).__name__}")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON numbers are forbidden")
    return depth


def canonical_json_bytes(value: Any, *, max_bytes: int = MAX_JSON_BYTES) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    _json_depth(value)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError(f"JSON exceeds {max_bytes} bytes")
    return encoded


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def compute_input_hash(logical_hash: str, plan_hash: str) -> str:
    return canonical_hash({"logical_hash": logical_hash, "plan_hash": plan_hash})


def strict_json_loads(data: str | bytes, *, max_bytes: int = MAX_JSON_BYTES) -> JsonValue:
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    if len(raw) > max_bytes:
        raise ValueError(f"JSON exceeds {max_bytes} bytes")
    value = json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_constant,
    )
    _json_depth(value)
    return value


def unit_record_hash(record: UnitRecord | dict[str, JsonValue]) -> str:
    data = record.model_dump(mode="json") if isinstance(record, UnitRecord) else dict(record)
    data.pop("record_hash", None)
    return canonical_hash(data)


def is_accepted(record: UnitRecord) -> bool:
    return (
        record.accepted_revision == record.revision
        and record.accepted_target_hash is not None
        and record.accepted_target_hash == record.target_hash
        and record.candidate is not None
        and not record.unresolved_issues
        and record.local_checks.get("passed") is True
        and record.local_checks.get("target_hash") == record.target_hash
        and record.review is not None
        and record.review.get("passed") is True
        and record.review.get("revision") == record.revision
        and record.review.get("input_hash") == record.input_hash
        and record.review.get("target_hash") == record.target_hash
        and bool(record.items)
        and all(item.status == ItemStatus.REVIEWED for item in record.items.values())
    )


unit_is_accepted = is_accepted


__all__ = [
    "Attempt",
    "BookPlan",
    "Counters",
    "CutPlan",
    "DocumentPlan",
    "DocumentStatus",
    "Event",
    "FailureRecord",
    "ItemRecord",
    "ItemStatus",
    "NodeRecord",
    "RegistryEntry",
    "ResourceRecord",
    "RunConfig",
    "RunResult",
    "Segment",
    "SlotRange",
    "SourceSlot",
    "Unit",
    "UnitRecord",
    "Usage",
    "canonical_hash",
    "canonical_json_bytes",
    "compute_input_hash",
    "is_accepted",
    "strict_json_loads",
    "unit_is_accepted",
    "unit_record_hash",
]
