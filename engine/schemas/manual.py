"""Immutable evidence for an explicitly authorized Codex-authored translation."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from engine.schemas.contracts import FrozenModel, ItemRecord, canonical_hash


class ManualArtifact(FrozenModel):
    format: Literal["epubox-manual-1"] = "epubox-manual-1"
    author: Literal["codex"] = "codex"
    authorization: Literal["user-explicit"] = "user-explicit"
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    member_id: str = Field(min_length=1)
    member_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_projection_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prior: ItemRecord
    target_projection: str = Field(min_length=1)
    target_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_hashes(self) -> ManualArtifact:
        if self.prior.item_id != self.member_id or self.prior.segment_id != self.member_id:
            raise ValueError("manual artifact prior record belongs to another member")
        if self.target_hash != canonical_hash(self.target_projection):
            raise ValueError("manual artifact target hash does not match its target")
        return self


__all__ = ["ManualArtifact"]
