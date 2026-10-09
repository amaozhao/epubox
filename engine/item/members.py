"""Materialize, validate and pack preflight-approved request members."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from itertools import pairwise
from types import MappingProxyType
from typing import Any, Literal, cast

from engine.agents.runtime import MAX_MODEL_INPUT_TOKENS
from engine.item.budget import measure_budget
from engine.item.inline import plain_text, projection_identities, validate_projection
from engine.item.packing import BatchBoundary, BlockedItem
from engine.item.request import context_suffix, marker_fields, select_terms, target_bindings
from engine.item.views import validate_source_views
from engine.schemas.bridge import AtomicDocument, AtomicItem, validate_batch_identity
from engine.schemas.budget import BUDGET_VERSION, BudgetLimits, BudgetResult, BudgetStage
from engine.schemas.contracts import (
    FrozenModel,
    GlossarySnapshot,
    ItemRecord,
    RegistryEntry,
    RequestManifest,
    canonical_hash,
)
from engine.schemas.members import MemberBatch, RequestMember, member_input_hash
from engine.services.preflight import PREFLIGHT_VERSION, PreflightReport, validate_list_pieces

PACKING_VERSION = 2
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
    "minimum",
    "minimum_unavoidable",
    "end",
]


def validate_member_target(member: RequestMember, target: str) -> None:
    """Validate a model result against one member's localized registry."""
    validate_projection(member, target, member.registry)


def materialize_members(inventories: Sequence[AtomicDocument], report: PreflightReport) -> tuple[RequestMember, ...]:
    """Convert the complete accepted preflight coverage into stable request members."""
    ordered = tuple(inventories)
    hashed = tuple(sorted(ordered, key=lambda value: value.document.document_id))
    if not ordered or report.check is None:
        raise ValueError("request members require an accepted preflight report")
    if report.source_hash != ordered[0].document.source_hash or any(
        inventory.document.source_hash != report.source_hash for inventory in ordered
    ):
        raise ValueError("preflight and atomic inventories must share one source identity")
    expected_maps = {inventory.document.document_id: canonical_hash(inventory.source_map) for inventory in ordered}
    if (
        report.check.source_hash != report.source_hash
        or report.map_hashes != expected_maps
        or report.check.map_hashes != expected_maps
    ):
        raise ValueError("preflight map identities differ from the atomic inventories")
    expected_resources = {
        inventory.document.resource.path: inventory.document.resource.source_sha256 for inventory in ordered
    }
    if report.resource_hashes != expected_resources:
        raise ValueError("preflight resource identities differ from the atomic inventories")
    if report.check.atoms_hash != canonical_hash(hashed) or report.atoms_hash != report.check.atoms_hash:
        raise ValueError("preflight atom identity differs from the atomic inventories")
    expected_budget = canonical_hash(
        {
            "version": BUDGET_VERSION,
            "preflight_version": PREFLIGHT_VERSION,
            "model": report.model,
            "limits": report.limits,
            "pieces": tuple(piece.model_dump(mode="json") for piece in report.pieces),
        }
    )
    if report.check.budget_hash != report.budget_hash or report.budget_hash != expected_budget:
        raise ValueError("preflight budget identity is incomplete")

    parents = {item.item_id: item for inventory in ordered for item in inventory.items}
    pieces_by_parent: dict[str, list[Any]] = {}
    for piece in report.pieces:
        if piece.item_id not in parents:
            raise ValueError(f"preflight piece references an unknown atom: {piece.item_id}")
        pieces_by_parent.setdefault(piece.item_id, []).append(piece)
    diagnostics = {diagnostic.item_id: diagnostic for diagnostic in report.diagnostics}
    if len(diagnostics) != len(report.diagnostics):
        raise ValueError("preflight diagnostics cannot repeat an atomic item")
    if set(diagnostics) != set(parents) or set(pieces_by_parent) != set(parents):
        raise ValueError("preflight pieces must cover every atomic item exactly once")

    preflight_hash = canonical_hash(report)
    result: list[RequestMember] = []
    for inventory in ordered:
        for parent in inventory.items:
            pieces = sorted(pieces_by_parent[parent.item_id], key=lambda value: value.source_span.byte_start)
            diagnostic = diagnostics[parent.item_id]
            if diagnostic.status == "blocked" or tuple(piece.piece_id for piece in pieces) != diagnostic.piece_ids:
                raise ValueError("preflight diagnostics differ from accepted piece coverage")
            _validate_coverage(parent, pieces)
            if any(
                piece.item_id != parent.item_id
                or piece.document_id != parent.document_id
                or piece.resource_path != inventory.document.resource.path
                or piece.translate.identity.tokenizer_model != report.model
                or piece.review.identity.tokenizer_model != report.model
                or not _budget_limits_match(piece.translate, report.limits)
                or not _budget_limits_match(piece.review, report.limits)
                for piece in pieces
            ):
                raise ValueError("preflight piece ownership or model identity differs from its parent")
            count = len(pieces)
            if parent.atomic_tag not in {None, "ul", "ol"} and (count != 1 or pieces[0].piece_id != parent.item_id):
                raise ValueError("hard atomic items cannot be split")
            for index, piece in enumerate(pieces):
                registry = parent.registry if count == 1 else _local_registry(parent, piece.source_projection)
                result.append(
                    RequestMember(
                        item_id=piece.piece_id,
                        parent_item_id=parent.item_id,
                        parent_hash=canonical_hash(parent),
                        preflight_hash=preflight_hash,
                        unit_id=parent.unit_id,
                        document_id=parent.document_id,
                        kind=parent.kind,
                        channel=parent.channel,
                        ordinal=parent.ordinal,
                        piece_index=index,
                        piece_count=count,
                        root_node_key=parent.node_key,
                        source_span=piece.source_span,
                        source_projection=piece.source_projection,
                        registry=registry,
                        atomic_tag=parent.atomic_tag,
                    )
                )
    return tuple(result)


