"""Greedy complete-atom request planning; no model calls or checkpoint writes."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any, Literal, cast

from engine.item.budget import measure_budget
from engine.item.request import SourceIndex, build_payload
from engine.schemas.bridge import AtomicItem, ByteSpan, RequestBatch, batch_item_hash
from engine.schemas.budget import BudgetLimits, BudgetResult, BudgetStage
from engine.schemas.contracts import FrozenModel, GlossarySnapshot, ItemRecord, RequestManifest, canonical_hash

type BoundaryReason = Literal[
    "source",
    "input",
    "output",
    "context",
    "resource",
    "channel",
    "adjacency",
    "completed",
    "blocked",
    "heading",
    "end",
]
PACKING_VERSION = 1


class BatchBoundary(FrozenModel):
    request_id: str
    reason: BoundaryReason
    failures: tuple[str, ...] = ()


class BlockedItem(FrozenModel):
    item_id: str
    document_id: str
    resource_path: str
    source_span: ByteSpan
    atomic_tag: str | None
    budget: BudgetResult


class PackingResult(FrozenModel):
    format: Literal["epubox-packing-1"] = "epubox-packing-1"
    stage: BudgetStage
    source_hash: str
    freeze_id: str
    batches: tuple[RequestBatch, ...]
    boundaries: tuple[BatchBoundary, ...]
    blocked: tuple[BlockedItem, ...]
    skipped: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.blocked


def pack_requests(
    stage: BudgetStage,
    items: Sequence[AtomicItem],
    glossary: GlossarySnapshot,
    index: SourceIndex,
    limits: BudgetLimits,
    *,
    targets: Mapping[str, ItemRecord] | None = None,
    revisions: Mapping[str, int] | None = None,
    record_versions: Mapping[str, int] | None = None,
    plan_epochs: Mapping[str, int] | None = None,
    completed: Collection[str] = (),
    tokenizer_model: str = "gpt-3.5-turbo",
) -> PackingResult:
    """Keep every pending atom intact and account for blocked and completed atoms.

    Completion is stage-specific and supplied from the saved checkpoint. This pure
    planner never rewrites an earlier request or discards a saved translation.
    """
    if stage not in {"translate", "review"}:
        raise ValueError("body planning requires translate or review stage")
    if stage == "translate" and targets is not None:
        raise ValueError("translation planning cannot include review targets")
    members = tuple(items)
    identifiers = {item.item_id for item in members}
    if len(identifiers) != len(members):
        raise ValueError("body planning cannot repeat an atomic item")
    if not set(completed).issubset(index.items_by_id):
        raise ValueError("completed checkpoint references an unknown atomic item")
    source_hashes = {document.source_hash for document in index.documents.values()}
    if source_hashes != {glossary.source_hash}:
        raise ValueError("frozen glossary differs from the source index")
    for item in members:
        index.validate_items((item,))
    _validate_order(members, index.document_order)
    for versions in (revisions, record_versions, plan_epochs):
        if versions is not None and any(type(value) is not int or value < 0 for value in versions.values()):
            raise ValueError("saved revisions and versions must be non-negative integers")

    batches: list[RequestBatch] = []
    boundaries: list[BatchBoundary] = []
    blocked: list[BlockedItem] = []
    skipped: list[str] = []
    batch: RequestBatch | None = None

    def close(reason: BoundaryReason, failures: tuple[str, ...] = ()) -> None:
        nonlocal batch
        if batch is not None:
            batches.append(batch)
            boundaries.append(BatchBoundary(request_id=batch.manifest.request_id, reason=reason, failures=failures))
            batch = None

    def candidate(values: tuple[AtomicItem, ...]) -> tuple[dict[str, Any], BudgetResult]:
        identity = {
            "version": PACKING_VERSION,
            "stage": stage,
            "source": glossary.source_hash,
            "freeze": canonical_hash(glossary),
            "items": [canonical_hash(item) for item in values],
            "limits": limits.to_dict(),
            "model": tokenizer_model,
            "targets": {item.item_id: canonical_hash(targets.get(item.item_id)) for item in values}
            if stage == "review" and targets is not None
            else {},
            "revisions": {item.unit_id: (revisions or {}).get(item.unit_id, 0) for item in values},
            "versions": {item.unit_id: (record_versions or {}).get(item.unit_id, 0) for item in values},
            "epochs": {item.unit_id: (plan_epochs or {}).get(item.unit_id, 0) for item in values},
        }
        request_id = "tx-" + canonical_hash(identity)[:32]
        for count in (2, 1, 0):
            payload = build_payload(
                stage,
                values,
                glossary,
                index,
                request_id=request_id,
                targets={item.item_id: targets[item.item_id] for item in values if item.item_id in targets}
                if targets is not None
                else None,
                revisions={item.unit_id: revisions[item.unit_id] for item in values if item.unit_id in revisions}
                if stage == "review" and revisions is not None
                else None,
                context_count=count,
            )
            budget = measure_budget(stage=stage, payload=payload, limits=limits, tokenizer_model=tokenizer_model)
            if budget.fits or not payload["context"]:
                return payload, budget
        return payload, budget

    for position, item in enumerate(members):
        if item.item_id in completed:
            close("completed")
            skipped.append(item.item_id)
            continue
        if batch is not None:
            previous = batch.items[-1]
            if previous.document_id != item.document_id:
                close("resource")
            elif previous.channel != item.channel:
                close("channel")
            elif previous.ordinal + 1 != item.ordinal:
                close("adjacency")
        if batch is not None and item.kind == "heading" and position + 1 < len(members):
            following = members[position + 1]
            if (
                following.item_id not in completed
                and following.document_id == item.document_id
                and following.channel == item.channel
                and following.ordinal == item.ordinal + 1
            ):
                _, pair_budget = candidate((item, following))
                if pair_budget.fits:
                    _, combined_budget = candidate((*batch.items, item, following))
                    if not combined_budget.fits:
                        close("heading", combined_budget.failures)
        proposed = (*batch.items, item) if batch is not None else (item,)
        payload, budget = candidate(proposed)
        if not budget.fits and batch is not None:
            reason = budget.failures[0].split(" ", 1)[0]
            assert reason in {"source", "input", "output", "context"}
            close(cast(BoundaryReason, reason), budget.failures)
            proposed = (item,)
            payload, budget = candidate(proposed)
        if budget.fits:
            batch = _batch(proposed, glossary, payload, budget, revisions, record_versions, plan_epochs)
        else:
            close("blocked", budget.failures)
            blocked.append(
                BlockedItem(
                    item_id=item.item_id,
                    document_id=item.document_id,
                    resource_path=index.documents[item.document_id].resource.path,
                    source_span=item.source_span,
                    atomic_tag=item.atomic_tag,
                    budget=budget,
                )
            )
    close("end")
    return PackingResult(
        stage=stage,
        source_hash=glossary.source_hash,
        freeze_id=glossary.freeze_id,
        batches=tuple(batches),
        boundaries=tuple(boundaries),
        blocked=tuple(blocked),
        skipped=tuple(skipped),
    )


def _validate_order(items: tuple[AtomicItem, ...], document_order: tuple[str, ...]) -> None:
    positions = {document_id: ordinal for ordinal, document_id in enumerate(document_order)}
    seen_documents: set[str] = set()
    previous: AtomicItem | None = None
    for item in items:
        if previous is not None and previous.document_id == item.document_id:
            if previous.ordinal >= item.ordinal:
                raise ValueError("body planning requires source reading order")
        elif (
            item.document_id in seen_documents
            or previous is not None
            and positions[item.document_id] < positions[previous.document_id]
        ):
            raise ValueError("body planning cannot return to an earlier resource")
        seen_documents.add(item.document_id)
        previous = item


def _batch(
    items: tuple[AtomicItem, ...],
    glossary: GlossarySnapshot,
    payload: dict[str, Any],
    budget: BudgetResult,
    revisions: Mapping[str, int] | None,
    versions: Mapping[str, int] | None,
    epochs: Mapping[str, int] | None,
) -> RequestBatch:
    wire = {entry["item_id"]: entry for entry in payload["items"]}
    context_hash = canonical_hash(payload["context"])
    manifest = RequestManifest(
        request_id=payload["request_id"],
        stage=budget.stage,
        owner_kind="translation_item",
        owner_id=items[0].item_id,
        item_ids=tuple(item.item_id for item in items),
        input_hashes={
            item.item_id: batch_item_hash(item, glossary.freeze_id, wire[item.item_id], context_hash) for item in items
        },
        wire_hash=budget.wire_hash,
        record_versions={item.unit_id: (versions or {}).get(item.unit_id, 0) for item in items},
        item_unit_ids={item.item_id: (item.unit_id,) for item in items},
        unit_document_ids={item.unit_id: item.document_id for item in items},
        plan_epochs={item.unit_id: (epochs or {}).get(item.unit_id, 0) for item in items},
        revisions={item.unit_id: (revisions or {}).get(item.unit_id, 0) for item in items},
        target_hashes={item.item_id: canonical_hash(wire[item.item_id]["target"]) for item in items}
        if budget.stage == "review"
        else {},
        glossary_file_sha256=canonical_hash(glossary),
        freeze_id=glossary.freeze_id,
        term_ids_by_item={
            item.item_id: tuple(term["term_id"] for term in wire[item.item_id]["terms"]) for item in items
        },
        terms_hashes={item.item_id: canonical_hash(wire[item.item_id]["terms"]) for item in items},
        context_hashes={item.item_id: context_hash for item in items},
    )
    return RequestBatch(
        manifest=manifest, items=items, context=tuple(payload["context"]), payload=payload, budget=budget
    )


__all__ = ["PACKING_VERSION", "BatchBoundary", "BlockedItem", "PackingResult", "pack_requests"]
