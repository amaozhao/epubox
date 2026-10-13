"""Durable child checkpoints for oversized legacy list members."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, cast

from engine.agents.workflow import _frame, _target_error, _wire_items
from engine.item.inline import plain_text
from engine.schemas.contracts import (
    ItemRecord,
    ItemStatus,
    RequestManifest,
    canonical_hash,
    canonical_json_bytes,
    parse_contract,
)
from engine.schemas.members import MemberBatch, RequestMember
from engine.schemas.parts import PartPlan
from engine.services import frames, state
from engine.services.atomic import CorruptRecord, IdentityMismatch, StaleWrite, safe_id
from engine.services.journal import BodyJournal
from engine.services.partition import PartSession, merge_targets, plan_path, split_target, validate_plan
from engine.services.working import BudgetCache


def open_parts(base, parent_id: str, *, measure_tokens: bool = True) -> tuple[PartPlan, dict[str, ItemRecord]]:
    """Read and validate an existing part plan and its exact child inventory."""
    path = plan_path(base.store.root, parent_id)
    try:
        plan = parse_contract(state.read(path), PartPlan, "epubox-parts-1")
        if plan.parent_id != parent_id:
            raise IdentityMismatch("part plan filename differs from its parent")
        validate_plan(base, plan, measure_tokens=measure_tokens)
    except Exception as error:
        if isinstance(error, (IdentityMismatch, ValueError)):
            raise
        raise CorruptRecord(f"invalid part plan {path}: {error}") from error
    expected = {safe_id(child.item_id) for child in plan.children}
    actual = {path.stem for path in state.glob(path.parent / "results", "*.json")}
    if actual != expected:
        raise IdentityMismatch("part result inventory differs from its immutable plan")
    records: dict[str, ItemRecord] = {}
    for child in plan.children:
        child_path = path.parent / "results" / f"{safe_id(child.item_id)}.json"
        try:
            record = ItemRecord.model_validate_json(state.read(child_path))
        except Exception as error:
            raise CorruptRecord(f"invalid part result {child_path}: {error}") from error
        if record.item_id != child.item_id or record.segment_id != child.item_id:
            raise IdentityMismatch("part result ownership differs from its immutable plan")
        records[child.item_id] = record
    return plan, records


def composite_parts(base, record: ItemRecord) -> tuple[PartPlan, dict[str, ItemRecord]]:
    """Validate the persisted child summary that proves one composed parent."""
    summary = record.checks.get("parts")
    if not isinstance(summary, Mapping):
        raise IdentityMismatch("composed parent lacks its part summary")
    plan, records = open_parts(base, record.item_id, measure_tokens=False)
    parent = base.index.members_by_id.get(record.item_id)
    if (
        parent is None
        or record.status != ItemStatus.REVIEWED
        or record.stage != "reviewed"
        or record.failure is not None
        or record.next_action is not None
        or any(value.status != ItemStatus.REVIEWED for value in records.values())
    ):
        raise IdentityMismatch("composed parent has incomplete part results")
    child_hashes = {item_id: canonical_hash(value) for item_id, value in records.items()}
    merged = merge_targets(
        cast(Any, parent),
        plan,
        {item_id: cast(str, value.target_projection) for item_id, value in records.items()},
    )
    _validate_parent_target(base, parent.item_id, merged)
    expected = {
        "plan_hash": canonical_hash(plan),
        "child_hashes": child_hashes,
        "merged_target_hash": canonical_hash(merged),
    }
    provenance = _seed_provenance(plan)
    if (
        dict(summary) != expected
        or record.checks != provenance | {"parts": expected}
        or record.target_projection != merged
        or record.target_hash != canonical_hash(merged)
    ):
        raise IdentityMismatch("composed parent differs from its persisted part results")
    if plan.seed is not None and any(
        getattr(record, name) != getattr(plan.seed, name)
        for name in (
            "item_id",
            "segment_id",
            "selected_term_ids",
            "term_applicability",
            "terms_hash",
            "context_hash",
        )
    ):
        raise IdentityMismatch("composed parent changed its frozen seed metadata")
    return plan, records


def validate_summary(base, record: ItemRecord) -> None:
    composite_parts(base, record)


def _snapshot_identity(plan: PartPlan, records: Mapping[str, ItemRecord]) -> str:
    return canonical_hash(
        {
            "rule": "part-proof-2",
            "plan": plan,
            "children": {item_id: canonical_hash(value) for item_id, value in sorted(records.items())},
        }
    )


class JointPartSession(PartSession):
    """One proof-only view over several independently owned part plans."""

    def __init__(self, base, plans: Sequence[PartPlan]):
        ordered = tuple(plans)
        if not ordered or len({plan.parent_id for plan in ordered}) != len(ordered):
            raise IdentityMismatch("joint part proof requires unique parent plans")
        children = tuple(child for plan in ordered for child in plan.children)
        if len({child.item_id for child in children}) != len(children):
            raise IdentityMismatch("part plans cannot share derived children")
        self.base = base
        self.plans = ordered
        self._plans_by_parent = {plan.parent_id: plan for plan in ordered}
        self._owners = {child.item_id: plan.parent_id for plan in ordered for child in plan.children}
        self._child_hashes = {child.item_id: canonical_hash(child) for child in children}
        self._plan_texts = {plan.parent_id: state.text(plan_path(base.store.root, plan.parent_id)) for plan in ordered}
        if any(self._plan_texts[plan.parent_id].encode() != canonical_json_bytes(plan) for plan in ordered):
            raise IdentityMismatch("saved part plan changed after snapshot validation")
        self.store = base.store
        self.prepared = base.prepared
        self._saved_batches = BudgetCache(16 * 1024 * 1024)
        self._frame_requests = BudgetCache(16 * 1024 * 1024)
        self.index = _joint_scoped_index(base, ordered)
        self._source_tokens_verified = False
        self._prepared_batches = {}
        self._proof_requests: Mapping[str, RequestManifest] = {}

    def owner(self, item_id: str) -> PartPlan | None:
        parent = self._owners.get(item_id)
        return self._plans_by_parent.get(parent) if parent is not None else None

    def _request_owner(self, request: RequestManifest) -> PartPlan:
        owners = {self._owners.get(item_id) for item_id in request.item_ids}
        if None in owners or len(owners) != 1:
            raise IdentityMismatch("persisted part request crosses immutable plans")
        owner = next(iter(owners))
        assert owner is not None
        return self._plans_by_parent[owner]

    def initial_request_wire(
        self, item_id: str, prior: ItemRecord | None = None
    ) -> tuple[RequestManifest, Mapping[str, Any]] | None:
        if self.owner(item_id) is None:
            return None
        value = super().initial_request_wire(item_id, prior)
        if value is not None:
            self._request_owner(value[0])
        return value

    def verify(self):
        # The joint proof is one immutable transaction. Rechecking the whole
        # ready fingerprint for every historical request turns thousands of
        # request proofs into repeated whole-book tuple comparisons. The
        # closing verify_snapshots call rebinds the base and every plan before
        # any result is accepted by the root journal.
        return self.prepared

    def verify_snapshots(self) -> None:
        """Rebind every plan once after the shared request traversal."""
        self.base.verify()
        for plan in self.plans:
            if state.text(plan_path(self.store.root, plan.parent_id)) != self._plan_texts[plan.parent_id]:
                raise IdentityMismatch("saved part plan changed during joint verification")
            parent = self.base.index.members_by_id.get(plan.parent_id)
            if parent is None or canonical_hash(parent) != plan.parent_hash:
                raise IdentityMismatch("part plan parent identity changed during joint verification")

    def owns(self, member: RequestMember) -> bool:
        return (
            self.owner(member.item_id) is not None
            and self.index.members_by_id.get(member.item_id) == member
            and canonical_hash(member) == self._child_hashes.get(member.item_id)
        )

    def verify_batch(self, batch, *, initial: bool = False, sent_request: RequestManifest | None = None) -> None:
        self._request_owner(batch.manifest)
        super().verify_batch(batch, initial=initial, sent_request=sent_request)

    def result_path(self, item_id: str):
        plan = self.owner(item_id)
        if plan is None:
            raise IdentityMismatch("part result references an unknown child")
        return self.store.root / "parts" / safe_id(plan.parent_id) / "results" / f"{safe_id(item_id)}.json"


def _joint_scoped_index(base, plans: Sequence[PartPlan]):
    """Preserve each plan's independent order and context inside one index."""
    index = copy.copy(base.index)
    children = tuple(child for plan in plans for child in plan.children)
    members = MappingProxyType({child.item_id: child for child in children})
    index.members = children
    index.members_by_id = members
    index.items_by_id = members
    positions: dict[str, int] = {}
    for plan in plans:
        for position, child in enumerate(plan.children):
            positions[child.item_id] = position
    index._lane_positions = positions
    index._texts = MappingProxyType({child.item_id: plain_text(child.source_projection) for child in children})
    index._previous_ids = {child.item_id: () for child in children}
    index.source_tokens = MappingProxyType({child.item_id: 0 for child in children})
    index.body_tokens = MappingProxyType({})
    return index