def _budget_limits_match(budget: BudgetResult, limits: Mapping[str, int | float | None]) -> bool:
    identity = budget.identity
    expected = dict(limits)
    if identity.version != expected.pop("output_version", 2):
        return False
    expected.pop("minimum_source_tokens", None)
    expected.setdefault("context_unlimited", False)
    source_tokens, tolerance, hard_limit = expected["source_tokens"], expected.pop("source_tolerance_tokens", 0), expected.pop("source_hard_limit", None)
    if type(source_tokens) is not int or type(tolerance) is not int:
        return False
    if (hard_limit is not None and type(hard_limit) is not int) or identity.source_hard_limit != hard_limit:
        return False
    expected["source_tokens"] = min(source_tokens + tolerance, hard_limit) if hard_limit is not None else source_tokens + tolerance
    configured_input = expected.get("input_tokens")
    if type(configured_input) is not int:
        return False
    expected["input_tokens"] = min(configured_input, MAX_MODEL_INPUT_TOKENS)
    return {
        "source_tokens": identity.source_limit,
        "input_tokens": identity.input_limit,
        "output_tokens": identity.output_limit,
        "context_tokens": identity.context_limit,
        "context_unlimited": identity.context_unlimited,
        "safety_tokens": identity.safety_tokens,
        "target_ratio": identity.target_ratio,
    } == expected


def _validate_coverage(parent: AtomicItem, pieces: Sequence[Any]) -> None:
    if not pieces or pieces[0].source_span.byte_start != parent.source_span.byte_start:
        raise ValueError("preflight pieces must start at the parent source span")
    if pieces[-1].source_span.byte_end != parent.source_span.byte_end:
        raise ValueError("preflight pieces must end at the parent source span")
    if any(left.source_span.byte_end != right.source_span.byte_start for left, right in pairwise(pieces)):
        raise ValueError("preflight piece spans must be contiguous")
    if "".join(piece.source_projection for piece in pieces) != parent.source_projection:
        raise ValueError("preflight piece projections must exactly reconstruct their parent")
    if any(not piece.translate.fits or not piece.review.fits for piece in pieces):
        raise ValueError("request members require passing translate and review budgets")
    if len(pieces) == 1:
        if pieces[0].piece_id != parent.item_id:
            raise ValueError("a whole member must retain its parent item ID")
        return
    boundaries = parent.region.get("safe_boundaries")
    safe_offsets = (
        {
            offset
            for value in boundaries
            if isinstance(value, dict)
            for offset in (value.get("byte_offset"),)
            if type(offset) is int
        }
        if isinstance(boundaries, list)
        else set()
    )
    if parent.atomic_tag in {"ul", "ol"}:
        validate_list_pieces(parent, pieces)
    elif any(piece.source_span.byte_end not in safe_offsets for piece in pieces[:-1]):
        raise ValueError("split member seams must use frozen safe source boundaries")
    for piece in pieces:
        expected = (
            "pc-"
            + canonical_hash(
                {
                    "item_id": parent.item_id,
                    "bytes": (piece.source_span.byte_start, piece.source_span.byte_end),
                    "source": canonical_hash(piece.source_projection),
                }
            )[:32]
        )
        if piece.piece_id != expected:
            raise ValueError("split member ID differs from its stable source-range identity")


