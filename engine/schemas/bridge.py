"""T00 value contracts for preparation, whole atoms and source byte positions.

These objects freeze the handoff between tasks. Producers and the workflow
adapter are implemented by their owning tasks; no new runner lives here.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any, Literal

from pydantic import Field, model_validator

from engine.agents.runtime import wire_hash
from engine.schemas.budget import BudgetResult
from engine.schemas.contracts import (
    BookPlan,
    DocumentPlan,
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
type AtomicTag = Literal["p", "table", "em", "i", "ul", "ol"]
type SourceChannel = Literal["body", "attribute", "metadata", "navigation"]

MAP_FORMAT = "epubox-map-1"
BATCH_FORMAT = "epubox-batch-1"
PREPARED_FORMAT = "epubox-prepared-1"
ATOMIC_TAGS = frozenset({"p", "table", "em", "i", "ul", "ol"})
TRANSLATABLE_ATTRIBUTES = frozenset({"alt", "title", "aria-label", "aria-description"})


def source_channel(unit: Unit) -> SourceChannel:
    if unit.kind == "navigation":
        return "navigation"
    if unit.kind.startswith(("head_", "metadata_")) or unit.region.get("attribute_name") == "content":
        return "metadata"
    return "attribute" if unit.kind == "attribute" else "body"


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
    node_spans: dict[str, ByteSpan] = Field(default_factory=dict)
    locations: tuple[SourceLocation, ...] = ()
    protected_spans: tuple[ByteSpan, ...] = ()

    @model_validator(mode="after")
    def validate_locations(self) -> SourceMap:
        refs: dict[str, list[SourceRef]] = {}
        spans = [location.byte_span for location in self.locations]
        spans.extend(self.protected_spans)
        if any(span.byte_end > self.source_size for span in [*spans, *self.node_spans.values()]):
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
        if self.source_size and (
            not ordered_spans
            or ordered_spans[0].byte_start != 0
            or ordered_spans[-1].byte_end != self.source_size
            or any(left.byte_end != right.byte_start for left, right in pairwise(ordered_spans))
        ):
            raise ValueError("editable and protected spans must cover every original source byte")
        return self


class AtomicItem(Unit):
    """One outermost indivisible element or one safe virtual text block."""

    item_id: str = Field(min_length=1)
    ordinal: int = Field(ge=0, strict=True)
    channel: SourceChannel
    atomic_tag: AtomicTag | None = None
    source_span: ByteSpan


class AtomicDocument(FrozenModel):
    """Persistable T05 output; raw resource bytes stay in the source snapshot."""

    format: Literal["epubox-atoms-1"] = "epubox-atoms-1"
    document: DocumentPlan
    source_map: SourceMap
    items: tuple[AtomicItem, ...]

    @model_validator(mode="after")
    def validate_inventory(self) -> AtomicDocument:
        document = self.document
        if self.source_map.document_id != document.document_id or self.source_map.source_hash != document.source_hash:
            raise ValueError("atomic inventory and source map must share a document identity")
        if self.source_map.document_hash != document.resource.source_sha256:
            raise ValueError("atomic inventory must refer to the original resource bytes")
        if tuple(item.unit_id for item in self.items) != tuple(unit.unit_id for unit in document.units):
            raise ValueError("atomic inventory must cover the ordered source Units exactly once")
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("atomic item IDs must be unique")
        if set(self.source_map.node_spans) != set(document.nodes):
            raise ValueError("atomic inventory requires the complete element byte-span inventory")
        if tuple(item.ordinal for item in self.items) != tuple(range(len(self.items))):
            raise ValueError("atomic ordinals must cover the complete reading order")
        if any(left.source_span.byte_start > right.source_span.byte_start for left, right in pairwise(self.items)):
            raise ValueError("atomic reading order must match the original byte positions")
        fields = set(Unit.model_fields)
        by_id = {item.unit_id: item for item in self.items}
        owners_by_range = {
            (slot.slot_id, part.start, part.end): part.owner_unit_id
            for slot in document.source_slots.values()
            for part in slot.ranges
            if part.owner_kind == "unit"
        }
        locations_by_owner: dict[str, list[ByteSpan]] = {}
        for location in self.source_map.locations:
            ref = location.source_ref
            owner = owners_by_range.get((ref.slot_id, ref.start, ref.end))
            if owner is None:
                raise ValueError("mapped location must belong to an authoritative source owner")
            locations_by_owner.setdefault(owner, []).append(location.byte_span)
        patch_parent: AtomicItem | None = None
        previous_content: ByteSpan | None = None
        for item, unit in zip(self.items, document.units, strict=True):
            if (
                Unit.model_validate(item.model_dump(include=fields)) != unit
                or item.document_id != document.document_id
            ):
                raise ValueError("atomic item differs from its authoritative source Unit")
            if item.source_span.byte_end > self.source_map.source_size:
                raise ValueError("atomic item byte span exceeds the original resource")
            node_span = self.source_map.node_spans[item.node_key]
            if item.source_span.byte_start < node_span.byte_start or item.source_span.byte_end > node_span.byte_end:
                raise ValueError("item byte span must stay inside its authoritative source node")
            if any(
                location.byte_start < item.source_span.byte_start or location.byte_end > item.source_span.byte_end
                for location in locations_by_owner.get(item.unit_id, ())
            ):
                raise ValueError("actual mapped source ranges must lie inside their item byte span")
            if item.channel != source_channel(unit):
                raise ValueError("atomic content channel differs from its authoritative source Unit")
            qname = document.nodes[item.node_key].qname
            tag = qname.removeprefix("{http://www.w3.org/1999/xhtml}")
            attribute_item = bool(unit.slot_ids) and all(
                document.source_slots[slot_id].field == "attribute" for slot_id in unit.slot_ids
            )
            if (unit.region.get("type") == "attribute") != attribute_item:
                raise ValueError("item region type must match its authoritative source fields")
            if attribute_item:
                if len(unit.slot_ids) != 1:
                    raise ValueError("one attribute item must name exactly one source attribute")
                slot = document.source_slots[unit.slot_ids[0]]
                if (
                    item.node_key != slot.node_key
                    or unit.region.get("node_key") != slot.node_key
                    or unit.region.get("attribute_name") != slot.attribute_name
                    or locations_by_owner.get(item.unit_id) != [item.source_span]
                ):
                    raise ValueError("attribute item must match its exact authoritative source field and bytes")
            else:
                if previous_content is not None and previous_content.byte_end > item.source_span.byte_start:
                    raise ValueError("independent content item byte spans must not overlap")
                previous_content = item.source_span
            expected_atomic = (
                tag
                if qname.startswith("{http://www.w3.org/1999/xhtml}") and tag in ATOMIC_TAGS and not attribute_item
                else None
            )
            if item.atomic_tag != expected_atomic:
                raise ValueError("indivisible source elements must retain their atomic tag")
            if item.atomic_tag is not None and (
                item.source_span != self.source_map.node_spans[item.node_key]
                or document.nodes[item.node_key].qname != f"{{http://www.w3.org/1999/xhtml}}{item.atomic_tag}"
                or item.channel != "body"
            ):
                raise ValueError("an indivisible item must own its complete original element")
            if item.channel == "body":
                patch_parent = item
            if unit.region.get("type") == "attribute":
                parent_id = unit.region.get("patch_owner_id")
                expected_parent = None
                if patch_parent is not None:
                    parent_span = patch_parent.source_span
                    parent_path = document.nodes[patch_parent.node_key].element_path
                    path = document.nodes[item.node_key].element_path
                    if (
                        parent_span.byte_start <= item.source_span.byte_start
                        and item.source_span.byte_end <= parent_span.byte_end
                        and path[: len(parent_path)] == parent_path
                        and (
                            item.node_key == patch_parent.node_key
                            or any(entry.source_node_key == item.node_key for entry in patch_parent.registry.values())
                        )
                    ):
                        expected_parent = patch_parent.unit_id
                if parent_id != expected_parent or parent_id is not None and parent_id not in by_id:
                    raise ValueError("attribute patch owner must be its containing source body item")
        owners = {document.nodes[item.node_key].element_path for item in self.items if item.atomic_tag is not None}
        for item in self.items:
            if item.channel != "body":
                continue
            path = document.nodes[item.node_key].element_path
            if any(path[:depth] in owners for depth in range(len(path))):
                raise ValueError("outer atomic elements cannot have descendant body tasks")
        owned = {
            (slot.slot_id, part.start, part.end)
            for slot in document.source_slots.values()
            for part in slot.ranges
            if part.owner_kind == "unit"
        }
        mapped = {
            (location.source_ref.slot_id, location.source_ref.start, location.source_ref.end)
            for location in self.source_map.locations
        }
        if owned != mapped:
            raise ValueError("every owned source character range requires exactly one byte mapping")
        for location in self.source_map.locations:
            slot = document.source_slots[location.source_ref.slot_id]
            if (
                location.node_key != slot.node_key
                or location.field != slot.field
                or location.attribute_name != slot.attribute_name
            ):
                raise ValueError("mapped location fields must match the authoritative source slot")
        return self


def batch_item_hash(item: FrozenModel, freeze_id: str, wire_item, context_hash: str) -> str:
    return canonical_hash({"atom": item, "freeze": freeze_id, "wire": wire_item, "context": context_hash})


class RequestBatch(FrozenModel):
    format: Literal["epubox-batch-1"] = BATCH_FORMAT
    manifest: RequestManifest
    items: tuple[AtomicItem, ...]
    context: tuple[str, ...] = Field(default=(), max_length=2)
    payload: dict[str, JsonValue]
    budget: BudgetResult

    @model_validator(mode="after")
    def validate_members(self) -> RequestBatch:
        return validate_batch_identity(self)


def validate_batch_identity(batch: Any, *, allow_pieces: bool = False) -> Any:
    if batch.manifest.stage not in {"translate", "review"}:
        raise ValueError("body batches require translate or review stage")
    if batch.manifest.source_hard_limit != batch.budget.identity.source_hard_limit:
        raise ValueError("batch manifest and budget source hard limits differ")
    if not batch.items or tuple(item.item_id for item in batch.items) != batch.manifest.item_ids:
        raise ValueError("ordered batch members must match the manifest")
    if not allow_pieces and len({item.unit_id for item in batch.items}) != len(batch.items):
        raise ValueError("an atomic Unit cannot appear twice in one batch")
    if len({(item.document_id, item.channel) for item in batch.items}) != 1:
        raise ValueError("batch members must share a document and channel")
    if allow_pieces:
        for left, right in pairwise(batch.items):
            if (left.ordinal, left.piece_index) >= (right.ordinal, right.piece_index):
                raise ValueError("batch members must follow source and piece order")
            if left.unit_id == right.unit_id and (
                left.parent_item_id != right.parent_item_id
                or left.parent_hash != right.parent_hash
                or left.preflight_hash != right.preflight_hash
                or left.piece_count != right.piece_count
                or not batch.manifest.sparse
                and right.piece_index != left.piece_index + 1
            ):
                raise ValueError("repeated Unit members require consecutive verified siblings")
    elif any(left.ordinal >= right.ordinal for left, right in pairwise(batch.items)):
        raise ValueError("batch members must follow source reading order")
    if any(len(fragment) > 400 for fragment in batch.context):
        raise ValueError("each shared context fragment is limited to 400 characters")
    for item in batch.items:
        if batch.manifest.item_unit_ids[item.item_id] != (item.unit_id,):
            raise ValueError("batch item owner differs from the manifest")
        if batch.manifest.unit_document_ids[item.unit_id] != item.document_id:
            raise ValueError("batch document owner differs from the manifest")
    if batch.budget.stage != batch.manifest.stage or not batch.budget.fits:
        raise ValueError("batch requires a fitting budget for its current stage")
    payload_hash = wire_hash(
        batch.budget.stage, batch.payload, None if batch.manifest.output_unlimited else batch.budget.output_tokens
    )
    if batch.budget.wire_hash != payload_hash or batch.manifest.wire_hash != payload_hash:
        raise ValueError("batch budget and manifest must bind the complete payload")
    if batch.payload.get("request_id") != batch.manifest.request_id:
        raise ValueError("payload request identity differs from the manifest")
    if batch.payload.get("context", []) != list(batch.context):
        raise ValueError("payload must contain the batch's shared context exactly once")
    wire_items = batch.payload.get("items")
    if not isinstance(wire_items, list) or len(wire_items) != len(batch.items):
        raise ValueError("payload must contain exactly the ordered batch members")
    for item, wire_item in zip(batch.items, wire_items, strict=True):
        if (
            not isinstance(wire_item, dict)
            or wire_item.get("item_id") != item.item_id
            or wire_item.get("source") != item.source_projection
            or "context" in wire_item
        ):
            raise ValueError("payload item differs from its whole source atom")
        terms = wire_item.get("terms", [])
        if wire_item.get("repair") != batch.manifest.feedback_by_item.get(item.item_id):
            raise ValueError("payload repair feedback differs from its frozen manifest")
        if not isinstance(terms, list):
            raise ValueError("payload terms must identify every selected rule")  # noqa: TRY004 - Pydantic wraps ValueError.
        selected: list[str] = []
        for term in terms:
            if not isinstance(term, dict):
                raise ValueError("payload terms must identify every selected rule")  # noqa: TRY004 - Pydantic wraps ValueError.
            term_id = term.get("term_id")
            if not isinstance(term_id, str) or not term_id:
                raise ValueError("payload terms must identify every selected rule")
            selected.append(term_id)
        term_ids = tuple(sorted(selected))
        if len(set(term_ids)) != len(term_ids) or batch.manifest.term_ids_by_item[item.item_id] != term_ids:
            raise ValueError("manifest term IDs differ from payload terms")
        context_hash = canonical_hash(batch.payload.get("context", []))
        if batch.manifest.terms_hashes[item.item_id] != canonical_hash(terms):
            raise ValueError("manifest terms hash differs from payload terms")
        if batch.manifest.context_hashes[item.item_id] != context_hash:
            raise ValueError("manifest context hash differs from shared payload context")
        if batch.manifest.input_hashes[item.item_id] != batch_item_hash(
            item, batch.manifest.freeze_id or "", wire_item, context_hash
        ):
            raise ValueError("manifest input hash differs from its source atom and payload")
        if batch.manifest.stage == "review" and (
            type(wire_item.get("base_revision")) is not int
            or wire_item["base_revision"] != batch.manifest.revisions[item.unit_id]
        ):
            raise ValueError("manifest revision differs from the saved review payload")
        if (
            batch.manifest.stage == "review"
            and canonical_hash(wire_item.get("target")) != (batch.manifest.target_hashes[item.item_id])
        ):
            raise ValueError("review payload target differs from its saved target identity")
    if batch.manifest.stage == "review" and batch.budget.review_targets != "actual":
        raise ValueError("a dispatchable review batch requires actual saved targets")
    return batch


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
