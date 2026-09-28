"""Strict, versioned value objects for the v2.5 translation pipeline."""

from __future__ import annotations

import hashlib
import json
import math
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

DOCUMENT_FORMAT = "epubox-document-3"
UNIT_FORMAT = "epubox-unit-3"
BOOK_FORMAT = "epubox-book-3"
PREPARATION_FORMAT = "epubox-preparation-1"
TERM_PLAN_FORMAT = "epubox-term-plan-1"
EXTRACTION_RECORD_FORMAT = "epubox-extraction-record-1"
CANDIDATES_FORMAT = "epubox-candidates-1"
FREEZE_FORMAT = "epubox-freeze-1"
GLOSSARY_FORMAT = "epubox-glossary-1"
REQUEST_FORMAT = "epubox-request-2"
REVIEW_PROTOCOL = "epubox-review-2"
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_JSON_DEPTH = 64


class UnsupportedFormatError(ValueError):
    """Raised when a persisted object or protocol is from another contract."""


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
        if (self.owner_kind == "unit") != (self.owner_unit_id is not None):
            raise ValueError("only unit-owned ranges must name owner_unit_id")
        return self


class SourceSlot(FrozenModel):
    slot_id: str = Field(min_length=1)
    node_key: str = Field(min_length=1)
    field: Literal["text", "tail", "attribute"]
    source_value: str
    ranges: tuple[SlotRange, ...] = ()
    attribute_name: str | None = None

    @model_validator(mode="after")
    def validate_ownership(self) -> SourceSlot:
        if (self.field == "attribute") != (self.attribute_name is not None):
            raise ValueError("attribute slots require attribute_name, other slots forbid it")
        if not self.source_value:
            if any(interval.start != 0 or interval.end != 0 for interval in self.ranges):
                raise ValueError("empty source slots can only contain empty ownership ranges")
            return self
        position = 0
        for interval in self.ranges:
            if interval.start != position or interval.end <= interval.start:
                raise ValueError("source slot ranges must be continuous and non-empty")
            if interval.end > len(self.source_value):
                raise ValueError("source slot range exceeds source_value")
            position = interval.end
        if position != len(self.source_value):
            raise ValueError("source slot ranges must cover source_value")
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


class SourceRef(FrozenModel):
    slot_id: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> SourceRef:
        if self.end <= self.start:
            raise ValueError("source ref end must follow start")
        return self


def source_view_hash_payload(
    *, unit_id: str, document_id: str, text: str, source_refs: tuple[SourceRef, ...], view_kind: str
) -> dict[str, JsonValue]:
    return {
        "unit_id": unit_id,
        "document_id": document_id,
        "text": text,
        "source_refs": [ref.model_dump(mode="json") for ref in source_refs],
        "view_kind": view_kind,
    }


class SourceTextView(FrozenModel):
    view_id: str = Field(min_length=1)
    unit_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    text: str
    view_hash: str = Field(min_length=1)
    source_refs: tuple[SourceRef, ...]
    view_kind: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_hash(self) -> SourceTextView:
        if self.text and not self.source_refs:
            raise ValueError("non-empty SourceTextView requires source_refs")
        expected = canonical_hash(
            source_view_hash_payload(
                unit_id=self.unit_id,
                document_id=self.document_id,
                text=self.text,
                source_refs=self.source_refs,
                view_kind=self.view_kind,
            )
        )
        if self.view_hash != expected:
            raise ValueError("SourceTextView view_hash does not match its canonical source payload")
        return self


class Unit(FrozenModel):
    unit_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    source_projection: str
    node_key: str = Field(min_length=1)
    slot_ids: tuple[str, ...]
    registry: dict[str, RegistryEntry] = Field(default_factory=dict)
    source_view_ids: tuple[str, ...] = ()
    context_view_ids: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    region: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_ids(self) -> Unit:
        if len(set(self.slot_ids)) != len(self.slot_ids):
            raise ValueError("Unit slot_ids must be unique")
        if len(set(self.source_view_ids)) != len(self.source_view_ids):
            raise ValueError("Unit source_view_ids must be unique")
        if len(set(self.context_view_ids)) != len(self.context_view_ids):
            raise ValueError("Unit context_view_ids must be unique")
        if any(key != entry.ref_id for key, entry in self.registry.items()):
            raise ValueError("registry keys must match ref_id")
        return self