def _local_registry(parent: AtomicItem, projection: str) -> dict[str, RegistryEntry]:
    identities = set(projection_identities(projection))
    if not identities.issubset(parent.registry):
        raise ValueError("piece projection references a marker outside its parent")
    by_node = {entry.source_node_key: ref_id for ref_id, entry in parent.registry.items() if ref_id in identities}
    result: dict[str, RegistryEntry] = {}
    for ref_id in identities:
        entry = parent.registry[ref_id]
        parent_ref = entry.parent_ref
        if parent_ref != parent.node_key and parent_ref not in identities and parent_ref not in by_node:
            parent_ref = parent.node_key
        result[ref_id] = entry.model_copy(
            update={
                "parent_ref": parent_ref,
                "fixed_order": tuple(value for value in entry.fixed_order if value in identities),
            }
        )
    validate_projection(projection, projection, result)
    return result


def merge_member_targets(
    parent: AtomicItem,
    members: Sequence[RequestMember],
    results: Mapping[str, ItemRecord],
) -> str:
    """Validate every sibling before returning one complete parent target."""
    ordered = tuple(sorted(members, key=lambda member: member.piece_index))
    if not ordered or len(ordered) != ordered[0].piece_count:
        raise ValueError("merge requires every sibling member")
    if tuple(member.piece_index for member in ordered) != tuple(range(len(ordered))):
        raise ValueError("merge requires ordered contiguous sibling pieces")
    if any(
        member.parent_item_id != parent.item_id
        or member.parent_hash != canonical_hash(parent)
        or member.unit_id != parent.unit_id
        or member.piece_count != len(ordered)
        or member.preflight_hash != ordered[0].preflight_hash
        for member in ordered
    ):
        raise ValueError("member identity differs from its parent or siblings")
    if set(results) != {member.item_id for member in ordered}:
        raise ValueError("merge results must exactly cover every sibling")
    targets: list[str] = []
    for member in ordered:
        record = results[member.item_id]
        if record.item_id != member.item_id or record.segment_id != member.item_id:
            raise ValueError("member result identity differs from its request member")
        target = record.target_projection
        if target is None or record.target_hash != canonical_hash(target):
            raise ValueError("member merge requires a hash-bound target")
        validate_member_target(member, target)
        targets.append(target)
    merged = "".join(targets)
    validate_projection(parent, merged, parent.registry)
    return merged


class MemberIndex:
    """Verified member lookup, text and bounded preceding context."""

    def __init__(
        self,
        inventories: Sequence[AtomicDocument],
        report: PreflightReport,
        members: Sequence[RequestMember] | None = None,
    ) -> None:
        saved = tuple(AtomicDocument.model_validate(value.model_dump(mode="python")) for value in inventories)
        expected = materialize_members(saved, report)
        supplied = expected if members is None else tuple(members)
        if supplied != expected:
            raise ValueError("request members differ from deterministic preflight materialization")
        documents = {inventory.document.document_id: inventory.document for inventory in saved}
        for document in documents.values():
            validate_source_views(document)
        self.inventories = saved
        self.documents = MappingProxyType(documents)
        self.members = supplied
        self.members_by_id = MappingProxyType({member.item_id: member for member in supplied})
        self._glossary_digest: tuple[GlossarySnapshot, str] | None = None
        lane_positions: dict[tuple[str, str], int] = {}
        self._lane_positions: dict[str, int] = {}
        for member in supplied:
            lane = (member.document_id, member.channel)
            self._lane_positions[member.item_id] = lane_positions.get(lane, 0)
            lane_positions[lane] = self._lane_positions[member.item_id] + 1
        self.items_by_id = self.members_by_id
        self.source_hash = report.source_hash
        self.source_tokens = MappingProxyType(
            {piece.piece_id: piece.translate.source_tokens for piece in report.pieces}
        )
        body_tokens: dict[str, int] = {}
        for member in supplied:
            if member.channel == "body":
                body_tokens[member.document_id] = (
                    body_tokens.get(member.document_id, 0) + self.source_tokens[member.item_id]
                )
        self.body_tokens = MappingProxyType(body_tokens)
        self.document_order = tuple(inventory.document.document_id for inventory in saved)
        parents = {item.item_id: item for inventory in saved for item in inventory.items}
        self._texts = MappingProxyType(
            {
                member.item_id: (
                    "\n".join(
                        documents[member.document_id].source_views[view_id].text
                        for view_id in parents[member.parent_item_id].source_view_ids
                    )
                    if member.piece_count == 1
                    else plain_text(member.source_projection)
                )
                for member in supplied
            }
        )
        self._previous: dict[str, tuple[tuple[RequestMember, str], ...]] = {}
        channels: dict[tuple[str, str], tuple[tuple[RequestMember, str], ...]] = {}
        for member in supplied:
            key = (member.document_id, member.channel)
            prior = channels.get(key, ())
            self._previous[member.item_id] = prior
            if text := self._texts[member.item_id]:
                channels[key] = (*prior, (member, text))[-2:]

    def validate_items(
        self,
        items: tuple[RequestMember, ...],
        *,
        relaxed_adjacency: bool = False,
        sparse: bool = False,
    ) -> None:
        if not items or len({item.item_id for item in items}) != len(items):
            raise ValueError("request candidates must be non-empty and unique")
        if any(self.members_by_id.get(item.item_id) != item for item in items):
            raise ValueError("request candidate is not owned by this member index")
        if len({(item.document_id, item.channel) for item in items}) != 1:
            raise ValueError("request candidates must share one document and channel")
        for left, right in pairwise(items):
            if not (
                self._lane_positions[right.item_id] > self._lane_positions[left.item_id]
                if sparse
                else self.adjacent(left, right)
                if relaxed_adjacency
                else _adjacent(left, right)
            ):
                raise ValueError("request candidates must follow adjacent source reading order")

    def adjacent(self, left: RequestMember, right: RequestMember) -> bool:
        return (
            left.document_id == right.document_id
            and left.channel == right.channel
            and self._lane_positions.get(right.item_id) == self._lane_positions.get(left.item_id, -2) + 1
        )

    def text(self, item: RequestMember) -> str:
        if self.members_by_id.get(item.item_id) != item:
            raise ValueError("request member is not owned by this index")
        return self._texts[item.item_id]

    def preceding(
        self,
        items: tuple[RequestMember, ...],
        count: int,
        *,
        relaxed_adjacency: bool = False,
        sparse: bool = False,
    ) -> tuple[tuple[RequestMember, str], ...]:
        self.validate_items(items, relaxed_adjacency=relaxed_adjacency, sparse=sparse)
        if type(count) is not int or not 0 <= count <= 2:
            raise ValueError("context_count must be 0, 1, or 2")
        return self._previous[items[0].item_id][-count:] if count else ()


