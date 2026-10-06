"""Frozen request members materialized from an accepted atomic preflight."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from engine.schemas.bridge import AtomicTag, ByteSpan, SourceChannel, batch_item_hash, validate_batch_identity
from engine.schemas.budget import BudgetResult
from engine.schemas.contracts import FrozenModel, JsonValue, RegistryEntry, RequestManifest

MEMBER_FORMAT = "epubox-member-1"
MEMBER_BATCH_FORMAT = "epubox-batch-2"


class RequestMember(FrozenModel):
    """One complete atom or one preflight-approved virtual piece."""

    format: Literal["epubox-member-1"] = MEMBER_FORMAT
    item_id: str = Field(min_length=1)
    parent_item_id: str = Field(min_length=1)
    parent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    preflight_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    unit_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    channel: SourceChannel
    ordinal: int = Field(ge=0, strict=True)
    piece_index: int = Field(ge=0, strict=True)
    piece_count: int = Field(ge=1, strict=True)
    root_node_key: str = Field(min_length=1)
    source_span: ByteSpan
    source_projection: str = Field(min_length=1)
    registry: dict[str, RegistryEntry] = Field(default_factory=dict)
    atomic_tag: AtomicTag | None = None

    @property
    def node_key(self) -> str:
        return self.root_node_key

    @model_validator(mode="after")
    def validate_piece(self) -> RequestMember:
        if self.piece_index >= self.piece_count:
            raise ValueError("member piece index must be inside its piece count")
        if (self.piece_count == 1) != (self.item_id == self.parent_item_id):
            raise ValueError("only whole members retain the parent item ID")
        if self.piece_count > 1 and not self.item_id.startswith("pc-"):
            raise ValueError("split members require stable preflight piece IDs")
        if any(key != entry.ref_id for key, entry in self.registry.items()):
            raise ValueError("member registry keys must match ref_id")
        return self


def member_input_hash(member: RequestMember, freeze_id: str, wire_item: object, context_hash: str) -> str:
    return batch_item_hash(member, freeze_id, wire_item, context_hash)


class MemberBatch(FrozenModel):
    """Dispatchable batch containing identity-bound request members."""

    format: Literal["epubox-batch-2"] = MEMBER_BATCH_FORMAT
    manifest: RequestManifest
    items: tuple[RequestMember, ...]
    context: tuple[str, ...] = Field(default=(), max_length=2)
    payload: dict[str, JsonValue]
    budget: BudgetResult

    @model_validator(mode="after")
    def validate_members(self) -> MemberBatch:
        if self.payload.get("prompt_version") != "epubox-members-1":
            raise ValueError("member payload requires its frozen prompt version")
        return validate_batch_identity(self, allow_pieces=True)


__all__ = ["MEMBER_BATCH_FORMAT", "MEMBER_FORMAT", "MemberBatch", "RequestMember", "member_input_hash"]