class DocumentPlan(FrozenModel):
    format: Literal["epubox-document-3"] = DOCUMENT_FORMAT
    document_id: str = Field(min_length=1)
    source_hash: str = Field(min_length=1)
    resource: ResourceRecord
    adapter_version: str = Field(min_length=1)
    extractor_version: str = Field(min_length=1)
    source_markup: str
    nodes: dict[str, NodeRecord] = Field(default_factory=dict)
    source_slots: dict[str, SourceSlot] = Field(default_factory=dict)
    source_views: dict[str, SourceTextView] = Field(default_factory=dict)
    units: tuple[Unit, ...] = ()
    boundaries: tuple[dict[str, JsonValue], ...] = ()
    derived_bindings: tuple[dict[str, JsonValue], ...] = ()
    preparation_issues: tuple[dict[str, JsonValue], ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> DocumentPlan:
        if any(key != node.node_key for key, node in self.nodes.items()):
            raise ValueError("node map keys must match node_key")
        if any(key != slot.slot_id for key, slot in self.source_slots.items()):
            raise ValueError("source slot map keys must match slot_id")
        special_slots: set[str] = set()
        special_positions: set[tuple[str, int]] = set()
        for boundary in self.boundaries:
            if boundary.get("kind") != "non_element_tail":
                continue
            slot_id = boundary.get("slot_id")
            parent = boundary.get("parent_node_key")
            child_index = boundary.get("child_index")
            slot = self.source_slots.get(slot_id) if isinstance(slot_id, str) else None
            if (
                not isinstance(slot_id, str)
                or slot is None
                or slot.field != "tail"
                or not isinstance(parent, str)
                or slot.node_key != parent
                or type(child_index) is not int
                or child_index < 0
                or slot_id in special_slots
                or (parent, child_index) in special_positions
            ):
                raise ValueError("invalid or duplicate non-element tail boundary")
            special_slots.add(slot_id)
            special_positions.add((parent, child_index))
        ordinary_slots = [slot for slot in self.source_slots.values() if slot.slot_id not in special_slots]
        physical_slots = {(slot.node_key, slot.field, slot.attribute_name) for slot in ordinary_slots}
        if len(physical_slots) != len(ordinary_slots):
            raise ValueError("duplicate physical source slot")
        if any(key != view.view_id for key, view in self.source_views.items()):
            raise ValueError("source view map keys must match view_id")
        if len({node.element_path for node in self.nodes.values()}) != len(self.nodes):
            raise ValueError("node element paths must be unique")
        if len({unit.unit_id for unit in self.units}) != len(self.units):
            raise ValueError("unit_id must be unique within a document")
        units_by_id = {unit.unit_id: unit for unit in self.units}
        unit_ids = set(units_by_id)
        primary_view_owners: dict[str, list[str]] = {view_id: [] for view_id in self.source_views}
        slot_owners: dict[str, set[str]] = {}
        for slot_id, slot in self.source_slots.items():
            if slot.node_key not in self.nodes:
                raise ValueError("source slot references an unknown node")
            owners = {part.owner_unit_id for part in slot.ranges if part.owner_unit_id is not None}
            if not owners.issubset(unit_ids):
                raise ValueError("source slot references an unknown Unit")
            slot_owners[slot_id] = owners
        for view in self.source_views.values():
            if view.document_id != self.document_id or view.unit_id not in unit_ids:
                raise ValueError("source view references an unknown document or unit")
            slot_order = {slot_id: index for index, slot_id in enumerate(units_by_id[view.unit_id].slot_ids)}
            previous_slot = -1
            previous_end = -1
            for ref in view.source_refs:
                slot = self.source_slots.get(ref.slot_id)
                if slot is None or ref.end > len(slot.source_value):
                    raise ValueError("source view references an unknown or out-of-bounds source slot")
                position = slot_order.get(ref.slot_id)
                if (
                    position is None
                    or position < previous_slot
                    or (position == previous_slot and ref.start < previous_end)
                ):
                    raise ValueError("source view refs must follow owned source order without overlap")
                if not any(
                    interval.owner_kind == "unit"
                    and interval.owner_unit_id == view.unit_id
                    and interval.start <= ref.start
                    and ref.end <= interval.end
                    for interval in slot.ranges
                ):
                    raise ValueError("source view ref is not wholly owned by its Unit")
                previous_slot, previous_end = position, ref.end
        view_ids = set(self.source_views)
        for unit in self.units:
            if unit.document_id != self.document_id or unit.node_key not in self.nodes:
                raise ValueError("Unit references an unknown document or node")
            if not set(unit.slot_ids).issubset(self.source_slots):
                raise ValueError("Unit references an unknown source slot")
            owned_slots = {slot_id for slot_id, owners in slot_owners.items() if unit.unit_id in owners}
            if set(unit.slot_ids) != owned_slots:
                raise ValueError("Unit/source slot ownership must be bidirectional")
            if not set(unit.source_view_ids + unit.context_view_ids).issubset(view_ids):
                raise ValueError("Unit references an unknown source view")
            if any(self.source_views[view_id].unit_id != unit.unit_id for view_id in unit.source_view_ids):
                raise ValueError("Unit primary source views must belong to that Unit")
            for view_id in unit.source_view_ids:
                primary_view_owners[view_id].append(unit.unit_id)
            for entry in unit.registry.values():
                if entry.source_node_key not in self.nodes:
                    raise ValueError("registry entry references an unknown source node")
                if entry.parent_ref not in self.nodes and entry.parent_ref not in unit.registry:
                    raise ValueError("registry entry references an unknown parent")
        if any(owners != [self.source_views[view_id].unit_id] for view_id, owners in primary_view_owners.items()):
            raise ValueError("every source view must be registered exactly once by its owner Unit")
        return self


class TermScope(FrozenModel):
    kind: Literal["book", "documents", "units"]
    document_ids: tuple[str, ...] = ()
    unit_ids: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def validate_exact_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        kind = value.get("kind")
        if not isinstance(kind, str):
            return value
        expected = {
            "book": {"kind"},
            "documents": {"kind", "document_ids"},
            "units": {"kind", "unit_ids"},
        }.get(kind)
        if expected is not None and set(value) != expected:
            raise ValueError(f"{kind} scope must contain exactly {sorted(expected)}")
        return value

    @field_validator("document_ids", "unit_ids")
    @classmethod
    def normalize_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value for value in values):
            raise ValueError("scope IDs cannot be empty")
        return tuple(sorted(set(values)))

    @model_validator(mode="after")
    def validate_shape(self) -> TermScope:
        if self.kind == "book" and (self.document_ids or self.unit_ids):
            raise ValueError("book scope cannot name document or Unit IDs")
        if self.kind == "documents" and (not self.document_ids or self.unit_ids):
            raise ValueError("documents scope requires only document_ids")
        if self.kind == "units" and (not self.unit_ids or self.document_ids):
            raise ValueError("units scope requires only unit_ids")
        return self

    @model_serializer(mode="plain")
    def serialize_shape(self) -> dict[str, JsonValue]:
        if self.kind == "book":
            return {"kind": "book"}
        if self.kind == "documents":
            return {"kind": "documents", "document_ids": list(self.document_ids)}
        return {"kind": "units", "unit_ids": list(self.unit_ids)}