def build_member_payload(
    stage: BudgetStage,
    items: tuple[RequestMember, ...],
    glossary: GlossarySnapshot,
    index: MemberIndex,
    *,
    request_id: str,
    targets: Mapping[str, ItemRecord] | None = None,
    revisions: Mapping[str, int] | None = None,
    context_count: int = 2,
    compact_constraints: bool = False,
    relaxed_adjacency: bool = False,
    sparse: bool = False,
    feedback: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if stage not in {"translate", "review"} or not request_id:
        raise ValueError("member payload requires a supported stage and request ID")
    if glossary.source_hash != index.source_hash:
        raise ValueError("glossary and member index identities differ")
    index.validate_items(items, relaxed_adjacency=relaxed_adjacency, sparse=sparse)
    contexts = tuple(
        (item, context_suffix(text, 400))
        for item, text in index.preceding(
            items,
            context_count,
            relaxed_adjacency=relaxed_adjacency,
            sparse=sparse,
        )
    )
    review = _member_review(stage, items, targets, revisions)
    wire_items: list[dict[str, Any]] = []
    for item in items:
        validate_member_target(item, item.source_projection)
        hints, constraints = marker_fields(item)
        if compact_constraints and all(
            entry.movement in {"locked", "fixed"} and not entry.reorder_allowed for entry in item.registry.values()
        ):
            # Exact source order already governs every reference; retain the full registry locally.
            constraints = {ref: value | {"fixed_order": []} for ref, value in constraints.items()}
        terms = select_terms(item, glossary.terms, index, contexts)
        wire: dict[str, Any] = {
            "item_id": item.item_id,
            "source": item.source_projection,
            "terms": terms,
            "hints": hints,
            "constraints": constraints,
        }
        if feedback and item.item_id in feedback:
            wire["repair"] = feedback[item.item_id]
        if stage == "review":
            record = review[item.item_id]
            target = cast(str, record.target_projection)
            wire.update(
                target=target,
                base_revision=(revisions or {})[item.unit_id],
                applicability={
                    "terminology": any(term["role"] == "target" for term in terms),
                    "bindings": bool(item.registry),
                },
                bindings=target_bindings(item, target),
            )
        wire_items.append(wire)
    payload: dict[str, Any] = {
        "protocol": "epubox-text-1" if stage == "translate" else "epubox-review-2",
        "prompt_version": "epubox-members-1",
        "request_id": request_id,
        "items": wire_items,
        "context": [text for _, text in contexts],
    }
    if stage == "translate":
        payload["target_language"] = "zh-Hans"
    return payload


def fit_member_payload(
    stage: BudgetStage,
    items: tuple[RequestMember, ...],
    glossary: GlossarySnapshot,
    index: MemberIndex,
    limits: BudgetLimits,
    *,
    request_id: str,
    targets: Mapping[str, ItemRecord] | None = None,
    revisions: Mapping[str, int] | None = None,
    tokenizer_model: str = "gpt-3.5-turbo",
    sparse: bool = False,
    feedback: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], BudgetResult]:
    """Keep fitting legacy requests unchanged; compact only an otherwise blocked singleton."""
    payload: dict[str, Any] = {}
    budget: BudgetResult | None = None
    for count, compact in ((2, False), (1, False), (0, False), (0, True)):
        if sparse and count:
            continue
        if compact and len(items) != 1 and not limits.minimum_source_tokens:
            break
        if not compact and budget is not None and not payload["context"]:
            continue
        payload = build_member_payload(
            stage,
            items,
            glossary,
            index,
            request_id=request_id,
            targets=targets,
            revisions=revisions,
            context_count=count,
            compact_constraints=compact,
            relaxed_adjacency=bool(limits.minimum_source_tokens),
            sparse=sparse,
            feedback=feedback,
        )
        if limits.output_version in {5, 6, 7}:
            payload["wire_version"] = "epubox-wire-5"
        budget = measure_budget(stage=stage, payload=payload, limits=limits, tokenizer_model=tokenizer_model)
        if budget.fits:
            return payload, budget
    assert budget is not None
    if limits.output_version in {6, 7}:
        # Slot replies restore every marker locally; keep full constraints in the member registry.
        for item in payload["items"]:
            item["constraints"] = {}
        budget = measure_budget(stage=stage, payload=payload, limits=limits, tokenizer_model=tokenizer_model)
        if not budget.fits and stage == "review":
            payload["wire_version"] = "epubox-wire-8"
            budget = measure_budget(stage=stage, payload=payload, limits=limits, tokenizer_model=tokenizer_model)
    return payload, budget


