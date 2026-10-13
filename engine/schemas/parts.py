"""Immutable refinement plan for one legacy or repeatedly failing member."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_serializer, model_validator

from engine.schemas.contracts import FrozenModel, ItemRecord
from engine.schemas.members import RequestMember


class PartPlan(FrozenModel):
    format: Literal["epubox-parts-1"] = "epubox-parts-1"
    source_hash: str = Field(min_length=1)
    parent_id: str = Field(min_length=1)
    parent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    root_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_limit: int = Field(ge=1, strict=True)
    children: tuple[RequestMember, ...] = Field(min_length=2)
    seed: ItemRecord | None = None
    strategy: Literal["legacy_list", "list_items", "sentences", "table_cells"] = "legacy_list"
    separators: tuple[str, ...] = ()
    depth: int = Field(default=0, ge=0, le=3, strict=True)
    trigger_stage: Literal["translate", "review"] | None = None
    trigger_count: int | None = Field(default=None, ge=1, le=60, strict=True)

    @model_validator(mode="after")
    def validate_children(self) -> PartPlan:
        if any(
            child.parent_item_id != self.parent_id
            or child.parent_hash != self.parent_hash
            or not child.item_id.startswith(f"pc-{self.parent_id}-")
            for child in self.children
        ):
            raise ValueError("part children must retain their exact legacy parent identity")
        adaptive = self.strategy != "legacy_list"
        if adaptive != bool(self.separators):
            raise ValueError("adaptive part plans require frozen reconstruction separators")
        if adaptive and len(self.separators) != len(self.children) + 1:
            raise ValueError("adaptive part separators must surround every child")
        if adaptive and (self.depth < 1 or self.trigger_stage is None or self.trigger_count is None):
            raise ValueError("adaptive part plans require their durable failure trigger")
        if not adaptive and (self.depth or self.trigger_stage is not None or self.trigger_count is not None):
            raise ValueError("legacy list plans cannot carry an adaptive trigger")
        return self

    @model_serializer(mode="wrap")
    def serialize_compatible(self, handler):
        data = handler(self)
        if self.strategy == "legacy_list":
            for name in ("strategy", "separators", "depth", "trigger_stage", "trigger_count"):
                data.pop(name, None)
        return data


__all__ = ["PartPlan"]