class TermEvidence(FrozenModel):
    view_id: str = Field(min_length=1)
    source_quote: str = Field(min_length=1)
    unit_id: str | None = None
    document_id: str | None = None
    source_refs: tuple[SourceRef, ...] = ()
    evidence_check: Literal["pending", "source_matched", "rejected"] = "pending"


class TermRule(FrozenModel):
    term_id: str = Field(min_length=1)
    source: str = Field(min_length=1, max_length=500)
    target: str = Field(min_length=1, max_length=500)
    aliases: tuple[str, ...] = ()
    scope: TermScope
    mode: Literal["required", "preferred", "keep_source"] = "preferred"
    match_policy: Literal["exact", "casefold"] = "exact"
    note: str = Field(default="", max_length=2000)

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, aliases: tuple[str, ...]) -> tuple[str, ...]:
        if any(not alias for alias in aliases):
            raise ValueError("aliases cannot contain empty strings")
        return tuple(sorted(set(aliases)))

    @model_validator(mode="after")
    def validate_keep_source(self) -> TermRule:
        if self.mode == "keep_source" and self.target != self.source:
            raise ValueError("keep_source terms require target to equal source")
        return self


class UserTerm(TermRule):
    origin: Literal["user"] = "user"


class TermCandidate(FrozenModel):
    candidate_id: str = Field(min_length=1)
    extraction_item_id: str = Field(min_length=1)
    source: str = Field(min_length=1, max_length=500)
    target: str = Field(min_length=1, max_length=500)
    category: Literal["term", "person", "organization", "product", "abbreviation", "other"]
    aliases: tuple[str, ...] = ()
    scope_hint: Literal["document", "book"] = "document"
    note: str = Field(default="", max_length=2000)
    evidence: tuple[TermEvidence, ...]
    status: Literal[
        "proposed",
        "adopted_preferred",
        "shadowed_by_user",
        "deferred_conflict",
        "rejected_evidence",
        "rejected_schema",
    ] = "proposed"

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, aliases: tuple[str, ...]) -> tuple[str, ...]:
        if any(not alias for alias in aliases):
            raise ValueError("aliases cannot contain empty strings")
        return tuple(sorted(set(aliases)))

    @model_validator(mode="after")
    def require_evidence(self) -> TermCandidate:
        if not self.evidence:
            raise ValueError("term candidates require source evidence")
        if self.status == "adopted_preferred" and not any(
            evidence.evidence_check == "source_matched"
            and evidence.unit_id
            and evidence.document_id
            and evidence.source_refs
            for evidence in self.evidence
        ):
            raise ValueError("adopted candidates require complete source-matched evidence")
        return self


class ExtractionItem(FrozenModel):
    item_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    view_ids: tuple[str, ...]
    primary_ranges: tuple[dict[str, JsonValue], ...]
    context_refs: tuple[str, ...] = ()
    user_term_ids: tuple[str, ...] = ()
    context_user_term_ids: tuple[str, ...] = ()
    extraction_input_hash: str = Field(min_length=1)
    http_limit: int = Field(default=6, ge=0)

    @field_validator("view_ids", "context_refs", "user_term_ids", "context_user_term_ids")
    @classmethod
    def unique_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("extraction item IDs must be unique")
        return values

    @model_validator(mode="after")
    def require_primary_views(self) -> ExtractionItem:
        if not self.view_ids or not self.primary_ranges:
            raise ValueError("extraction items require primary views and ranges")
        return self


class TermExtractionPlan(FrozenModel):
    format: Literal["epubox-term-plan-1"] = TERM_PLAN_FORMAT
    source_hash: str = Field(min_length=1)
    preparation_hash: str = Field(min_length=1)
    plan_hash: str = Field(min_length=1)
    auto_extract: bool = True
    extraction_http_limit: int = Field(ge=0)
    resolution_group_limit: int = Field(default=20, ge=0)
    items: tuple[ExtractionItem, ...] = ()

    @model_validator(mode="after")
    def validate_items(self) -> TermExtractionPlan:
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("extraction item IDs must be unique")
        if not self.auto_extract and (self.items or self.extraction_http_limit or self.resolution_group_limit):
            raise ValueError("disabled extraction requires an explicit empty, zero-budget plan")
        expected_limit = sum(item.http_limit for item in self.items) + 3 * self.resolution_group_limit
        if self.extraction_http_limit != expected_limit:
            raise ValueError("extraction_http_limit must equal the fixed preparation budget")
        if self.plan_hash != term_plan_hash(self):
            raise ValueError("term plan hash does not match its canonical payload")
        return self


class TermExtractionRecord(FrozenModel):
    format: Literal["epubox-extraction-record-1"] = EXTRACTION_RECORD_FORMAT
    item_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    view_ids: tuple[str, ...]
    extraction_input_hash: str = Field(min_length=1)
    record_version: int = Field(default=0, ge=0)
    status: Literal[
        "pending",
        "in_flight",
        "retry_wait",
        "succeeded",
        "succeeded_with_rejections",
        "failed_exhausted",
        "unplannable",
    ] = "pending"
    candidates: tuple[TermCandidate, ...] = ()
    diagnostics: tuple[dict[str, JsonValue], ...] = ()
    request_ids: tuple[str, ...] = ()
    counters: dict[str, int] = Field(default_factory=dict)

    @field_validator("counters")
    @classmethod
    def validate_counters(cls, counters: dict[str, int]) -> dict[str, int]:
        if any(value < 0 for value in counters.values()):
            raise ValueError("extraction counters cannot be negative")
        return counters

    @model_validator(mode="after")
    def validate_candidate_ownership(self) -> TermExtractionRecord:
        for candidate in self.candidates:
            if candidate.extraction_item_id != self.item_id:
                raise ValueError("candidate belongs to another extraction item")
            if not {evidence.view_id for evidence in candidate.evidence}.issubset(self.view_ids):
                raise ValueError("candidate evidence references an unknown extraction view")
        return self