def _member_review(
    stage: BudgetStage,
    items: tuple[RequestMember, ...],
    targets: Mapping[str, ItemRecord] | None,
    revisions: Mapping[str, int] | None,
) -> Mapping[str, ItemRecord]:
    if stage == "translate":
        if targets is not None or revisions is not None:
            raise ValueError("translation payloads cannot include review targets")
        return {}
    if targets is None or revisions is None or set(targets) != {item.item_id for item in items}:
        raise ValueError("review targets must exactly cover the candidate members")
    if set(revisions) != {item.unit_id for item in items}:
        raise ValueError("review revisions must exactly cover the candidate Units")
    for item in items:
        record = targets[item.item_id]
        if record.item_id != item.item_id or record.segment_id != item.item_id:
            raise ValueError("review target identity differs from its request member")
        if record.target_projection is None or record.target_hash != canonical_hash(record.target_projection):
            raise ValueError("review requires a current hash-bound target")
        validate_member_target(item, record.target_projection)
        if type(revisions[item.unit_id]) is not int or revisions[item.unit_id] < 0:
            raise ValueError("review revisions must be non-negative integers")
    return targets


class MemberPackingResult(FrozenModel):
    format: Literal["epubox-packing-2"] = "epubox-packing-2"
    stage: BudgetStage
    source_hash: str
    freeze_id: str
    batches: tuple[MemberBatch, ...]
    boundaries: tuple[BatchBoundary, ...]
    blocked: tuple[BlockedItem, ...]
    skipped: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.blocked