def verify_composite(journal: BodyJournal, record: ItemRecord) -> None:
    plan, records = composite_parts(journal.session, record)
    snapshot_hash = _snapshot_identity(plan, records)
    part = PartJournal(
        journal,
        plan.parent_id,
        _snapshot=(plan, records),
        _snapshot_hash=snapshot_hash,
        _proof_only=True,
    )
    if part.plan != plan or part._records != records:
        raise IdentityMismatch("part evidence changed during composite verification")
    journal._proof_metrics["part_snapshots"] += 1


def verify_composites(journal: BodyJournal, parents: Sequence[ItemRecord]) -> None:
    """Prove several composed parents through one owner-aware child traversal."""
    snapshots: list[tuple[PartPlan, dict[str, ItemRecord]]] = []
    parent_ids: set[str] = set()
    child_ids: set[str] = set()
    for parent in parents:
        plan, records = composite_parts(journal.session, parent)
        if plan.parent_id in parent_ids or child_ids.intersection(records):
            raise IdentityMismatch("composite proof inventory repeats a parent or child")
        parent_ids.add(plan.parent_id)
        child_ids.update(records)
        snapshots.append((plan, records))
    if not snapshots:
        return
    proof = _CompositeProofJournal(journal, snapshots)
    proof.validate()
    journal._proof_metrics["part_snapshots"] += len(snapshots)