class CandidatePool(FrozenModel):
    format: Literal["epubox-candidates-1"] = CANDIDATES_FORMAT
    source_hash: str = Field(min_length=1)
    preparation_hash: str = Field(min_length=1)
    term_plan_hash: str = Field(min_length=1)
    record_version: int = Field(default=0, ge=0)
    extraction_status: Literal["open", "closed", "closed_with_gaps", "disabled", "not_required"] = "open"
    candidates: tuple[TermCandidate, ...] = ()
    conflict_groups: tuple[dict[str, JsonValue], ...] = ()
    consumed_response_ids: tuple[str, ...] = ()
    record_hash: str | None = None

    @model_validator(mode="after")
    def validate_candidates(self) -> CandidatePool:
        if len({candidate.candidate_id for candidate in self.candidates}) != len(self.candidates):
            raise ValueError("candidate IDs must be unique")
        if self.extraction_status != "open" and any(candidate.status == "proposed" for candidate in self.candidates):
            raise ValueError("closed candidate pools cannot contain proposed candidates")
        if self.record_hash is not None and self.record_hash != candidate_pool_record_hash(self):
            raise ValueError("candidate pool record_hash does not match its canonical payload")
        return self


class FrozenTerm(TermRule):
    origin: Literal["user", "model_extraction"]
    candidate_ids: tuple[str, ...] = ()
    evidence: tuple[TermEvidence, ...] = ()
    frequency: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_origin(self) -> FrozenTerm:
        if self.origin == "model_extraction":
            if self.mode != "preferred" or not self.candidate_ids:
                raise ValueError("model-extracted terms must be preferred and retain candidate IDs")
            if self.scope.kind == "book":
                raise ValueError("model-extracted terms cannot expand to book scope")
            if self.aliases:
                raise ValueError("model-extracted aliases require a separately verified alias evidence contract")
            if not any(
                evidence.evidence_check == "source_matched"
                and evidence.unit_id
                and evidence.document_id
                and evidence.source_refs
                for evidence in self.evidence
            ):
                raise ValueError("model-extracted terms require complete source-matched evidence")
        return self


class GlossaryPayload(FrozenModel):
    source_hash: str = Field(min_length=1)
    freeze_id: str = Field(min_length=1)
    extraction_config_hash: str = Field(min_length=1)
    user_terms_hash: str = Field(min_length=1)
    extraction_status: Literal["closed", "closed_with_gaps", "disabled", "not_required"]
    warnings: tuple[str, ...] = ()
    terms: tuple[FrozenTerm, ...] = ()

    @model_validator(mode="after")
    def validate_terms(self) -> GlossaryPayload:
        if len({term.term_id for term in self.terms}) != len(self.terms):
            raise ValueError("frozen term IDs must be unique")
        if not self.terms and not self.warnings:
            raise ValueError("an empty glossary requires an explicit reason")
        return self


class FreezeIntent(FrozenModel):
    format: Literal["epubox-freeze-1"] = FREEZE_FORMAT
    freeze_id: str = Field(min_length=1)
    source_hash: str = Field(min_length=1)
    preparation_hash: str = Field(min_length=1)
    term_plan_hash: str = Field(min_length=1)
    candidate_pool_hash: str = Field(min_length=1)
    user_terms_hash: str = Field(min_length=1)
    rules_hash: str = Field(min_length=1)
    coverage: dict[str, JsonValue] = Field(default_factory=dict)
    snapshot_payload: GlossaryPayload

    @model_validator(mode="after")
    def validate_payload(self) -> FreezeIntent:
        if self.snapshot_payload.freeze_id != self.freeze_id or self.snapshot_payload.source_hash != self.source_hash:
            raise ValueError("freeze identity must match snapshot payload")
        if self.snapshot_payload.user_terms_hash != self.user_terms_hash:
            raise ValueError("freeze user terms hash must match snapshot payload")
        if self.rules_hash != glossary_rules_hash(self.snapshot_payload.terms):
            raise ValueError("rules_hash does not match the frozen prompt rules")
        return self


class GlossarySnapshot(GlossaryPayload):
    format: Literal["epubox-glossary-1"] = GLOSSARY_FORMAT


