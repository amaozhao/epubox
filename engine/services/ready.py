"""Commit and verify the atomic ready marker only after all dependencies exist."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from engine.epub.bindings import resolve_derived_navigation
from engine.schemas.bridge import AtomicDocument
from engine.schemas.budget import BudgetLimits
from engine.schemas.contracts import (
    ItemRecord,
    ItemStatus,
    RequestManifest,
    canonical_hash,
    canonical_json_bytes,
    parse_contract,
)
from engine.schemas.members import MemberBatch
from engine.schemas.ready import AtomicPlan, AtomicPreparedInput
from engine.services import state
from engine.services.atomic import CorruptRecord, IdentityMismatch, StaleWrite, safe_id
from engine.services.preflight import require_preflight

if TYPE_CHECKING:
    from engine.item.members import MemberIndex
    from engine.schemas.members import RequestMember
    from engine.services.preflight import PreflightReport
    from engine.services.store import RunStore


def limits_for(preparation) -> BudgetLimits:
    return limits_from_config(preparation.translation_config)


def limits_from_config(config) -> BudgetLimits:
    context = _int(config, "context_tokens", 32768)
    ratio = config.get("target_ratio", 1.6)
    if isinstance(ratio, bool) or not isinstance(ratio, (float, int)):
        raise IdentityMismatch("target_ratio must be a positive number")
    return BudgetLimits(
        source_tokens=_int(config, "max_source_tokens", 2000),
        input_tokens=_int(config, "max_input_tokens", context),
        output_tokens=_int(config, "max_output_tokens", 4096),
        context_tokens=context,
        safety_tokens=_int(config, "safety_margin", 256, zero=True),
        target_ratio=float(ratio),
        output_version=cast(Literal[2, 3, 4, 5], _int(config, "output_budget_version", 2)),
        minimum_source_tokens=(
            _int(config, "minimum_source_tokens", 500)
            if config.get("planner_version") == "epubox-member-planner-2"
            else 0
        ),
        source_tolerance_tokens=(1000 if config.get("planner_version") == "epubox-member-planner-2" else 0),
    )


def write_ready(
    store: RunStore,
    inventories: Sequence[AtomicDocument],
    preflight: PreflightReport,
    members: Sequence[RequestMember],
    packing,
    *,
    output_policy_hash: str = "preserve-source-resources-1",
    derived_sources: Mapping[str, str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> ReadySession:
    """Write dependencies, verify them, then atomically create prepared.json last."""
    from engine.schemas.members import MemberBatch, RequestMember

    if progress:
        progress("准备：核对原文、词表与预检记录。")
    preparation, _ = store._trusted_preparation_documents()
    freeze, glossary = store._trusted_frozen_glossary(preparation)
    current = require_preflight(store, limits_for(preparation), _model(preparation))
    if current != preflight or not packing.ready:
        raise IdentityMismatch("atomic ready requires the matching passed preflight and fitting body plan")
    ordered = _inventories(store, preparation)
    if tuple(inventories) != ordered:
        raise IdentityMismatch("ready inventories differ from committed source reading order")
    expected_derived = navigation_sources(ordered)
    if dict(derived_sources or {}) != expected_derived:
        raise IdentityMismatch("derived navigation differs from the canonical source bindings")
    from engine.item.members import MemberIndex, pack_members

    index = MemberIndex(ordered, current)
    if tuple(members) != tuple(index.items_by_id.values()):
        raise IdentityMismatch("ready members differ from the verified materialization")
    derived_ids = {member.item_id for member in members if member.unit_id in expected_derived}
    if not state.compact(store.root):
        if progress:
            progress(f"准备：复核 {len(members)} 个片段的请求计划。")
        expected = pack_members(
            "translate",
            tuple(members),
            glossary,
            index,
            limits_for(preparation),
            completed=derived_ids,
            tokenizer_model=_model(preparation),
        )
        if packing != expected:
            raise IdentityMismatch("ready batches differ from the frozen complete request plan")
    _terminal_requests(store)
    if progress:
        progress(f"准备：批量保存 {len(members)} 个片段、{len(packing.batches)} 个请求批次。")
    with store.lock(), state.batch(store.root):
        for member in members:
            _immutable(
                store.root / "members" / f"{safe_id(member.item_id)}.json", member, RequestMember, "epubox-member-1"
            )
        for batch in packing.batches:
            _immutable(
                store.root / "batches" / f"{safe_id(batch.manifest.request_id)}.json",
                batch,
                MemberBatch,
                "epubox-batch-2",
            )
        unit_ids = tuple(unit.unit_id for inventory in ordered for unit in inventory.document.units)
        plan = AtomicPlan(
            source_hash=preparation.source_hash,
            run_id=preparation.run_id,
            preparation_hash=_sha(store.root / "preparation.json"),
            glossary_file_sha256=_sha(store.root / "glossary.json"),
            freeze_file_sha256=_sha(store.root / "glossary" / "freeze.json"),
            freeze_id=freeze.freeze_id,
            document_hashes=preparation.document_hashes,
            inventory_hashes={
                inventory.document.document_id: _sha(
                    store.root / "inventories" / f"{safe_id(inventory.document.document_id)}.json"
                )
                for inventory in ordered
            },
            member_hashes={member.item_id: canonical_hash(member) for member in members},
            batch_hashes={batch.manifest.request_id: canonical_hash(batch) for batch in packing.batches},
            unit_ids=unit_ids,
            unit_documents=preparation.unit_documents,
            unit_members={
                unit: tuple(member.item_id for member in members if member.unit_id == unit) for unit in unit_ids
            },
            derived_sources=expected_derived,
            required_unit_count=len(unit_ids),
            translation_config=preparation.translation_config,
            preflight_file_sha256=_sha(store.root / "checks" / "preflight.json"),
            output_policy_hash=output_policy_hash,
        )
        if current.check is None:
            raise IdentityMismatch("atomic preflight has no pass receipt")
        ready = AtomicPreparedInput(preparation=preparation, glossary=glossary, plan=plan, preflight=current.check)
        _immutable(store.root / "plans" / "book.json", plan, AtomicPlan, "epubox-plan-1")
        _initial_results(store, ready, members, packing.batches)
        if progress:
            progress("准备：验证保存后的请求与回填关系。")
        checkpoint = ReadySession.__new__(ReadySession)
        checkpoint.store = store
        signature = checkpoint._fingerprint(include_prepared=False)
        _verify(store, ready, progress=progress)
        _immutable(store.root / "prepared.json", ready, AtomicPreparedInput, "epubox-prepared-2")
        session = ReadySession._verified(store, ready, index, packing.batches, signature)
    session.verify()
    return session


def _read_ready(store: RunStore, progress: Callable[[str], None] | None = None):
    ready = _read(store.root / "prepared.json", AtomicPreparedInput, "epubox-prepared-2")
    index, batches = _verify(store, ready, progress=progress)
    return ready, index, batches


def read_ready(store: RunStore) -> AtomicPreparedInput:
    return _read_ready(store)[0]


def load_index(store: RunStore, prepared: AtomicPreparedInput | None = None) -> MemberIndex:
    from engine.item.members import MemberIndex

    ready = prepared or read_ready(store)
    preparation = store.read_preparation()
    if preparation != ready.preparation:
        raise IdentityMismatch("prepared index belongs to another source task")
    report = require_preflight(store, limits_for(preparation), _model(preparation))
    return MemberIndex(_inventories(store, preparation), report)


def navigation_sources(inventories: Sequence[AtomicDocument]) -> dict[str, str]:
    documents = resolve_derived_navigation(tuple(inventory.document for inventory in inventories))
    return {
        str(binding["unit_id"]): str(binding["source_unit_id"])
        for document in documents
        for binding in document.derived_bindings
        if binding.get("kind") == "derived_navigation"
    }


def _verify(store: RunStore, ready: AtomicPreparedInput, progress: Callable[[str], None] | None = None):
    from engine.item.members import MemberIndex, pack_members
    from engine.schemas.members import MemberBatch, RequestMember

    if progress:
        progress("准备：验证原文快照和预检记录。")
    preparation, _ = store._trusted_preparation_documents()
    freeze, glossary = store._trusted_frozen_glossary(preparation)
    if preparation != ready.preparation or glossary != ready.glossary or freeze.freeze_id != ready.plan.freeze_id:
        raise IdentityMismatch("atomic ready source or glossary changed")
    if _sha(store.root / "glossary" / "freeze.json") != ready.plan.freeze_file_sha256:
        raise IdentityMismatch("atomic ready freeze file changed")
    if _sha(store.root / "checks" / "preflight.json") != ready.plan.preflight_file_sha256:
        raise IdentityMismatch("atomic ready preflight file changed")
    report = require_preflight(store, limits_for(preparation), _model(preparation))
    if report.check != ready.preflight:
        raise IdentityMismatch("atomic ready preflight identity changed")
    if _read(store.root / "plans" / "book.json", AtomicPlan, "epubox-plan-1") != ready.plan:
        raise IdentityMismatch("atomic ready plan changed")
    _exact_files(store.root / "inventories", set(ready.plan.inventory_hashes))
    inventories = _inventories(store, preparation)
    if {
        doc.document.document_id: _sha(store.root / "inventories" / f"{safe_id(doc.document.document_id)}.json")
        for doc in inventories
    } != ready.plan.inventory_hashes:
        raise IdentityMismatch("atomic ready inventory references changed")
    if navigation_sources(inventories) != ready.plan.derived_sources:
        raise IdentityMismatch("atomic ready navigation sources changed")
    index = MemberIndex(inventories, report)
    members = tuple(index.items_by_id.values())
    unit_ids = tuple(unit.unit_id for inventory in inventories for unit in inventory.document.units)
    unit_members = {unit: tuple(member.item_id for member in members if member.unit_id == unit) for unit in unit_ids}
    if ready.plan.unit_ids != unit_ids or ready.plan.unit_members != unit_members:
        raise IdentityMismatch("atomic ready parent membership changed")
    if {member.item_id: canonical_hash(member) for member in members} != ready.plan.member_hashes:
        raise IdentityMismatch("atomic ready member plan changed")
    _exact_files(store.root / "members", set(ready.plan.member_hashes))
    for member in members:
        if (
            _read(store.root / "members" / f"{safe_id(member.item_id)}.json", RequestMember, "epubox-member-1")
            != member
        ):
            raise IdentityMismatch("atomic ready materialized member changed")
    derived = {member.item_id for member in members if member.unit_id in ready.plan.derived_sources}
    if progress:
        progress(f"准备：复核 {len(members)} 个片段的完整请求预算。")
    expected = pack_members(
        "translate",
        members,
        glossary,
        index,
        limits_for(preparation),
        completed=derived,
        tokenizer_model=_model(preparation),
    )
    if (
        not expected.ready
        or {batch.manifest.request_id: canonical_hash(batch) for batch in expected.batches} != ready.plan.batch_hashes
    ):
        raise IdentityMismatch("atomic ready request plan changed")
    if progress:
        progress("准备：核对已保存的成员、批次与初始断点。")
    _exact_files(store.root / "batches", set(ready.plan.batch_hashes))
    for batch in expected.batches:
        if (
            _read(store.root / "batches" / f"{safe_id(batch.manifest.request_id)}.json", MemberBatch, "epubox-batch-2")
            != batch
        ):
            raise IdentityMismatch("atomic ready batch changed")
    _exact_files(store.root / "results", set(ready.plan.member_hashes))
    for member in members:
        result = _read(store.root / "results" / f"{safe_id(member.item_id)}.json", ItemRecord, None)
        if result.item_id != member.item_id or result.segment_id != member.item_id:
            raise IdentityMismatch("atomic ready result ownership changed")
    _terminal_requests(store)
    return index, expected.batches


def derived_record(item_id: str, source_id: str) -> ItemRecord:
    return ItemRecord(
        item_id=item_id,
        segment_id=item_id,
        terms_hash=canonical_hash([]),
        context_hash=canonical_hash([]),
        stage="derived",
        status=ItemStatus.BLOCKED_DEPENDENCY,
        checks={"source_unit_id": source_id},
    )


def _initial_results(store, ready, members, batches) -> None:
    wires = {entry["item_id"]: (batch, entry) for batch in batches for entry in batch.payload["items"]}
    for member in members:
        path = store.root / "results" / f"{safe_id(member.item_id)}.json"
        if member.item_id in wires:
            batch, wire = wires[member.item_id]
            ids = batch.manifest.term_ids_by_item[member.item_id]
            record = ItemRecord(
                item_id=member.item_id,
                segment_id=member.item_id,
                selected_term_ids=ids,
                term_applicability={term["term_id"]: term["role"] for term in wire["terms"]},
                terms_hash=batch.manifest.terms_hashes[member.item_id],
                context_hash=batch.manifest.context_hashes[member.item_id],
            )
        else:
            record = derived_record(member.item_id, ready.plan.derived_sources[member.unit_id])
        if not state.exists(path):
            store._atomic_write(path, record)
        elif _read(path, ItemRecord, None) != record:
            raise StaleWrite("initial atomic result already contains different state")


def _inventories(store, preparation) -> tuple[AtomicDocument, ...]:
    ordered = (
        *preparation.reading_order,
        *(key for key in preparation.document_hashes if key not in preparation.reading_order),
    )
    return tuple(
        _read(store.root / "inventories" / f"{safe_id(key)}.json", AtomicDocument, "epubox-atoms-1") for key in ordered
    )


def _terminal_requests(store) -> None:
    requests = tuple(store.read_request(path.stem) for path in state.glob(store.root / "requests", "*.json"))
    succeeded = {
        (request.stage, request.owner_kind, request.owner_id, canonical_hash(request.input_hashes))
        for request in requests
        if any(attempt.state == "succeeded" for attempt in request.attempts)
    }
    for request in requests:
        if request.stage not in {"terms", "resolution"}:
            continue
        identity = (request.stage, request.owner_kind, request.owner_id, canonical_hash(request.input_hashes))
        if identity in succeeded:
            continue
        if any(
            attempt.state == "sent"
            or attempt.state == "unknown"
            and attempt.finished_at is None
            or attempt.state == "reserved"
            and any(
                value is not None for value in (attempt.sent_at, attempt.finished_at, attempt.usage, attempt.error)
            )
            for attempt in request.attempts
        ):
            raise IdentityMismatch(f"term request is not terminal: {request.request_id}")


def _exact_files(directory: Path, expected: set[str]) -> None:
    if {path.stem for path in state.glob(directory, "*.json")} != expected:
        raise IdentityMismatch(f"atomic ready {directory.name} inventory changed")


def _read(path, model, format):
    try:
        raw = state.read(path)
        return parse_contract(raw, model, format) if format else model.model_validate_json(raw)
    except Exception as error:
        raise CorruptRecord(f"invalid ready dependency {path}: {error}") from error


def _immutable(path, value, model, format) -> None:
    if state.exists(path):
        if _read(path, model, format) != value:
            raise StaleWrite(f"immutable atomic ready dependency changed: {path.name}")
        return
    from engine.services.atomic import AtomicStore

    AtomicStore.atomic_write_bytes(path, canonical_json_bytes(value))


def _sha(path: Path) -> str:
    return hashlib.sha256(state.read(path)).hexdigest()


def _model(preparation) -> str:
    value = preparation.translation_config.get("model", preparation.extraction_config.get("model"))
    if not isinstance(value, str) or not value:
        raise IdentityMismatch("atomic preparation requires a frozen model")
    return value


def _int(config, key, default, *, zero=False) -> int:
    value = config.get(key, default)
    if type(value) is not int or value < (0 if zero else 1):
        raise IdentityMismatch(f"invalid frozen capacity: {key}")
    return value


__all__ = [
    "ReadySession",
    "limits_for",
    "limits_from_config",
    "load_index",
    "navigation_sources",
    "read_ready",
    "write_ready",
]


class ReadySession:
    """Reuse one verified source index while immutable dependency stats agree."""

    def __init__(self, store: RunStore, progress: Callable[[str], None] | None = None):
        self.store = store
        self._signature = self._fingerprint()
        self._checkpoint = self._quick_fingerprint()
        self._saved_batches: dict[tuple[object, ...], MemberBatch] = {}
        self._frame_requests: dict[tuple[str, int], RequestManifest] = {}
        self.prepared, self.index, batches = _read_ready(store, progress)
        self._prepared_batches = {batch.manifest.request_id: batch for batch in batches}
        if self._signature != self._fingerprint():
            raise IdentityMismatch("ready dependencies changed during workflow verification")

    @classmethod
    def _verified(
        cls,
        store: RunStore,
        prepared: AtomicPreparedInput,
        index: MemberIndex,
        batches: Sequence[MemberBatch],
        signature: tuple,
    ) -> ReadySession:
        """Carry the just-verified in-memory plan across the preparation/translation handoff."""
        self = cls.__new__(cls)
        self.store = store
        self._saved_batches = {}
        self._frame_requests = {}
        self.prepared = prepared
        self.index = index
        self._prepared_batches = {batch.manifest.request_id: batch for batch in batches}
        if (
            self._fingerprint(include_prepared=False) != signature
            or _read(store.root / "prepared.json", AtomicPreparedInput, "epubox-prepared-2") != prepared
        ):
            raise IdentityMismatch("ready dependencies changed during preparation handoff")
        self._signature = self._fingerprint()
        self._checkpoint = self._quick_fingerprint()
        return self

    def verify(self) -> AtomicPreparedInput:
        checkpoint = self._quick_fingerprint()
        if checkpoint is not None and checkpoint == self._checkpoint:
            return self.prepared
        self._saved_batches.clear()
        self._frame_requests.clear()
        signature = self._fingerprint()
        if signature != self._signature:
            current, index, batches = _read_ready(self.store)
            if current != self.prepared:
                raise IdentityMismatch("workflow source task changed after it became ready")
            self.index = index
            self._prepared_batches = {batch.manifest.request_id: batch for batch in batches}
            if signature != self._fingerprint():
                raise IdentityMismatch("ready dependencies changed during workflow verification")
            self._signature = signature
        self._checkpoint = checkpoint
        return self.prepared

    def verify_batch(self, batch, *, initial: bool = False) -> None:
        from engine.item.budget import measure_budget

        prepared = self.verify()
        capacity = limits_for(prepared.preparation)
        self.index.validate_items(
            batch.items,
            relaxed_adjacency=bool(capacity.minimum_source_tokens),
            sparse=batch.manifest.sparse,
        )
        if (
            batch.manifest.freeze_id != prepared.glossary.freeze_id
            or batch.manifest.glossary_file_sha256 != canonical_hash(prepared.glossary)
        ):
            raise IdentityMismatch("workflow batch uses another frozen glossary")
        for member in batch.items:
            if member.unit_id in prepared.plan.derived_sources:
                raise IdentityMismatch("workflow cannot dispatch a derived navigation member")
            if prepared.plan.member_hashes.get(member.item_id) != canonical_hash(member):
                raise IdentityMismatch("workflow batch uses an unprepared member")
        if initial and prepared.plan.batch_hashes.get(batch.manifest.request_id) != canonical_hash(batch):
            raise IdentityMismatch("workflow initial batch differs from the ready plan")
        from engine.item.members import fit_member_payload

        targets = None
        if batch.manifest.stage == "review":
            targets = {}
            for wire in batch.payload["items"]:
                targets[wire["item_id"]] = ItemRecord(
                    item_id=wire["item_id"],
                    segment_id=wire["item_id"],
                    terms_hash=canonical_hash(wire.get("terms", [])),
                    context_hash=canonical_hash(batch.context),
                    target_projection=wire["target"],
                    target_hash=canonical_hash(wire["target"]),
                )
        expected_payload, _ = fit_member_payload(
            batch.manifest.stage,
            batch.items,
            prepared.glossary,
            self.index,
            capacity,
            request_id=batch.manifest.request_id,
            targets=targets,
            revisions=batch.manifest.revisions if batch.manifest.stage == "review" else None,
            tokenizer_model=_model(prepared.preparation),
            sparse=batch.manifest.sparse,
        )
        if expected_payload != batch.payload:
            raise IdentityMismatch("workflow payload differs from its frozen source, terms or context")
        measured = measure_budget(
            stage=batch.manifest.stage,
            payload=batch.payload,
            limits=capacity,
            tokenizer_model=_model(prepared.preparation),
        )
        if not measured.fits or measured != batch.budget:
            raise IdentityMismatch("workflow request exceeds or differs from its frozen capacity")
        if batch.payload.get("prompt_version") != prepared.plan.translation_config.get("prompt_version"):
            raise IdentityMismatch("workflow prompt differs from the frozen task")

    def _fingerprint(self, *, include_prepared: bool = True):
        root = self.store.root
        paths = [
            state.snapshot(root),
            *(
                root / value
                for value in (
                    "preparation.json",
                    "prepared.json",
                    "glossary.json",
                    "plans/book.json",
                    "checks/preflight.json",
                )
                if include_prepared or value != "prepared.json"
            ),
        ]
        for directory in ("documents", "inventories", "members", "batches", "glossary"):
            base = root / directory
            if not state.compact(root):
                paths.append(base)
            paths.extend(sorted({*state.glob(base, "*.json"), *state.glob(base, "**/*.json")}))
        try:
            return tuple(
                (str(path), status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)
                for path in paths
                for status in [state.stat(path)]
            )
        except OSError as error:
            raise IdentityMismatch(f"workflow ready dependency is unavailable: {error}") from error

    def _quick_fingerprint(self):
        if not state.compact(self.store.root):
            return None
        paths = (self.store.root / "state.json", state.snapshot(self.store.root))
        try:
            return tuple(
                (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)
                for path in paths
                for status in [path.stat()]
            )
        except OSError as error:
            raise IdentityMismatch(f"workflow ready dependency is unavailable: {error}") from error