class _CompositeProofJournal(BodyJournal):
    """Cold-recovery journal that validates all part plans in one pass."""

    def __init__(
        self,
        root: BodyJournal,
        snapshots: Sequence[tuple[PartPlan, dict[str, ItemRecord]]],
    ) -> None:
        self.root = root
        self.store = root.store
        self._active = root._active
        self._snapshots = tuple(snapshots)
        self._snapshot_hashes = {
            plan.parent_id: _snapshot_identity(plan, records) for plan, records in self._snapshots
        }
        self.session = JointPartSession(root.session, tuple(plan for plan, _records in self._snapshots))
        self._seed_targets_by_parent = {
            plan.parent_id: _seed_targets(root, plan) for plan, _records in self._snapshots
        }
        self._requests = root._requests
        self._requests_by_item = root._requests_by_item
        self.session.bind_proof_requests(self._requests, self._requests_by_item)
        self._records = {item_id: record for _plan, records in self._snapshots for item_id, record in records.items()}
        self._responses = root._responses
        self._translations = root._translations
        self._reviews = root._reviews
        self._translation_batches = root._translation_batches
        self._review_batches = root._review_batches
        self._review_records = root._review_records
        self._pending_frames = root._pending_frames
        self._proof_metrics = root._proof_metrics
        self._initial_records = None
        self._ambiguous_cache = {}
        self._run_limit = root._run_limit
        self._started = getattr(root, "_started", {})
        self._progress = getattr(root, "_progress", None)

    def validate(self) -> None:
        from engine.services.proofs import validate_records

        validate_records(self, tuple(self._records.values()))
        self.session.verify_snapshots()
        for plan, records in self._snapshots:
            if self._snapshot_hashes[plan.parent_id] != _snapshot_identity(plan, records):
                raise IdentityMismatch("part evidence changed during composite verification")
            for item_id, record in records.items():
                path = self.session.result_path(item_id)
                if state.read(path) != canonical_json_bytes(record):
                    raise IdentityMismatch("part evidence changed during composite verification")

    def _plan_for_request(self, request: RequestManifest) -> PartPlan:
        return self.session._request_owner(request)

    def _seed_translation_targets(self, request: RequestManifest) -> dict[str, str] | None:
        plan = self._plan_for_request(request)
        targets = self._seed_targets_by_parent[plan.parent_id]
        if not set(request.item_ids).issubset(targets):
            return None
        batch = self._translation_batch(request)
        if batch.manifest.item_ids != request.item_ids:
            raise IdentityMismatch("seeded part request frame changed")
        return {item_id: targets[item_id] for item_id in request.item_ids}

    def _review_target(self, item_id: str, target_hash: str, review_epoch: int) -> ItemRecord:
        try:
            return BodyJournal._review_target(self, item_id, target_hash, review_epoch)
        except IdentityMismatch:
            plan = self.session.owner(item_id)
            if plan is None:
                raise
            target = self._seed_targets_by_parent[plan.parent_id].get(item_id)
            if target is None or canonical_hash(target) != target_hash:
                raise
            from engine.services.custody import review_draft

            return review_draft(self._records[item_id], target, review_epoch)

    def _scoped_requests(self) -> tuple[RequestManifest, ...]:
        selected: dict[str, RequestManifest] = {}
        for item_id in self._records:
            for request in self._requests_by_item.get(item_id, ()):
                selected[request.request_id] = request
        return tuple(selected.values())