class TermPreparation(FrozenModel):
    plan: TermExtractionPlan
    records: dict[str, TermExtractionRecord] = Field(default_factory=dict)
    candidates: CandidatePool
    freeze: FreezeIntent | None = None
    paused: bool = False

    @model_validator(mode="after")
    def validate_identity(self) -> TermPreparation:
        plan_ids = {item.item_id for item in self.plan.items}
        if not set(self.records).issubset(plan_ids):
            raise ValueError("term preparation contains an unknown extraction item")
        if any(key != record.item_id for key, record in self.records.items()):
            raise ValueError("extraction record keys must match item_id")
        if self.candidates.term_plan_hash != self.plan.plan_hash:
            raise ValueError("candidate pool does not belong to the extraction plan")
        if self.candidates.source_hash != self.plan.source_hash:
            raise ValueError("candidate pool source does not match the extraction plan")
        if self.candidates.preparation_hash != self.plan.preparation_hash:
            raise ValueError("candidate pool preparation does not match the extraction plan")
        if self.candidates.extraction_status == "disabled" and (
            self.plan.auto_extract
            or self.plan.items
            or self.records
            or self.candidates.candidates
            or self.candidates.conflict_groups
            or self.candidates.consumed_response_ids
        ):
            raise ValueError("disabled extraction requires an empty disabled plan and pool")
        if self.candidates.extraction_status == "not_required" and (
            not self.plan.auto_extract
            or self.plan.items
            or self.records
            or self.candidates.candidates
            or self.candidates.conflict_groups
            or self.candidates.consumed_response_ids
        ):
            raise ValueError("not_required extraction requires an enabled plan with no source views")
        if self.freeze is not None:
            terminal = {"succeeded", "succeeded_with_rejections", "failed_exhausted", "unplannable"}
            if self.paused:
                raise ValueError("paused term preparation cannot be frozen")
            if self.candidates.extraction_status == "open":
                raise ValueError("open candidate pools cannot be frozen")
            if set(self.records) != plan_ids or any(record.status not in terminal for record in self.records.values()):
                raise ValueError("freeze requires a terminal record for every extraction item")
            if self.freeze.source_hash != self.plan.source_hash:
                raise ValueError("freeze source does not match the extraction plan")
            if self.freeze.preparation_hash != self.plan.preparation_hash:
                raise ValueError("freeze preparation does not match the extraction plan")
            if self.freeze.term_plan_hash != self.plan.plan_hash:
                raise ValueError("freeze term plan does not match the extraction plan")
            if self.freeze.candidate_pool_hash != canonical_hash(self.candidates):
                raise ValueError("freeze candidate pool hash does not match the current pool")
            if self.freeze.snapshot_payload.extraction_status != self.candidates.extraction_status:
                raise ValueError("freeze snapshot status does not match the candidate pool")
            pool_candidates = {candidate.candidate_id: candidate for candidate in self.candidates.candidates}
            record_candidates = {
                candidate.candidate_id: candidate
                for record in self.records.values()
                for candidate in record.candidates
            }
            for candidate_id, candidate in pool_candidates.items():
                origin = record_candidates.get(candidate_id)
                if origin is None or candidate.model_dump(exclude={"status"}) != origin.model_dump(exclude={"status"}):
                    raise ValueError("candidate pool changed an extraction response payload")
            for term in self.freeze.snapshot_payload.terms:
                if term.origin != "model_extraction":
                    continue
                adopted: list[TermCandidate] = []
                for candidate_id in term.candidate_ids:
                    candidate = pool_candidates.get(candidate_id)
                    if (
                        candidate is None
                        or candidate_id not in record_candidates
                        or candidate.status != "adopted_preferred"
                        or candidate.source != term.source
                        or candidate.target != term.target
                    ):
                        raise ValueError("frozen model term references a ghost or non-adopted candidate")
                    adopted.append(candidate)
                evidence_hashes = {
                    canonical_hash(evidence) for candidate in adopted for evidence in candidate.evidence
                }
                if any(canonical_hash(evidence) not in evidence_hashes for evidence in term.evidence):
                    raise ValueError("frozen model term evidence does not match its adopted candidates")
                source_documents = {
                    evidence.document_id
                    for candidate in adopted
                    for evidence in candidate.evidence
                    if evidence.evidence_check == "source_matched" and evidence.document_id
                }
                source_units = {
                    evidence.unit_id
                    for candidate in adopted
                    for evidence in candidate.evidence
                    if evidence.evidence_check == "source_matched" and evidence.unit_id
                }
                if term.scope.kind == "documents" and not set(term.scope.document_ids).issubset(source_documents):
                    raise ValueError("model term document scope exceeds adopted source evidence")
                if term.scope.kind == "units" and not set(term.scope.unit_ids).issubset(source_units):
                    raise ValueError("model term Unit scope exceeds adopted source evidence")
        return self


class PreparationPlan(FrozenModel):
    format: Literal["epubox-preparation-1"] = PREPARATION_FORMAT
    state: Literal["parsed_ready"] = "parsed_ready"
    source_hash: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    source_epub_version: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    document_hashes: dict[str, str]
    reading_order: tuple[str, ...] = ()
    unit_documents: dict[str, str] = Field(default_factory=dict)
    user_terms: tuple[UserTerm, ...] = ()
    user_terms_hash: str = Field(min_length=1)
    extraction_config: dict[str, JsonValue] = Field(default_factory=dict)
    translation_config: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_inventory(self) -> PreparationPlan:
        if len(set(self.reading_order)) != len(self.reading_order):
            raise ValueError("reading_order must be unique")
        if not set(self.reading_order).issubset(self.document_hashes):
            raise ValueError("reading_order references an unknown document")
        if not set(self.unit_documents.values()).issubset(self.document_hashes):
            raise ValueError("unit inventory references an unknown document")
        validate_term_scopes(self.user_terms, set(self.document_hashes), set(self.unit_documents))
        if self.user_terms_hash != canonical_hash(self.user_terms):
            raise ValueError("user_terms_hash does not match normalized user terms")
        return self


class Segment(FrozenModel):
    segment_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    source_start: int = Field(ge=0)
    source_end: int = Field(ge=0)
    source_projection: str
    selected_term_ids: tuple[str, ...] = ()
    term_applicability: dict[str, Literal["target", "context"]] = Field(default_factory=dict)
    terms_hash: str = Field(min_length=1)
    context_hash: str = Field(min_length=1)
    virtual_boundaries: tuple[str, ...] = ()
    segment_hash: str = Field(min_length=1)

    @field_validator("selected_term_ids")
    @classmethod
    def normalize_term_ids(cls, term_ids: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(term_ids)))

    @model_validator(mode="after")
    def validate_segment(self) -> Segment:
        if self.source_end < self.source_start:
            raise ValueError("segment end must not precede start")
        if set(self.term_applicability) != set(self.selected_term_ids):
            raise ValueError("term applicability must cover exactly the selected terms")
        if self.segment_hash != segment_hash(self):
            raise ValueError("Segment segment_hash does not match its canonical payload")
        return self


