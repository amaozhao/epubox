"""Atomic-lane ready references, separate from legacy CutPlan book records."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from engine.schemas.bridge import PreflightCheck
from engine.schemas.contracts import FrozenModel, GlossarySnapshot, JsonValue, PreparationPlan, canonical_hash


class AtomicPlan(FrozenModel):
    format: Literal["epubox-plan-1"] = "epubox-plan-1"
    source_hash: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    preparation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    glossary_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    freeze_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    freeze_id: str = Field(min_length=1)
    document_hashes: dict[str, str]
    inventory_hashes: dict[str, str]
    member_hashes: dict[str, str]
    batch_hashes: dict[str, str]
    unit_ids: tuple[str, ...]
    unit_documents: dict[str, str]
    unit_members: dict[str, tuple[str, ...]]
    derived_sources: dict[str, str] = Field(default_factory=dict)
    required_unit_count: int = Field(ge=0, strict=True)
    translation_config: dict[str, JsonValue]
    preflight_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_policy_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_refs(self) -> AtomicPlan:
        owners = set(self.unit_ids)
        if len(owners) != len(self.unit_ids) or self.required_unit_count != len(owners):
            raise ValueError("atomic plan must contain every required parent exactly once")
        if set(self.unit_documents) != owners or set(self.unit_members) != owners:
            raise ValueError("atomic plan must cover every parent owner")
        members = tuple(member for unit in self.unit_ids for member in self.unit_members[unit])
        if len(members) != len(set(members)) or set(members) != set(self.member_hashes):
            raise ValueError("atomic plan must cover every request member exactly once")
        if any(not self.unit_members[unit] for unit in self.unit_ids):
            raise ValueError("atomic parents require at least one member")
        if set(self.inventory_hashes) != set(self.document_hashes):
            raise ValueError("atomic plan must cover every source inventory")
        if not set(self.unit_documents.values()).issubset(self.document_hashes):
            raise ValueError("atomic plan names an unknown parent document")
        if not set(self.derived_sources).issubset(owners) or not set(self.derived_sources.values()).issubset(owners):
            raise ValueError("derived navigation must reference existing parent Units")
        for unit, source in self.derived_sources.items():
            seen = {unit}
            while source in self.derived_sources:
                if source in seen:
                    raise ValueError("derived navigation cannot contain a dependency cycle")
                seen.add(source)
                source = self.derived_sources[source]
            if source in seen:
                raise ValueError("derived navigation cannot depend on itself")
        for hashes in (self.document_hashes, self.inventory_hashes, self.member_hashes, self.batch_hashes):
            if any(
                len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in hashes.values()
            ):
                raise ValueError("atomic references require SHA256 digests")
        return self


class AtomicPreparedInput(FrozenModel):
    format: Literal["epubox-prepared-2"] = "epubox-prepared-2"
    preparation: PreparationPlan
    glossary: GlossarySnapshot
    plan: AtomicPlan
    preflight: PreflightCheck

    @model_validator(mode="after")
    def validate_identity(self) -> AtomicPreparedInput:
        prep, plan = self.preparation, self.plan
        if {prep.source_hash, self.glossary.source_hash, plan.source_hash, self.preflight.source_hash} != {
            prep.source_hash
        }:
            raise ValueError("atomic ready inputs must share the original source")
        if plan.run_id != prep.run_id or plan.preparation_hash != canonical_hash(prep):
            raise ValueError("atomic ready preparation identity changed")
        if plan.document_hashes != prep.document_hashes or plan.unit_documents != prep.unit_documents:
            raise ValueError("atomic ready source ownership changed")
        if plan.translation_config != prep.translation_config:
            raise ValueError("atomic ready translation configuration changed")
        if plan.freeze_id != self.glossary.freeze_id or plan.glossary_file_sha256 != canonical_hash(self.glossary):
            raise ValueError("atomic ready glossary identity changed")
        if self.glossary.extraction_config_hash != canonical_hash(prep.extraction_config):
            raise ValueError("atomic ready extraction configuration changed")
        if self.glossary.user_terms_hash != prep.user_terms_hash:
            raise ValueError("atomic ready user terminology changed")
        if set(self.preflight.map_hashes) != set(plan.inventory_hashes):
            raise ValueError("atomic ready preflight coverage changed")
        return self


__all__ = ["AtomicPlan", "AtomicPreparedInput"]