class PartJournal(BodyJournal):
    """Read-only opener plus explicit initializer for one persisted part plan."""

    @classmethod
    def initialize(cls, root: BodyJournal, plan: PartPlan) -> PartJournal:
        path = plan_path(root.store.root, plan.parent_id)
        if not state.exists(path) or parse_contract(state.read(path), PartPlan, "epubox-parts-1") != plan:
            raise IdentityMismatch("part initialization requires its persisted immutable plan")
        _prove_seed(root, plan)
        session = PartSession(root.session, plan)
        targets = _seed_targets(root, plan)
        batches_by_item = {item.item_id: batch for batch in session._prepared_batches.values() for item in batch.items}
        wires = {
            cast(str, value["item_id"]): cast(Mapping[str, Any], value)
            for batch in session._prepared_batches.values()
            for value in cast(list[dict[str, Any]], batch.payload["items"])
        }
        with root.store.lock(), state.batch(root.store.root):
            for batch in session._prepared_batches.values():
                request = batch.manifest
                request_path = root.store._path("requests", request.request_id)
                if state.exists(request_path):
                    saved = root.store.read_request(request.request_id)
                    if saved.model_copy(update={"attempts": ()}) != request:
                        raise StaleWrite("part initial request identity changed")
                    root._register_request(saved)
                else:
                    root._register_request(root.store.write_request(request))
            for child in plan.children:
                path = session.result_path(child.item_id)
                if state.exists(path):
                    continue
                batch = batches_by_item[child.item_id]
                wire = wires[child.item_id]
                raw_terms = wire.get("terms")
                terms = (
                    tuple(term for term in raw_terms if isinstance(term, Mapping))
                    if isinstance(raw_terms, list)
                    else ()
                )
                target = targets.get(child.item_id)
                record = ItemRecord(
                    item_id=child.item_id,
                    segment_id=child.item_id,
                    selected_term_ids=batch.manifest.term_ids_by_item[child.item_id],
                    term_applicability={str(term["term_id"]): cast(Any, term["role"]) for term in terms},
                    terms_hash=batch.manifest.terms_hashes[child.item_id],
                    context_hash=batch.manifest.context_hashes[child.item_id],
                    stage="proofread" if target is not None else "translation",
                    status=ItemStatus.LOCAL_VALID if target is not None else ItemStatus.PENDING,
                    target_projection=target,
                    target_hash=canonical_hash(target) if target is not None else None,
                    checks={"translation_frame": _frame(batch)} if target is not None else {},
                    request_id=batch.manifest.request_id if target is not None else None,
                    next_action="review" if target is not None else None,
                )
                root.store._base.atomic_write_bytes(path, canonical_json_bytes(record))
        return cls(root, plan.parent_id)

    def __init__(
        self,
        root: BodyJournal,
        parent_id: str,
        *,
        _snapshot: tuple[PartPlan, dict[str, ItemRecord]] | None = None,
        _snapshot_hash: str | None = None,
        _proof_only: bool = False,
    ):
        self.root = root
        self.store = root.store
        self._active = root._active
        self.plan, records = _snapshot if _snapshot is not None else open_parts(root.session, parent_id)
        if self.plan.parent_id != parent_id:
            raise IdentityMismatch("part snapshot belongs to another parent")
        if _snapshot_hash is not None and _snapshot_hash != _snapshot_identity(self.plan, records):
            raise IdentityMismatch("part evidence changed before child verification")
        _prove_seed(root, self.plan)
        self.session = PartSession(
            root.session,
            self.plan,
            restore_only=_proof_only,
            validated_plan=_snapshot is not None,
        )
        self._requests = root._requests
        self._requests_by_item = root._requests_by_item
        self.session.bind_proof_requests(self._requests, root._requests_by_item)
        self._records = records
        if _proof_only:
            # Cold proof is a single bounded dependency traversal. Reuse the
            # root caches so 159 historical parts do not allocate six fresh
            # cache arenas apiece, and return rebuilt legacy frames to the
            # parent for its single flush after validation.
            self._responses = root._responses
            self._translations = root._translations
            self._reviews = root._reviews
            self._translation_batches = root._translation_batches
            self._review_batches = root._review_batches
            self._review_records = root._review_records
            self._pending_frames = root._pending_frames
        else:
            self._responses = BudgetCache[tuple[str, str], Any](16 * 1024 * 1024)
            self._translations = BudgetCache[str, Mapping[str, Any]](16 * 1024 * 1024)
            self._reviews = BudgetCache[str, Mapping[str, Any]](16 * 1024 * 1024)
            self._translation_batches = BudgetCache[str, tuple[RequestManifest, MemberBatch]](16 * 1024 * 1024)
            self._review_batches = BudgetCache[str, tuple[RequestManifest, MemberBatch]](16 * 1024 * 1024)
            self._review_records = BudgetCache[str, Mapping[str, ItemRecord]](16 * 1024 * 1024)
            self._pending_frames = {}
        self._proof_metrics = root._proof_metrics
        self._initial_records: dict[str, ItemRecord] | None = None
        self._ambiguous_cache: dict[str, tuple[tuple[Any, ...], bool]] = {}
        self._run_limit = root._run_limit
        self._started = getattr(root, "_started", {})
        self._progress = getattr(root, "_progress", None)
        if not _proof_only:
            frames.warm(self)
        from engine.services.proofs import validate_records

        validate_records(self, tuple(self._records.values()))
        if _snapshot_hash is not None and _snapshot_hash != _snapshot_identity(self.plan, self._records):
            raise IdentityMismatch("part evidence changed during child verification")
        if not _proof_only:
            self._initialize_counts()

    def records(self, item_ids: Sequence[str] | None = None) -> dict[str, ItemRecord]:
        selected = tuple(item_ids) if item_ids is not None else tuple(child.item_id for child in self.plan.children)
        return super().records(selected)

    def pending_batches(self) -> tuple[MemberBatch, ...]:
        actionable = {ItemStatus.PENDING, ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE}
        selected = {item_id for item_id, record in self._records.items() if record.status in actionable}
        if self.plan.strategy != "legacy_list" or any(
            record.status == ItemStatus.REVIEWED and isinstance(record.checks.get("parts"), Mapping)
            for record in self._records.values()
        ):
            from engine.execution.atomic import _pending_batches

            return _pending_batches(
                self,
                tuple(self.session._prepared_batches.values()),
                selected,
                partitions=tuple((item_id,) for item_id in sorted(selected))
                if self.plan.strategy != "legacy_list"
                else (),
                output_unlimited=True,
            )
        return tuple(
            batch
            for batch in self.session._prepared_batches.values()
            if any(self._records[item_id].status in actionable for item_id in batch.manifest.item_ids)
        )

    def compose(self) -> ItemRecord | None:
        if any(record.status != ItemStatus.REVIEWED for record in self._records.values()):
            return None
        for record in self._records.values():
            self._require_review_proof(record)
        parent = self.root.session.index.members_by_id[self.plan.parent_id]
        merged = merge_targets(
            cast(Any, parent),
            self.plan,
            {item_id: cast(str, value.target_projection) for item_id, value in self._records.items()},
        )
        _validate_parent_target(self.root.session, parent.item_id, merged)
        current = self.root._records[self.plan.parent_id]
        summary = {
            "plan_hash": canonical_hash(self.plan),
            "child_hashes": {item_id: canonical_hash(value) for item_id, value in self._records.items()},
            "merged_target_hash": canonical_hash(merged),
        }
        composed = current.model_copy(
            update={
                "stage": "reviewed",
                "status": ItemStatus.REVIEWED,
                "target_projection": merged,
                "target_hash": canonical_hash(merged),
                "checks": _seed_provenance(self.plan) | {"parts": summary},
                "failure": None,
                "next_action": None,
            }
        )
        validate_summary(self.root.session, composed)
        if current == composed:
            return composed
        if current.status == ItemStatus.REVIEWED:
            raise StaleWrite("reviewed parent differs from its persisted part proof")
        with self.store.lock(), state.batch(self.store.root):
            self.store._base.atomic_write_bytes(
                self.root._result_path(composed.item_id), canonical_json_bytes(composed)
            )
        self.root._update_result(current, composed)
        return composed

    def _translation_target(self, record: ItemRecord) -> str:
        targets = _seed_targets(self.root, self.plan)
        if record.request_id in self.session._prepared_batches and record.item_id in targets:
            return targets[record.item_id]
        return super()._translation_target(record)

    def _seed_translation_targets(self, request: RequestManifest) -> dict[str, str] | None:
        targets = _seed_targets(self.root, self.plan)
        if not set(request.item_ids).issubset(targets):
            return None
        # Validate the persisted frame and its exact request membership. This
        # replaces planner reconstruction for completed seed-derived children.
        batch = self._translation_batch(request)
        if batch.manifest.item_ids != request.item_ids:
            raise IdentityMismatch("seeded part request frame changed")
        return {item_id: targets[item_id] for item_id in request.item_ids}

    def _review_target(self, item_id: str, target_hash: str, review_epoch: int) -> ItemRecord:
        try:
            return super()._review_target(item_id, target_hash, review_epoch)
        except IdentityMismatch:
            target = _seed_targets(self.root, self.plan).get(item_id)
            if target is None or canonical_hash(target) != target_hash:
                raise
            from engine.services.custody import review_draft

            return review_draft(self._records[item_id], target, review_epoch)

    def _unit_members(self) -> dict[str, tuple[str, ...]]:
        result: dict[str, list[str]] = {}
        for child in self.plan.children:
            result.setdefault(child.unit_id, []).append(child.item_id)
        return {unit_id: tuple(item_ids) for unit_id, item_ids in result.items()}

    def _scoped_requests(self) -> tuple[RequestManifest, ...]:
        selected: dict[str, RequestManifest] = {}
        for child in self.plan.children:
            for request in self._requests_by_item.get(child.item_id, ()):
                selected[request.request_id] = request
        return tuple(selected.values())

    def _initialize_counts(self) -> None:
        self._translated = sum(record.target_projection is not None for record in self._records.values())
        self._reviewed = sum(record.status == ItemStatus.REVIEWED for record in self._records.values())
        self._attention_units = {
            unit_id
            for unit_id, items in self._unit_members().items()
            if any(self._records[item].status == ItemStatus.NEEDS_ATTENTION for item in items)
        }
        self._accepted_units = {
            unit_id
            for unit_id, items in self._unit_members().items()
            if all(self._records[item].status == ItemStatus.REVIEWED for item in items)
        }
        for name in (
            "_actual_attempts",
            "_body_actual_attempts",
            "_run_attempts",
            "_body_attempts",
            "_input_tokens",
            "_output_tokens",
        ):
            setattr(self, name, getattr(self.root, name, 0))
        self._accounted_usage = getattr(self.root, "_accounted_usage", set())
        self._prior_input_breach = getattr(self.root, "_prior_input_breach", None)

    def _update_result(self, prior: ItemRecord, current: ItemRecord) -> None:
        self._records[current.item_id] = current
        self._translated += int(current.target_projection is not None) - int(prior.target_projection is not None)
        self._reviewed += int(current.status == ItemStatus.REVIEWED) - int(prior.status == ItemStatus.REVIEWED)
        unit = self.session.index.members_by_id[current.item_id].unit_id
        values = tuple(self._records[item] for item in self._unit_members()[unit])
        self._attention_units.discard(unit)
        self._accepted_units.discard(unit)
        if any(value.status == ItemStatus.NEEDS_ATTENTION for value in values):
            self._attention_units.add(unit)
        elif all(value.status == ItemStatus.REVIEWED for value in values):
            self._accepted_units.add(unit)

    def _sync_counters(self, *, to_root: bool) -> None:
        source, target = (self, self.root) if to_root else (self.root, self)
        for name in (
            "_actual_attempts",
            "_body_actual_attempts",
            "_run_attempts",
            "_body_attempts",
            "_input_tokens",
            "_output_tokens",
            "_prior_input_breach",
        ):
            setattr(target, name, getattr(source, name))
        target._accounted_usage = source._accounted_usage

    def _reserve(self, request_id: str, attempt) -> None:
        self._sync_counters(to_root=False)
        super()._reserve(request_id, attempt)
        self._sync_counters(to_root=True)

    def _finish(self, *args, **kwargs) -> None:
        self._sync_counters(to_root=False)
        super()._finish(*args, **kwargs)
        self._sync_counters(to_root=True)

    def _account_usage(self, request_id: str, attempt_id: str, usage) -> None:
        super()._account_usage(request_id, attempt_id, usage)

    def _persist(self, *args, **kwargs) -> None:
        self._sync_counters(to_root=False)
        super()._persist(*args, **kwargs)
        self._sync_counters(to_root=True)

    def _emit(self, event: dict[str, Any]) -> None:
        self.root._emit(event)