class CutPlan(FrozenModel):
    plan_epoch: int = Field(ge=0)
    plan_hash: str = Field(min_length=1)
    segments: tuple[Segment, ...]

    @model_validator(mode="after")
    def validate_segments(self) -> CutPlan:
        if not self.segments:
            raise ValueError("CutPlan requires at least one Segment")
        if len({segment.segment_id for segment in self.segments}) != len(self.segments):
            raise ValueError("Segment IDs must be unique")
        if len({segment.item_id for segment in self.segments}) != len(self.segments):
            raise ValueError("item IDs must be unique")
        if self.segments[0].source_start != 0 or any(
            current.source_end != following.source_start
            for current, following in zip(self.segments, self.segments[1:], strict=False)
        ):
            raise ValueError("CutPlan Segment ranges must be ordered and continuous from zero")
        if self.plan_hash != cut_plan_hash(self):
            raise ValueError("CutPlan plan_hash does not match its canonical payload")
        return self


class ItemRecord(FrozenModel):
    item_id: str = Field(min_length=1)
    segment_id: str = Field(min_length=1)
    selected_term_ids: tuple[str, ...] = ()
    term_applicability: dict[str, Literal["target", "context"]] = Field(default_factory=dict)
    terms_hash: str = Field(min_length=1)
    context_hash: str = Field(min_length=1)
    stage: str = "translation"
    status: ItemStatus = ItemStatus.PENDING
    target_projection: str | None = None
    target_hash: str | None = None
    checks: dict[str, JsonValue] = Field(default_factory=dict)
    request_id: str | None = None
    failure: dict[str, JsonValue] | None = None
    next_action: str | None = None

    @field_validator("selected_term_ids")
    @classmethod
    def normalize_term_ids(cls, term_ids: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(term_ids)))

    @model_validator(mode="after")
    def validate_item(self) -> ItemRecord:
        if set(self.term_applicability) != set(self.selected_term_ids):
            raise ValueError("term applicability must cover exactly the selected terms")
        if (self.target_projection is None) != (self.target_hash is None):
            raise ValueError("target projection and hash must be present together")
        if self.target_projection is not None and self.target_hash != canonical_hash(self.target_projection):
            raise ValueError("item target_hash does not match target_projection")
        return self


class UnitRecord(FrozenModel):
    format: Literal["epubox-unit-3"] = UNIT_FORMAT
    unit_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    source_hash: str = Field(min_length=1)
    logical_hash: str | None = None
    input_hash: str | None = None
    plan_epoch: int = Field(default=0, ge=0)
    record_version: int = Field(default=0, ge=0)
    revision: int = Field(default=0, ge=0)
    cut_plan: CutPlan | None = None
    items: dict[str, ItemRecord] = Field(default_factory=dict)
    candidate: str | None = None
    accepted_revision: int | None = Field(default=None, ge=0)
    accepted_target_hash: str | None = None
    local_checks: dict[str, JsonValue] = Field(default_factory=dict)
    review: dict[str, JsonValue] | None = None
    term_feedback: tuple[dict[str, JsonValue], ...] = ()
    unresolved_issues: tuple[dict[str, JsonValue], ...] = ()
    counters: dict[str, int] = Field(default_factory=dict)
    record_hash: str | None = None

    @model_validator(mode="after")
    def validate_state(self) -> UnitRecord:
        if (self.logical_hash is None) != (self.input_hash is None):
            raise ValueError("logical_hash and input_hash must be present together")
        if (self.accepted_revision is None) != (self.accepted_target_hash is None):
            raise ValueError("accepted revision and hash must be present together")
        if self.accepted_revision is not None and self.accepted_revision > self.revision:
            raise ValueError("accepted_revision cannot exceed revision")
        if any(value < 0 for value in self.counters.values()):
            raise ValueError("Unit counters cannot be negative")
        if self.cut_plan is not None and self.cut_plan.plan_epoch != self.plan_epoch:
            raise ValueError("CutPlan plan_epoch must match UnitRecord plan_epoch")
        if self.cut_plan is None and self.input_hash is not None:
            raise ValueError("input_hash requires a CutPlan")
        if (
            self.cut_plan is not None
            and self.logical_hash is not None
            and self.input_hash != compute_input_hash(self.logical_hash, self.cut_plan.plan_hash)
        ):
            raise ValueError("Unit input_hash does not match logical_hash and plan_hash")
        if any(key != item.item_id for key, item in self.items.items()):
            raise ValueError("item map keys must match item_id")
        if self.cut_plan is None and self.items:
            raise ValueError("items require a CutPlan")
        if self.cut_plan is not None:
            planned = {segment.item_id: segment for segment in self.cut_plan.segments}
            if set(self.items) != set(planned):
                raise ValueError("Unit items must correspond one-to-one with CutPlan Segments")
            for item_id, item in self.items.items():
                segment = planned[item_id]
                if (
                    item.segment_id != segment.segment_id
                    or item.selected_term_ids != segment.selected_term_ids
                    or item.term_applicability != segment.term_applicability
                    or item.terms_hash != segment.terms_hash
                    or item.context_hash != segment.context_hash
                ):
                    raise ValueError("Unit item identity must match its CutPlan Segment")
        if self.record_hash is not None and self.record_hash != unit_record_hash(self):
            raise ValueError("Unit record_hash does not match its canonical payload")
        return self


