"""T00 value contracts for preparation, whole atoms and source byte positions.

These objects freeze the handoff between tasks. Producers and the workflow
adapter are implemented by their owning tasks; no new runner lives here.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Literal

from pydantic import Field, model_validator

from engine.agents.runtime import wire_hash
from engine.schemas.budget import BudgetResult
from engine.schemas.contracts import (
    BookPlan,
    FrozenModel,
    GlossarySnapshot,
    ItemRecord,
    JsonValue,
    PreparationPlan,
    RequestManifest,
    SourceRef,
    Unit,
    canonical_hash,
)

type SourceContext = PreparationPlan
type FrozenTerms = GlossarySnapshot
type ItemResult = ItemRecord

MAP_FORMAT = "epubox-map-1"
BATCH_FORMAT = "epubox-batch-1"
PREPARED_FORMAT = "epubox-prepared-1"
ATOMIC_TAGS = frozenset({"p", "table", "em", "i", "ul", "ol"})
TRANSLATABLE_ATTRIBUTES = frozenset({"alt", "title", "aria-label", "aria-description"})


class ByteSpan(FrozenModel):
    """Half-open positions in the original resource bytes, never decoded text."""

    byte_start: int = Field(ge=0, strict=True)
    byte_end: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def validate_bounds(self) -> ByteSpan:
        if self.byte_end <= self.byte_start:
            raise ValueError("byte span must be non-empty and ordered")
        return self


class SourceLocation(FrozenModel):
    """A decoded slot interval and its independently measured source bytes."""

    node_key: str = Field(min_length=1)
    source_ref: SourceRef
    byte_span: ByteSpan
    field: Literal["text", "tail", "attribute"]
    attribute_name: str | None = None

    @model_validator(mode="after")
    def validate_attribute(self) -> SourceLocation:
        if (self.field == "attribute") != (self.attribute_name is not None):
            raise ValueError("only attribute locations name an attribute")
        return self


class SourceMap(FrozenModel):
    format: Literal["epubox-map-1"] = MAP_FORMAT
    document_id: str = Field(min_length=1)
    source_hash: str = Field(min_length=1)
    document_hash: str = Field(min_length=1)
    encoding: str = Field(min_length=1)
    source_size: int = Field(ge=0, strict=True)
    locations: tuple[SourceLocation, ...] = ()
    protected_spans: tuple[ByteSpan, ...] = ()

    @model_validator(mode="after")
    def validate_locations(self) -> SourceMap:
        refs: dict[str, list[SourceRef]] = {}
        spans = [location.byte_span for location in self.locations]
        spans.extend(self.protected_spans)
        if any(span.byte_end > self.source_size for span in spans):
            raise ValueError("byte span exceeds the original resource")
        for location in self.locations:
            refs.setdefault(location.source_ref.slot_id, []).append(location.source_ref)
        for ranges in refs.values():
            ordered = sorted(ranges, key=lambda ref: ref.start)
            if any(left.end > right.start for left, right in pairwise(ordered)):
                raise ValueError("source locations overlap within one decoded slot")
        ordered_spans = sorted(spans, key=lambda span: span.byte_start)
        if any(left.byte_end > right.byte_start for left, right in pairwise(ordered_spans)):
            raise ValueError("editable and protected source byte spans must not overlap")
        return self


class AtomicItem(Unit):
    """One outermost indivisible element or one safe virtual text block."""

    item_id: str = Field(min_length=1)
    ordinal: int = Field(ge=0, strict=True)
    channel: Literal["body", "attribute", "metadata", "navigation"]
    atomic_tag: Literal["p", "table", "em", "i", "ul", "ol"] | None = None
    source_span: ByteSpan


class RequestBatch(FrozenModel):
    format: Literal["epubox-batch-1"] = BATCH_FORMAT
    manifest: RequestManifest
    items: tuple[AtomicItem, ...]
    context: tuple[str, ...] = Field(default=(), max_length=2)
    payload: dict[str, JsonValue]
    budget: BudgetResult

    @model_validator(mode="after")
    def validate_members(self) -> RequestBatch:
        if self.manifest.stage not in {"translate", "review"}:
            raise ValueError("body batches require translate or review stage")
        if not self.items or tuple(item.item_id for item in self.items) != self.manifest.item_ids:
            raise ValueError("ordered batch members must match the manifest")
        if len({item.unit_id for item in self.items}) != len(self.items):
            raise ValueError("an atomic Unit cannot appear twice in one batch")
        if len({(item.document_id, item.channel) for item in self.items}) != 1:
            raise ValueError("batch members must share a document and channel")
        if any(left.ordinal >= right.ordinal for left, right in pairwise(self.items)):
            raise ValueError("batch members must follow source reading order")
        if any(len(fragment) > 400 for fragment in self.context):
            raise ValueError("each shared context fragment is limited to 400 characters")
        for item in self.items:
            if self.manifest.item_unit_ids[item.item_id] != (item.unit_id,):
                raise ValueError("batch item owner differs from the manifest")
            if self.manifest.unit_document_ids[item.unit_id] != item.document_id:
                raise ValueError("batch document owner differs from the manifest")
        if self.budget.stage != self.manifest.stage or not self.budget.fits:
            raise ValueError("batch requires a fitting budget for its current stage")
        payload_hash = wire_hash(self.budget.stage, self.payload, self.budget.output_tokens)
        if self.budget.wire_hash != payload_hash or self.manifest.wire_hash != payload_hash:
            raise ValueError("batch budget and manifest must bind the complete payload")
        if self.payload.get("request_id") != self.manifest.request_id:
            raise ValueError("payload request identity differs from the manifest")
        if self.payload.get("context", []) != list(self.context):
            raise ValueError("payload must contain the batch's shared context exactly once")
        wire_items = self.payload.get("items")
        if not isinstance(wire_items, list) or len(wire_items) != len(self.items):
            raise ValueError("payload must contain exactly the ordered batch members")
        for item, wire_item in zip(self.items, wire_items, strict=True):
            if (
                not isinstance(wire_item, dict)
                or wire_item.get("item_id") != item.item_id
                or wire_item.get("source") != item.source_projection
                or "context" in wire_item
            ):
                raise ValueError("payload item differs from its whole source atom")
            if (
                self.manifest.stage == "review"
                and canonical_hash(wire_item.get("target")) != (self.manifest.target_hashes[item.item_id])
            ):
                raise ValueError("review payload target differs from its saved target identity")
        if self.manifest.stage == "review" and self.budget.review_targets != "actual":
            raise ValueError("a dispatchable review batch requires actual saved targets")
        return self


class PreflightCheck(FrozenModel):
    """Identity binding for T08; only the actual preflight producer can attest."""

    source_hash: str = Field(min_length=1)
    map_hashes: dict[str, str]
    atoms_hash: str = Field(min_length=1)
    budget_hash: str = Field(min_length=1)
    passed: Literal[True]


class PreparedInput(FrozenModel):
    """Ready handoff referencing existing preparation, glossary and plan values.

    T15 additionally checks committed files by reading them back. A constructed
    value alone is not evidence that those files have been safely published.
    """

    format: Literal["epubox-prepared-1"] = PREPARED_FORMAT
    preparation: SourceContext
    glossary: FrozenTerms
    bookplan: BookPlan
    preflight: PreflightCheck
    map_hashes: dict[str, str]
    plan_hashes: dict[str, str]

    @model_validator(mode="after")
    def validate_ready_identity(self) -> PreparedInput:
        source = self.preparation.source_hash
        if {self.bookplan.source_hash, self.glossary.source_hash, self.preflight.source_hash} != {source}:
            raise ValueError("prepared input must share the same source identity")
        if self.bookplan.run_id != self.preparation.run_id:
            raise ValueError("prepared input must share the same run identity")
        if self.bookplan.preparation_hash != canonical_hash(self.preparation):
            raise ValueError("prepared input preparation hash does not match its canonical payload")
        if self.bookplan.glossary_file_sha256 != canonical_hash(self.glossary):
            raise ValueError("prepared input glossary hash does not match its canonical payload")
        if self.bookplan.translation_config != self.preparation.translation_config:
            raise ValueError("prepared input translation configuration changed")
        if self.glossary.extraction_config_hash != canonical_hash(self.preparation.extraction_config):
            raise ValueError("prepared input extraction configuration changed")
        if self.bookplan.freeze_id != self.glossary.freeze_id:
            raise ValueError("prepared input must use the committed frozen glossary")
        if self.glossary.user_terms_hash != self.preparation.user_terms_hash:
            raise ValueError("prepared input user terminology changed")
        if self.bookplan.document_hashes != self.preparation.document_hashes:
            raise ValueError("prepared input document inventory changed")
        if set(self.map_hashes) != set(self.bookplan.document_hashes):
            raise ValueError("every prepared document requires a source map")
        if self.map_hashes != self.preflight.map_hashes:
            raise ValueError("preflight source maps differ from the prepared maps")
        if set(self.plan_hashes) != set(self.bookplan.unit_ids):
            raise ValueError("every prepared Unit requires a final plan reference")
        if any(
            plan_hash is not None and self.plan_hashes[unit_id] != plan_hash
            for unit_id, plan_hash in self.bookplan.initial_unit_plans.items()
        ):
            raise ValueError("prepared plan references differ from the committed plans")
        if self.bookplan.unit_documents != self.preparation.unit_documents:
            raise ValueError("prepared Unit ownership changed")
        if any(not value for value in (*self.map_hashes.values(), *self.plan_hashes.values())):
            raise ValueError("prepared references cannot be empty")
        return self