def pack_members(
    stage: BudgetStage,
    items: Sequence[RequestMember],
    glossary: GlossarySnapshot,
    index: MemberIndex,
    limits: BudgetLimits,
    *,
    targets: Mapping[str, ItemRecord] | None = None,
    revisions: Mapping[str, int] | None = None,
    record_versions: Mapping[str, int] | None = None,
    plan_epochs: Mapping[str, int] | None = None,
    completed: Collection[str] = (),
    tokenizer_model: str = "gpt-3.5-turbo",
    sparse: bool = False,
    feedback: Mapping[str, str] | None = None,
    whole: bool = False,
) -> MemberPackingResult:
    members = tuple(items)
    if stage not in {"translate", "review"}:
        raise ValueError("member planning requires translate or review stage")
    if stage == "translate" and (targets is not None or revisions is not None):
        raise ValueError("translation planning cannot include review targets")
    if len({item.item_id for item in members}) != len(members):
        raise ValueError("member planning cannot repeat an item")
    if whole and completed:
        raise ValueError("whole-member planning cannot skip completed items")
    if feedback and (
        not set(feedback).issubset(item.item_id for item in members)
        or any(not isinstance(value, str) or not value.strip() or len(value) > 1200 for value in feedback.values())
    ):
        raise ValueError("repair feedback must contain bounded messages for requested members")
    if not set(completed).issubset(index.items_by_id):
        raise ValueError("completed checkpoint references an unknown member")
    if glossary.source_hash != index.source_hash:
        raise ValueError("frozen glossary differs from the member index")
    if stage == "review" and (
        targets is None
        or revisions is None
        or set(targets) != {item.item_id for item in members}
        or set(revisions) != {item.unit_id for item in members}
    ):
        raise ValueError("review state must exactly cover every requested member and Unit")
    for versions in (revisions, record_versions, plan_epochs):
        if versions is not None and any(type(value) is not int or value < 0 for value in versions.values()):
            raise ValueError("saved revisions and versions must be non-negative integers")
    for item in members:
        index.validate_items((item,))
    _validate_order(members, index.document_order)
    cached = index._glossary_digest
    if cached is None or cached[0] is not glossary:
        cached = (glossary, canonical_hash(glossary))
        index._glossary_digest = cached
    glossary_hash = cached[1]
    member_hashes = {item.item_id: canonical_hash(item) for item in members}
    target_hashes = {item.item_id: canonical_hash((targets or {}).get(item.item_id)) for item in members}
    limit_values = limits.to_dict()
    batches: list[MemberBatch] = []
    boundaries: list[BatchBoundary] = []
    blocked: list[BlockedItem] = []
    skipped: list[str] = []
    pending: tuple[RequestMember, ...] = ()
    pending_payload: dict[str, Any] | None = None
    pending_budget: BudgetResult | None = None

    def adjacent(left: RequestMember, right: RequestMember) -> bool:
        if sparse:
            return left.document_id == right.document_id and left.channel == right.channel
        return index.adjacent(left, right) if limits.minimum_source_tokens else _adjacent(left, right)

    def close(reason: BoundaryReason, failures: tuple[str, ...] = ()) -> None:
        nonlocal pending, pending_payload, pending_budget
        if pending:
            assert pending_payload is not None and pending_budget is not None
            batch = _batch(pending, glossary, glossary_hash, pending_payload, pending_budget, revisions, record_versions, plan_epochs, sparse)  # fmt: skip
            batches.append(batch)
            boundaries.append(BatchBoundary(request_id=batch.manifest.request_id, reason=reason, failures=failures))
            pending, pending_payload, pending_budget = (), None, None

    def candidate(values: tuple[RequestMember, ...]) -> tuple[dict[str, Any], BudgetResult]:
        identity = {
            "version": PACKING_VERSION,
            "stage": stage,
            "source": glossary.source_hash,
            "freeze": glossary_hash,
            "members": [member_hashes[item.item_id] for item in values],
            "limits": limit_values,
            "model": tokenizer_model,
            "targets": {item.item_id: target_hashes[item.item_id] for item in values},
            "revisions": {item.unit_id: (revisions or {}).get(item.unit_id, 0) for item in values},
            "versions": {item.unit_id: (record_versions or {}).get(item.unit_id, 0) for item in values},
            "epochs": {item.unit_id: (plan_epochs or {}).get(item.unit_id, 0) for item in values},
        }
        if sparse:
            identity["sparse"] = True
        selected_feedback = {
            item.item_id: feedback[item.item_id] for item in values if feedback and item.item_id in feedback
        }
        if selected_feedback:
            identity["feedback"] = selected_feedback
        request_id = "tx-" + canonical_hash(identity)[:32]
        return fit_member_payload(
            stage,
            values,
            glossary,
            index,
            limits,
            request_id=request_id,
            targets={item.item_id: targets[item.item_id] for item in values} if targets is not None else None,
            revisions={item.unit_id: revisions[item.unit_id] for item in values}
            if stage == "review" and revisions is not None
            else None,
            tokenizer_model=tokenizer_model,
            sparse=sparse,
            feedback=selected_feedback,
        )

    for position, item in enumerate(members):
        if whole and position + 1 < len(members):
            continue
        if item.item_id in completed:
            if not sparse:
                close("completed")
            skipped.append(item.item_id)
            continue
        if pending and not adjacent(pending[-1], item):
            previous = pending[-1]
            close(
                "resource"
                if previous.document_id != item.document_id
                else "channel"
                if previous.channel != item.channel
                else "adjacency"
            )
        if pending and item.kind == "heading" and position + 1 < len(members):
            following = members[position + 1]
            if following.item_id not in completed and adjacent(item, following):
                _, pair_budget = candidate((item, following))
                if pair_budget.fits:
                    _, combined_budget = candidate((*pending, item, following))
                    if not combined_budget.fits:
                        close("heading", combined_budget.failures)
        proposed = members if whole else (*pending, item) if pending else (item,)
        payload, budget = candidate(proposed)
        if not budget.fits and pending:
            reason = cast(BoundaryReason, budget.failures[0].split(" ", 1)[0])
            close(reason, budget.failures)
            proposed = (item,)
            payload, budget = candidate(proposed)
        if budget.fits:
            pending, pending_payload, pending_budget = proposed, payload, budget
        else:
            close("blocked", budget.failures)
            for blocked_item in members if whole else (item,):
                blocked.append(
                    BlockedItem(
                        item_id=blocked_item.item_id,
                        document_id=blocked_item.document_id,
                        resource_path=index.documents[blocked_item.document_id].resource.path,
                        source_span=blocked_item.source_span,
                        atomic_tag=blocked_item.atomic_tag,
                        budget=budget,
                    )
                )
    close("end")
    minimum = limits.minimum_source_tokens
    if minimum:
        position = 0
        while position < len(batches):
            current = batches[position]
            if current.items[0].channel != "body" or current.budget.source_tokens >= minimum:
                position += 1
                continue
            previous_position = following_position = None
            for neighbor in range(position - 1, -1, -1):
                other = batches[neighbor]
                if other.items[0].document_id != current.items[0].document_id:
                    break
                if other.items[0].channel == "body":
                    if adjacent(other.items[-1], current.items[0]):
                        previous_position = neighbor
                    break
            for neighbor in range(position + 1, len(batches)):
                other = batches[neighbor]
                if other.items[0].document_id != current.items[0].document_id:
                    break
                if other.items[0].channel == "body":
                    if adjacent(current.items[-1], other.items[0]):
                        following_position = neighbor
                    break
            if previous_position is not None:
                left_position, right_position = previous_position, position
            elif following_position is not None:
                left_position, right_position = position, following_position
            else:
                boundaries[position] = BatchBoundary(
                    request_id=current.manifest.request_id,
                    reason="minimum_unavoidable",
                    failures=(
                        (
                            f"body source budget {current.budget.source_tokens} is below minimum {minimum}; "
                            "the HTML body has no adjacent request batch"
                        ),
                    ),
                )
                position += 1
                continue
            left_batch, right_batch = batches[left_position], batches[right_position]
            combined_items = (*left_batch.items, *right_batch.items)
            payload, budget = candidate(combined_items)
            if budget.fits:
                merged = _batch(
                    combined_items,
                    glossary,
                    glossary_hash,
                    payload,
                    budget,
                    revisions,
                    record_versions,
                    plan_epochs,
                    sparse,
                )
                final = boundaries[right_position]
                batches[left_position] = merged
                boundaries[left_position] = final.model_copy(update={"request_id": merged.manifest.request_id})
                del batches[right_position]
                del boundaries[right_position]
                position = max(0, left_position - 1)
                continue
            prefix = 0
            total = sum(index.source_tokens[item.item_id] for item in combined_items)
            splits: list[tuple[int, int]] = []
            for split, item in enumerate(combined_items[:-1], 1):
                prefix += index.source_tokens[item.item_id]
                if prefix >= minimum and total - prefix >= minimum:
                    splits.append((abs(prefix - (total - prefix)), split))
            for _, split in sorted(splits):
                left_items, right_items = combined_items[:split], combined_items[split:]
                if left_items[-1].kind == "heading":
                    continue
                left_payload, left_budget = candidate(left_items)
                right_payload, right_budget = candidate(right_items)
                if (
                    left_budget.fits
                    and right_budget.fits
                    and left_budget.source_tokens >= minimum
                    and right_budget.source_tokens >= minimum
                ):
                    left = _batch(
                        left_items,
                        glossary,
                        glossary_hash,
                        left_payload,
                        left_budget,
                        revisions,
                        record_versions,
                        plan_epochs,
                        sparse,
                    )
                    right = _batch(
                        right_items,
                        glossary,
                        glossary_hash,
                        right_payload,
                        right_budget,
                        revisions,
                        record_versions,
                        plan_epochs,
                        sparse,
                    )
                    break
            else:
                left = right = None
            if left is not None and right is not None:
                final = boundaries[right_position]
                batches[left_position], batches[right_position] = left, right
                boundaries[left_position] = BatchBoundary(request_id=left.manifest.request_id, reason="minimum")
                boundaries[right_position] = final.model_copy(update={"request_id": right.manifest.request_id})
            else:
                boundaries[position] = BatchBoundary(
                    request_id=current.manifest.request_id,
                    reason="minimum_unavoidable",
                    failures=(
                        f"body source budget {current.budget.source_tokens} is below minimum {minimum}; "
                        + ", ".join(budget.failures or ("no valid whole-member rebalance",)),
                    ),
                )
            position += 1
        if (
            limits.output_version in {5, 6, 7}
            and stage == "translate"
            and not sparse
            and not any((record_versions or {}).values())
        ):
            fitting_batches: list[MemberBatch] = []
            fitting_boundaries: list[BatchBoundary] = []
            for value, boundary in zip(batches, boundaries, strict=True):
                document = value.items[0].document_id
                if (
                    value.items[0].channel == "body"
                    and value.budget.source_tokens < minimum
                    and index.body_tokens.get(document, 0) >= minimum
                    and not (
                        limits.output_version in {6, 7}
                        and boundary.reason == "minimum_unavoidable"
                        and any(
                            budget in failure
                            for failure in boundary.failures
                            for budget in ("output budget", "input budget", "context budget")
                        )
                    )
                    and not any(
                        index.members_by_id[item].channel == "body"
                        and index.members_by_id[item].document_id == document
                        for item in completed
                    )
                ):
                    failure = (
                        f"body chunk has {value.budget.source_tokens} tokens, below required minimum {minimum}; "
                        + "; ".join(boundary.failures)
                    )
                    blocked.extend(
                        BlockedItem(
                            item_id=item.item_id,
                            document_id=item.document_id,
                            resource_path=index.documents[item.document_id].resource.path,
                            source_span=item.source_span,
                            atomic_tag=item.atomic_tag,
                            budget=value.budget,
                            reason=failure,
                        )
                        for item in value.items
                    )
                else:
                    fitting_batches.append(value)
                    fitting_boundaries.append(boundary)
            batches, boundaries = fitting_batches, fitting_boundaries
    return MemberPackingResult(
        stage=stage,
        source_hash=glossary.source_hash,
        freeze_id=glossary.freeze_id,
        batches=tuple(batches),
        boundaries=tuple(boundaries),
        blocked=tuple(blocked),
        skipped=tuple(skipped),
    )