class BookPlan(FrozenModel):
    format: Literal["epubox-book-3"] = BOOK_FORMAT
    source_hash: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    preparation_state: Literal["ready"] = "ready"
    preparation_hash: str = Field(min_length=1)
    glossary_file_sha256: str = Field(min_length=1)
    freeze_file_sha256: str = Field(min_length=1)
    freeze_id: str = Field(min_length=1)
    document_hashes: dict[str, str]
    unit_ids: tuple[str, ...]
    unit_documents: dict[str, str]
    required_unit_count: int = Field(ge=0)
    initial_unit_plans: dict[str, str | None]
    translation_config: dict[str, JsonValue] = Field(default_factory=dict)
    output_policy_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_inventory(self) -> BookPlan:
        if len(self.unit_ids) != len(set(self.unit_ids)) or self.required_unit_count != len(self.unit_ids):
            raise ValueError("BookPlan requires a unique, fixed Unit inventory")
        if set(self.unit_documents) != set(self.unit_ids) or set(self.initial_unit_plans) != set(self.unit_ids):
            raise ValueError("BookPlan must account for every Unit")
        if not set(self.unit_documents.values()).issubset(self.document_hashes):
            raise ValueError("BookPlan Unit owners must name hashed documents")
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

    @field_validator("reservation", mode="before")
    @classmethod
    def validate_reservation(cls, value: object) -> object:
        if not isinstance(value, dict) or any(type(amount) is not int or amount < 0 for amount in value.values()):
            raise ValueError("attempt reservation values must be non-negative integers")
        return value

    @model_validator(mode="after")
    def validate_affected_items(self) -> Attempt:
        if not self.affected_items or len(self.affected_items) != len(set(self.affected_items)):
            raise ValueError("attempt affected_items must be non-empty and unique")
        return self