def _prove_seed(root: BodyJournal, plan: PartPlan) -> None:
    seed = plan.seed
    if seed is None or seed.target_projection is None:
        return
    if seed.status == ItemStatus.REVIEWED:
        root._require_review_proof(seed)
    else:
        root._translation_proof(seed)


def _seed_targets(root: BodyJournal, plan: PartPlan) -> dict[str, str]:
    seed = plan.seed
    if seed is None or seed.target_projection is None:
        return {}
    _prove_seed(root, plan)
    parent = root.session.index.members_by_id[plan.parent_id]
    return split_target(parent, plan.children, seed.target_projection, plan=plan)


def _seed_provenance(plan: PartPlan) -> dict[str, Any]:
    if plan.seed is None:
        return {}
    return {
        name: plan.seed.checks[name]
        for name in ("translation_frame", "translation_epoch", "review_epoch")
        if name in plan.seed.checks
    }


def _validate_parent_target(base, parent_id: str, target: str) -> None:
    parent = base.index.members_by_id[parent_id]
    batch = base.initial_batch(parent_id)
    if batch is None:
        raise IdentityMismatch("part parent has no committed initial batch")
    error = _target_error(parent, target, _wire_items(batch)[parent_id])
    if error is not None:
        raise IdentityMismatch(f"composed parent no longer passes local validation: {error}")


__all__ = [
    "JointPartSession",
    "PartJournal",
    "composite_parts",
    "open_parts",
    "validate_summary",
    "verify_composite",
    "verify_composites",
]