def _adjacent(left: RequestMember, right: RequestMember) -> bool:
    if left.document_id != right.document_id or left.channel != right.channel:
        return False
    if left.parent_item_id == right.parent_item_id:
        return left.piece_count == right.piece_count and left.piece_index + 1 == right.piece_index
    return left.piece_index + 1 == left.piece_count and right.piece_index == 0 and left.ordinal + 1 == right.ordinal


def _validate_order(items: tuple[RequestMember, ...], document_order: tuple[str, ...]) -> None:
    positions = {document_id: index for index, document_id in enumerate(document_order)}
    previous: RequestMember | None = None
    seen: set[str] = set()
    for item in items:
        if previous is not None and previous.document_id == item.document_id:
            if (previous.ordinal, previous.piece_index) >= (item.ordinal, item.piece_index):
                raise ValueError("member planning requires source and piece order")
        elif (
            item.document_id in seen
            or previous is not None
            and positions[item.document_id] < positions[previous.document_id]
        ):
            raise ValueError("member planning cannot return to an earlier resource")
        seen.add(item.document_id)
        previous = item


def _batch(
    items: tuple[RequestMember, ...],
    glossary: GlossarySnapshot,
    glossary_hash: str,
    payload: dict[str, Any],
    budget: BudgetResult,
    revisions: Mapping[str, int] | None,
    versions: Mapping[str, int] | None,
    epochs: Mapping[str, int] | None,
    sparse: bool = False,
) -> MemberBatch:
    wire = {entry["item_id"]: entry for entry in payload["items"]}
    context_hash = canonical_hash(payload["context"])
    manifest = RequestManifest(
        request_id=payload["request_id"],
        stage=budget.stage,
        owner_kind="translation_item",
        owner_id=items[0].item_id,
        item_ids=tuple(item.item_id for item in items),
        input_hashes={
            item.item_id: member_input_hash(item, glossary.freeze_id, wire[item.item_id], context_hash)
            for item in items
        },
        wire_hash=budget.wire_hash,
        sparse=sparse,
        context_unlimited=budget.identity.context_unlimited,
        output_unlimited=budget.identity.output_limit is None,
        source_hard_limit=budget.identity.source_hard_limit,
        feedback_by_item={
            item.item_id: wire[item.item_id]["repair"] for item in items if "repair" in wire[item.item_id]
        },
        record_versions={item.unit_id: (versions or {}).get(item.unit_id, 0) for item in items},
        item_unit_ids={item.item_id: (item.unit_id,) for item in items},
        unit_document_ids={item.unit_id: item.document_id for item in items},
        plan_epochs={item.unit_id: (epochs or {}).get(item.unit_id, 0) for item in items},
        revisions={item.unit_id: (revisions or {}).get(item.unit_id, 0) for item in items},
        target_hashes={item.item_id: canonical_hash(wire[item.item_id]["target"]) for item in items}
        if budget.stage == "review"
        else {},
        glossary_file_sha256=glossary_hash,
        freeze_id=glossary.freeze_id,
        term_ids_by_item={
            item.item_id: tuple(term["term_id"] for term in wire[item.item_id]["terms"]) for item in items
        },
        terms_hashes={item.item_id: canonical_hash(wire[item.item_id]["terms"]) for item in items},
        context_hashes={item.item_id: context_hash for item in items},
    )
    batch = MemberBatch(
        manifest=manifest, items=items, context=tuple(payload["context"]), payload=payload, budget=budget
    )
    return validate_batch_identity(batch, allow_pieces=True)


__all__ = [
    "MemberIndex",
    "MemberPackingResult",
    "build_member_payload",
    "materialize_members",
    "merge_member_targets",
    "pack_members",
    "validate_member_target",
]
