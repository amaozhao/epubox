"""Terminology preparation contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_serializer, model_validator

from engine.schemas.base import (
    CANDIDATES_FORMAT,
    EXTRACTION_RECORD_FORMAT,
    FREEZE_FORMAT,
    GLOSSARY_FORMAT,
    PREPARATION_FORMAT,
    TERM_PLAN_FORMAT,
    FrozenModel,
    JsonValue,
    _hash_payload,
    canonical_hash,
)
from engine.schemas.source import SourceRef


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


class TermCandidateRejection(FrozenModel):
    rejection_id: str = Field(min_length=1)
    extraction_item_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    candidate_index: int = Field(ge=0)
    status: Literal["rejected_schema"] = "rejected_schema"
    reason: str = Field(min_length=1, max_length=2000)
    source: str | None = Field(default=None, min_length=1, max_length=500)
    target: str | None = Field(default=None, min_length=1, max_length=500)
    category: str | None = Field(default=None, min_length=1, max_length=100)


class ExtractionItem(FrozenModel):
    item_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    view_ids: tuple[str, ...]
    primary_ranges: tuple[dict[str, JsonValue], ...]
    context_refs: tuple[str, ...] = ()
    context_ranges: tuple[dict[str, JsonValue], ...] = ()
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
    rejections: tuple[TermCandidateRejection, ...] = ()
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
            if any(
                evidence.view_id not in self.view_ids
                and (
                    candidate.status not in {"rejected_evidence", "rejected_schema"}
                    or evidence.evidence_check != "rejected"
                )
                for evidence in candidate.evidence
            ):
                raise ValueError("candidate evidence references an unknown extraction view")
        if any(rejection.extraction_item_id != self.item_id for rejection in self.rejections):
            raise ValueError("candidate rejection belongs to another extraction item")
        if any(rejection.request_id not in self.request_ids for rejection in self.rejections):
            raise ValueError("candidate rejection references an unknown request")
        if len({rejection.rejection_id for rejection in self.rejections}) != len(self.rejections):
            raise ValueError("candidate rejection IDs must be unique")
        return self


class CandidatePool(FrozenModel):
    format: Literal["epubox-candidates-1"] = CANDIDATES_FORMAT
    source_hash: str = Field(min_length=1)
    preparation_hash: str = Field(min_length=1)
    term_plan_hash: str = Field(min_length=1)
    record_version: int = Field(default=0, ge=0)
    extraction_status: Literal["open", "closed", "closed_with_gaps", "disabled", "not_required"] = "open"
    candidates: tuple[TermCandidate, ...] = ()
    rejections: tuple[TermCandidateRejection, ...] = ()
    conflict_groups: tuple[dict[str, JsonValue], ...] = ()
    consumed_response_ids: tuple[str, ...] = ()
    record_hash: str | None = None

    @model_validator(mode="after")
    def validate_candidates(self) -> CandidatePool:
        if len({candidate.candidate_id for candidate in self.candidates}) != len(self.candidates):
            raise ValueError("candidate IDs must be unique")
        if len({rejection.rejection_id for rejection in self.rejections}) != len(self.rejections):
            raise ValueError("candidate rejection IDs must be unique")
        if any(rejection.request_id not in self.consumed_response_ids for rejection in self.rejections):
            raise ValueError("candidate rejection references an unconsumed response")
        if self.extraction_status != "open" and any(candidate.status == "proposed" for candidate in self.candidates):
            raise ValueError("closed candidate pools cannot contain proposed candidates")
        if self.record_hash is not None and self.record_hash != candidate_pool_record_hash(self):
            raise ValueError("candidate pool record_hash does not match its canonical payload")
        return self

    @model_serializer(mode="wrap")
    def serialize_compatible(self, handler: Any) -> Any:
        data = handler(self)
        if not self.rejections:
            data.pop("rejections", None)
        return data


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
    unit_documents: dict[str, str] = Field(default_factory=dict)
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
                if term.scope.kind == "units" and any(
                    unit_id not in source_units and self.unit_documents.get(unit_id) not in source_documents
                    for unit_id in term.scope.unit_ids
                ):
                    raise ValueError("model term Unit scope exceeds adopted source documents")
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


def term_plan_hash(plan: TermExtractionPlan | dict[str, Any]) -> str:
    data = plan.model_dump(mode="json") if isinstance(plan, BaseModel) else dict(plan)
    data.setdefault("format", TERM_PLAN_FORMAT)
    data.pop("plan_hash", None)
    return canonical_hash(data)


def candidate_pool_record_hash(pool: CandidatePool) -> str:
    return _hash_payload(pool, "record_hash")


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
