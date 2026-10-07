"""Translation execution contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from engine.schemas.base import (
    BOOK_FORMAT,
    REQUEST_FORMAT,
    UNIT_FORMAT,
    FrozenModel,
    ItemStatus,
    JsonValue,
    _hash_payload,
    canonical_hash,
)


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
    derived: dict[str, JsonValue] | None = None
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
        if self.derived is not None:
            state = self.derived.get("state")
            required = (
                {"state", "source_unit_id"}
                if state == "blocked_dependency"
                else {"state", "source_unit_id", "source_revision", "source_target_hash", "target", "target_hash"}
                if state == "valid"
                else None
            )
            if required is None or set(self.derived) != required or self.cut_plan is not None:
                raise ValueError("derived Unit requires one exact dependency state and no CutPlan")
            if not isinstance(self.derived["source_unit_id"], str) or not self.derived["source_unit_id"]:
                raise ValueError("derived Unit requires source_unit_id")
            if state == "valid" and (
                type(self.derived["source_revision"]) is not int
                or self.derived["source_revision"] < 0
                or not isinstance(self.derived["source_target_hash"], str)
                or not self.derived["source_target_hash"]
                or not isinstance(self.derived["target"], str)
                or not self.derived["target"]
                or self.derived["target_hash"] != canonical_hash(self.derived["target"])
            ):
                raise ValueError("valid derived Unit requires current source revision and target hash")
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
    sparse: bool = Field(default=False, strict=True, exclude_if=lambda value: not value)
    feedback_by_item: dict[str, str] = Field(default_factory=dict, exclude_if=lambda value: not value)
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

    @field_validator("record_versions", "plan_epochs", "revisions", mode="before")
    @classmethod
    def validate_versions(cls, versions: dict[str, int]) -> dict[str, int]:
        if not isinstance(versions, dict) or any(
            type(version) is not int or version < 0 for version in versions.values()
        ):
            raise ValueError("request versions must be non-negative integers")
        return versions

    @field_validator("term_ids_by_item")
    @classmethod
    def normalize_term_ids(cls, values: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        return {item_id: tuple(sorted(set(term_ids))) for item_id, term_ids in values.items()}

    @model_validator(mode="after")
    def validate_owner(self) -> RequestManifest:
        if self.feedback_by_item and (
            self.stage not in {"translate", "review"}
            or not set(self.feedback_by_item).issubset(self.item_ids)
            or any(not value.strip() or len(value) > 1200 for value in self.feedback_by_item.values())
        ):
            raise ValueError("repair feedback must name request members and contain bounded messages")
        if self.sparse and self.stage not in {"translate", "review"}:
            raise ValueError("only body workflow requests may regroup checkpoint members")
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


def segment_hash(segment: Segment | dict[str, Any]) -> str:
    return _hash_payload(segment, "segment_hash")


def cut_plan_hash(plan: CutPlan | dict[str, Any]) -> str:
    return _hash_payload(plan, "plan_hash")


def unit_record_hash(record: UnitRecord) -> str:
    return _hash_payload(record, "record_hash")


def validate_cut_plan_coverage(plan: CutPlan, source_length: int) -> None:
    if source_length < 0:
        raise ValueError("source length cannot be negative")
    if plan.segments[-1].source_end != source_length:
        raise ValueError("CutPlan does not cover the complete Unit source interval")


def compute_input_hash(logical_hash: str, plan_hash: str) -> str:
    return canonical_hash({"logical_hash": logical_hash, "plan_hash": plan_hash})