class RequestManifest(FrozenModel):
    format: Literal["epubox-request-2"] = REQUEST_FORMAT
    request_id: str = Field(min_length=1)
    stage: Literal["terms", "resolution", "translate", "review", "coherence"]
    owner_kind: Literal["extraction_item", "resolution_group", "translation_item"]
    owner_id: str = Field(min_length=1)
    item_ids: tuple[str, ...]
    input_hashes: dict[str, str]
    wire_hash: str = Field(min_length=1)
    record_versions: dict[str, int] = Field(default_factory=dict)
    item_unit_ids: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    unit_document_ids: dict[str, str] = Field(default_factory=dict)
    plan_epochs: dict[str, int] = Field(default_factory=dict)
    revisions: dict[str, int] = Field(default_factory=dict)
    target_hashes: dict[str, str] = Field(default_factory=dict)
    glossary_file_sha256: str | None = None
    freeze_id: str | None = None
    term_ids_by_item: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    terms_hashes: dict[str, str] = Field(default_factory=dict)
    context_hashes: dict[str, str] = Field(default_factory=dict)
    attempts: tuple[Attempt, ...] = ()

    @field_validator("record_versions", "plan_epochs", "revisions")
    @classmethod
    def validate_versions(cls, versions: dict[str, int]) -> dict[str, int]:
        if any(version < 0 for version in versions.values()):
            raise ValueError("request versions cannot be negative")
        return versions

    @field_validator("term_ids_by_item")
    @classmethod
    def normalize_term_ids(cls, values: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        return {item_id: tuple(sorted(set(term_ids))) for item_id, term_ids in values.items()}

    @model_validator(mode="after")
    def validate_owner(self) -> RequestManifest:
        if not self.item_ids or len(self.item_ids) != len(set(self.item_ids)):
            raise ValueError("request item_ids must be non-empty and unique")
        if len({attempt.attempt_id for attempt in self.attempts}) != len(self.attempts):
            raise ValueError("attempt IDs must be unique within a request")
        if set(self.input_hashes) != set(self.item_ids):
            raise ValueError("input_hashes must cover every affected item")
        if any(not value for value in self.input_hashes.values()):
            raise ValueError("input hashes cannot be empty")
        if any(not set(attempt.affected_items).issubset(self.item_ids) for attempt in self.attempts):
            raise ValueError("attempt references an item outside the request")

        expected_owner = {
            "terms": "extraction_item",
            "resolution": "resolution_group",
            "translate": "translation_item",
            "review": "translation_item",
            "coherence": "translation_item",
        }[self.stage]
        if self.owner_kind != expected_owner:
            raise ValueError(f"stage {self.stage} requires owner_kind={expected_owner}")

        unit_maps = (
            self.record_versions,
            self.item_unit_ids,
            self.unit_document_ids,
            self.plan_epochs,
            self.revisions,
            self.target_hashes,
            self.term_ids_by_item,
            self.terms_hashes,
            self.context_hashes,
        )
        if self.stage in {"terms", "resolution"}:
            if any(unit_maps) or self.glossary_file_sha256 is not None or self.freeze_id is not None:
                raise ValueError("term preparation requests cannot claim Unit revision identity")
            if self.owner_kind == "extraction_item" and self.owner_id not in self.item_ids:
                raise ValueError("extraction owner_id must name one affected item")
            if self.owner_kind == "resolution_group" and self.owner_id not in self.item_ids:
                raise ValueError("resolution owner_id must name its affected group")
            return self

        if not self.glossary_file_sha256 or not self.freeze_id:
            raise ValueError("translation requests require frozen glossary identity")
        if self.owner_id not in self.item_ids:
            raise ValueError("translation owner_id must name one affected item")
        item_ids = set(self.item_ids)
        per_item_maps = (self.item_unit_ids, self.term_ids_by_item, self.terms_hashes, self.context_hashes)
        if any(set(mapping) != item_ids for mapping in per_item_maps):
            raise ValueError("translation per-item identities must cover every affected item")
        participant_ids = {unit_id for participants in self.item_unit_ids.values() for unit_id in participants}
        if not participant_ids or any(
            not participants or len(participants) != len(set(participants))
            for participants in self.item_unit_ids.values()
        ):
            raise ValueError("translation items require Unit participants")
        if self.stage in {"translate", "review"} and any(
            len(participants) != 1 for participants in self.item_unit_ids.values()
        ):
            raise ValueError(f"{self.stage} items must each belong to exactly one Unit")
        per_unit_maps = (self.record_versions, self.unit_document_ids, self.plan_epochs, self.revisions)
        if any(set(mapping) != participant_ids for mapping in per_unit_maps):
            raise ValueError("translation Unit version maps must cover every participant")
        if self.stage in {"review", "coherence"} and set(self.target_hashes) != item_ids:
            raise ValueError(f"{self.stage} requests require a target hash for every item")
        if self.stage == "translate" and not set(self.target_hashes).issubset(item_ids):
            raise ValueError("translation target hashes reference an unknown item")
        if any(not value for value in self.terms_hashes.values()) or any(
            not value for value in self.context_hashes.values()
        ):
            raise ValueError("terms and context hashes cannot be empty")
        return self


def _hash_payload(value: BaseModel | dict[str, Any], field: str) -> str:
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
    data.pop(field, None)
    return canonical_hash(data)


def segment_hash(segment: Segment | dict[str, Any]) -> str:
    return _hash_payload(segment, "segment_hash")


def cut_plan_hash(plan: CutPlan | dict[str, Any]) -> str:
    return _hash_payload(plan, "plan_hash")


def term_plan_hash(plan: TermExtractionPlan | dict[str, Any]) -> str:
    data = plan.model_dump(mode="json") if isinstance(plan, BaseModel) else dict(plan)
    data.setdefault("format", TERM_PLAN_FORMAT)
    data.pop("plan_hash", None)
    return canonical_hash(data)


def candidate_pool_record_hash(pool: CandidatePool) -> str:
    return _hash_payload(pool, "record_hash")


def unit_record_hash(record: UnitRecord) -> str:
    return _hash_payload(record, "record_hash")


def validate_cut_plan_coverage(plan: CutPlan, source_length: int) -> None:
    if source_length < 0:
        raise ValueError("source length cannot be negative")
    if plan.segments[-1].source_end != source_length:
        raise ValueError("CutPlan does not cover the complete Unit source interval")


def compute_input_hash(logical_hash: str, plan_hash: str) -> str:
    return canonical_hash({"logical_hash": logical_hash, "plan_hash": plan_hash})


def validate_term_scopes(
    terms: tuple[TermRule, ...] | tuple[UserTerm, ...] | tuple[FrozenTerm, ...],
    document_ids: set[str],
    unit_ids: set[str],
) -> None:
    for term in terms:
        if not set(term.scope.document_ids).issubset(document_ids):
            raise ValueError(f"term {term.term_id} scope references an unknown document")
        if not set(term.scope.unit_ids).issubset(unit_ids):
            raise ValueError(f"term {term.term_id} scope references an unknown Unit")


def glossary_rules_hash(terms: tuple[FrozenTerm, ...]) -> str:
    rules = [
        {
            "term_id": term.term_id,
            "source": term.source,
            "target": term.target,
            "aliases": list(term.aliases),
            "scope": term.scope.model_dump(mode="json"),
            "mode": term.mode,
            "match_policy": term.match_policy,
            "note": term.note,
        }
        for term in sorted(terms, key=lambda item: item.term_id)
    ]
    return canonical_hash({"terms": rules})


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {key: _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(child) for child in value]
    return value


def _validate_json(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ValueError(f"JSON nesting exceeds {MAX_JSON_DEPTH}")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            _validate_json(child, depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate_json(child, depth + 1)
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise TypeError(f"not a JSON value: {type(value).__name__}")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON numbers are forbidden")


def canonical_json_bytes(value: Any, *, max_bytes: int = MAX_JSON_BYTES) -> bytes:
    value = _json_value(value)
    _validate_json(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > max_bytes:
        raise ValueError(f"JSON exceeds {max_bytes} bytes")
    return encoded


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def strict_json_loads(data: str | bytes, *, max_bytes: int = MAX_JSON_BYTES) -> JsonValue:
    raw = data if isinstance(data, bytes) else data.encode()
    if len(raw) > max_bytes:
        raise ValueError(f"JSON exceeds {max_bytes} bytes")
    value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    _validate_json(value)
    return value


def parse_contract[ModelT: BaseModel](
    data: str | bytes | dict[str, Any], model: type[ModelT], expected_format: str
) -> ModelT:
    value = strict_json_loads(data) if isinstance(data, (str, bytes)) else data
    if not isinstance(value, dict):
        raise TypeError("contract root must be a JSON object")
    actual = value.get("format")
    if actual != expected_format:
        raise UnsupportedFormatError(f"unsupported format {actual!r}; expected {expected_format!r}")
    return model.model_validate(value)


def require_protocol(data: dict[str, Any], expected_protocol: str) -> None:
    actual = data.get("protocol")
    if actual != expected_protocol:
        raise UnsupportedFormatError(f"unsupported protocol {actual!r}; expected {expected_protocol!r}")


__all__ = [
    "Attempt",
    "BookPlan",
    "CandidatePool",
    "CutPlan",
    "DocumentPlan",
    "ExtractionItem",
    "FreezeIntent",
    "FrozenTerm",
    "GlossaryPayload",
    "GlossarySnapshot",
    "ItemRecord",
    "ItemStatus",
    "NodeRecord",
    "PreparationPlan",
    "RegistryEntry",
    "RequestManifest",
    "ResourceRecord",
    "Segment",
    "SlotRange",
    "SourceRef",
    "SourceSlot",
    "SourceTextView",
    "TermCandidate",
    "TermEvidence",
    "TermExtractionPlan",
    "TermExtractionRecord",
    "TermPreparation",
    "TermRule",
    "TermScope",
    "Unit",
    "UnitRecord",
    "UnsupportedFormatError",
    "Usage",
    "UserTerm",
    "candidate_pool_record_hash",
    "canonical_hash",
    "canonical_json_bytes",
    "compute_input_hash",
    "cut_plan_hash",
    "glossary_rules_hash",
    "parse_contract",
    "require_protocol",
    "segment_hash",
    "source_view_hash_payload",
    "strict_json_loads",
    "term_plan_hash",
    "unit_record_hash",
    "validate_cut_plan_coverage",
    "validate_term_scopes",
]
