"""Commit and verify the atomic ready marker only after all dependencies exist."""

from __future__ import annotations

import copy
import hashlib
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import unquote, urlsplit

from engine.epub.bindings import resolve_derived_navigation
from engine.item.inline import parse_projection, plain_text
from engine.item.views import validate_source_views
from engine.schemas.bridge import AtomicDocument
from engine.schemas.budget import MAX_CHUNK_TOKENS, BudgetLimits
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
from engine.services.preflight import receipt_path, require_preflight, verify_preflight_window
from engine.services.terms.recovery import require_terminal_requests as _terminal_requests
from engine.services.working import BudgetCache, RecordSequence, RecordView

if TYPE_CHECKING:
    from engine.item.members import MemberIndex
    from engine.schemas.members import RequestMember
    from engine.services.preflight import PreflightReport
    from engine.services.store import RunStore


def limits_for(preparation) -> BudgetLimits:
    return limits_from_config(preparation.translation_config)


def request_source_limit(config) -> int:
    configured = config.get("source_hard_limit")
    values = (
        MAX_CHUNK_TOKENS,
        config.get("max_source_tokens", MAX_CHUNK_TOKENS),
        MAX_CHUNK_TOKENS if configured is None else configured,
    )
    if any(type(value) is not int or value < 1 for value in values):
        raise ValueError("source limits must be positive integers")
    return min(values)


_UNSET = object()


def limits_from_config(
    config,
    *,
    context_unlimited: bool | None = None,
    output_unlimited: bool | None = None,
    source_hard_limit: int | None | object = _UNSET,
) -> BudgetLimits:
    version = _int(config, "output_budget_version", 2)
    unlimited_output = version == 7 if output_unlimited is None else output_unlimited
    if type(unlimited_output) is not bool:
        raise IdentityMismatch("output_unlimited must be a boolean")
    output = None if unlimited_output else _int(config, "max_output_tokens", 4096)
    unlimited = config.get("context_unlimited", False) if context_unlimited is None else context_unlimited
    if type(unlimited) is not bool:
        raise IdentityMismatch("context_unlimited must be a boolean")
    context = _int(config, "context_tokens", 50000 + (output or 0) + 256 if unlimited else 32768)
    input_tokens = _int(config, "max_input_tokens", 50000 if unlimited else context)
    ratio = config.get("target_ratio", 1.6)
    if isinstance(ratio, bool) or not isinstance(ratio, (float, int)):
        raise IdentityMismatch("target_ratio must be a positive number")
    hard_limit = cast(
        int | None, config.get("source_hard_limit") if source_hard_limit is _UNSET else source_hard_limit
    )
    return BudgetLimits(
        source_tokens=_int(config, "max_source_tokens", 2000),
        input_tokens=input_tokens,
        output_tokens=output,
        context_tokens=context,
        safety_tokens=_int(config, "safety_margin", 256, zero=True),
        target_ratio=float(ratio),
        output_version=cast(Literal[2, 3, 4, 5, 6, 7], 7 if unlimited_output else version),
        minimum_source_tokens=(
            _int(config, "minimum_source_tokens", 500)
            if config.get("planner_version") == "epubox-member-planner-2"
            else 0
        ),
        source_tolerance_tokens=(
            1000 if hard_limit is None and config.get("planner_version") == "epubox-member-planner-2" else 0
        ),
        source_hard_limit=hard_limit,
        context_unlimited=unlimited,
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

    index = MemberIndex(ordered, current, copy_inventories=False)
    if tuple(members) != tuple(index.items_by_id.values()):
        raise IdentityMismatch("ready members differ from the verified materialization")
    derived_ids = {member.item_id for member in members if member.unit_id in expected_derived}
    if progress:
        progress(f"准备：复核 {len(members)} 个片段的请求计划。")
    policies = {
        (batch.manifest.context_unlimited, batch.manifest.output_unlimited, batch.manifest.source_hard_limit)
        for batch in packing.batches
    }
    if len(policies) > 1:
        raise IdentityMismatch("atomic ready request plan mixes budget policies")
    context_unlimited, output_unlimited, source_hard_limit = next(iter(policies), (None, None, None))
    expected = pack_members(
        "translate",
        tuple(members),
        glossary,
        index,
        limits_from_config(
            preparation.translation_config,
            context_unlimited=context_unlimited,
            output_unlimited=output_unlimited,
            source_hard_limit=source_hard_limit,
        ),
        completed=derived_ids,
        tokenizer_model=_model(preparation),
    )
    if packing != expected:
        raise IdentityMismatch("atomic ready request plan changed")
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
            preflight_file_sha256=_sha(receipt_path(store.root)),
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
    index, batches, owners, manifests = _verify(store, ready, progress=progress)
    return ready, index, batches, owners, manifests


def read_ready(store: RunStore) -> AtomicPreparedInput:
    return _read_ready(store)[0]


def load_index(store: RunStore, prepared: AtomicPreparedInput | None = None) -> MemberIndex:
    ready, index, _batches, _owners, _manifests = _read_ready(store)
    if prepared is not None and prepared != ready:
        raise IdentityMismatch("prepared index belongs to another source task")
    preparation = store.read_preparation()
    if preparation != ready.preparation:
        raise IdentityMismatch("prepared index belongs to another source task")
    return index


def navigation_sources(inventories: Sequence[AtomicDocument]) -> dict[str, str]:
    documents = resolve_derived_navigation(tuple(inventory.document for inventory in inventories))
    return {
        str(binding["unit_id"]): str(binding["source_unit_id"])
        for document in documents
        for binding in document.derived_bindings
        if binding.get("kind") == "derived_navigation"
    }


def _collect_navigation_facts(document, titles, hrefs) -> None:
    units = {unit.unit_id: unit for unit in document.units}
    for candidate in document.derived_bindings:
        kind = candidate.get("kind")
        unit_id = candidate.get("source_unit_id")
        node_key = candidate.get("source_node_key")
        if (
            kind not in {"title_candidate", "href_candidate"}
            or not isinstance(unit_id, str)
            or unit_id not in units
            or candidate.get("source_resource") != document.resource.path
            or not isinstance(node_key, str)
            or node_key not in document.nodes
        ):
            continue
        unit = units[unit_id]
        if node_key != unit.node_key and not any(
            entry.source_node_key == node_key for entry in unit.registry.values()
        ):
            continue
        source_text = str(candidate.get("source_text", ""))
        text_events = [
            event.value
            for event in parse_projection(unit.source_projection)
            if event.kind == "text" and event.value.strip()
        ]
        if (
            not source_text
            or any(entry.kind == "x" for entry in unit.registry.values())
            or len(text_events) != 1
            or plain_text(unit.source_projection).strip() != source_text
        ):
            continue
        if kind == "title_candidate":
            key = document.resource.path, str(candidate.get("fragment", "")), source_text
            titles.setdefault(key, []).append(unit_id)
        else:
            hrefs.append((unit_id, document.resource.path, str(candidate.get("href", "")), source_text))


def _resolve_navigation(unit_owners, titles, hrefs) -> dict[str, str]:
    result: dict[str, str] = {}
    for unit_id, resource_path, href, source_text in hrefs:
        target = _local_navigation_target(resource_path, href)
        if target is None:
            continue
        matches = titles.get((*target, source_text), ())
        if len(matches) == 1 and matches[0] in unit_owners:
            result[unit_id] = matches[0]
    return result


def _local_navigation_target(base_resource: str, href: str) -> tuple[str, str] | None:
    parsed = urlsplit(href)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or (not parsed.path and not parsed.fragment)
        or parsed.path.startswith("/")
    ):
        return None
    fragment = unquote(parsed.fragment)
    if not parsed.path:
        return base_resource, fragment
    path = PurePosixPath(base_resource).parent / unquote(parsed.path)
    parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts), fragment


def _verify(store: RunStore, ready: AtomicPreparedInput, progress: Callable[[str], None] | None = None):
    from engine.item.members import MemberIndex, materialize_document_members
    from engine.schemas.members import MemberBatch, RequestMember

    if progress:
        progress("准备：验证原文快照和预检记录。")
    preparation = store.read_preparation()
    freeze, glossary = store._trusted_frozen_glossary(preparation)
    if preparation != ready.preparation or glossary != ready.glossary or freeze.freeze_id != ready.plan.freeze_id:
        raise IdentityMismatch("atomic ready source or glossary changed")
    if _sha(store.root / "glossary" / "freeze.json") != ready.plan.freeze_file_sha256:
        raise IdentityMismatch("atomic ready freeze file changed")
    if _sha(store.root / "preparation.json") != ready.plan.preparation_hash:
        raise IdentityMismatch("atomic ready preparation file changed")
    if _sha(store.root / "glossary.json") != ready.plan.glossary_file_sha256:
        raise IdentityMismatch("atomic ready glossary file changed")
    if _sha(receipt_path(store.root)) != ready.plan.preflight_file_sha256:
        raise IdentityMismatch("atomic ready preflight file changed")
    if _read(store.root / "plans" / "book.json", AtomicPlan, "epubox-plan-1") != ready.plan:
        raise IdentityMismatch("atomic ready plan changed")
    _exact_files(store.root / "inventories", set(ready.plan.inventory_hashes))
    _exact_files(store.root / "members", set(ready.plan.member_hashes))
    members_by_document: dict[str, tuple[str, ...]] = {}
    member_metadata: dict[str, tuple[str, str, str]] = {}
    text_sources: dict[str, tuple[str, tuple[str, ...] | None]] = {}
    has_text: dict[str, bool] = {}
    resource_paths: dict[str, str] = {}
    unit_ids_by_document: dict[str, tuple[str, ...]] = {}
    unit_owners: dict[str, str] = {}
    title_candidates: dict[tuple[str, str, str], list[str]] = {}
    href_candidates: list[tuple[str, str, str, str]] = []
    report_hash: str | None = None

    def verify_document(report, document_id: str, raw: bytes, inventory: AtomicDocument) -> None:
        nonlocal report_hash
        if hashlib.sha256(raw).hexdigest() != ready.plan.inventory_hashes.get(document_id):
            raise IdentityMismatch("atomic ready inventory references changed")
        validate_source_views(inventory.document)
        if report_hash is None:
            report_hash = canonical_hash(report)
        expected = materialize_document_members(inventory, report, preflight_hash=report_hash)
        parents = {item.item_id: item for item in inventory.items}
        for member in expected:
            if canonical_hash(member) != ready.plan.member_hashes.get(member.item_id):
                raise IdentityMismatch("atomic ready member plan changed")
            if (
                _read(store.root / "members" / f"{safe_id(member.item_id)}.json", RequestMember, "epubox-member-1")
                != member
            ):
                raise IdentityMismatch("atomic ready materialized member changed")
            parent = parents[member.parent_item_id]
            views = tuple(parent.source_view_ids) if member.piece_count == 1 else None
            text = (
                "\n".join(inventory.document.source_views[view_id].text for view_id in views)
                if views is not None
                else plain_text(member.source_projection)
            )
            member_metadata[member.item_id] = (member.document_id, member.channel, member.unit_id)
            text_sources[member.item_id] = (member.document_id, views)
            has_text[member.item_id] = bool(text)
        members_by_document[document_id] = tuple(member.item_id for member in expected)
        resource_paths[document_id] = inventory.document.resource.path
        unit_ids = tuple(unit.unit_id for unit in inventory.document.units)
        unit_ids_by_document[document_id] = unit_ids
        for unit_id in unit_ids:
            if unit_id in unit_owners:
                raise IdentityMismatch("atomic ready Unit ownership is not unique")
            unit_owners[unit_id] = document_id
        _collect_navigation_facts(inventory.document, title_candidates, href_candidates)

    report, ordered_ids = verify_preflight_window(store, limits_for(preparation), _model(preparation), verify_document)
    if report.check != ready.preflight:
        raise IdentityMismatch("atomic ready preflight identity changed")
    if _resolve_navigation(unit_owners, title_candidates, href_candidates) != ready.plan.derived_sources:
        raise IdentityMismatch("atomic ready navigation sources changed")
    if progress:
        progress(f"准备：预检记录已核对，恢复 {len(ready.plan.member_hashes)} 个片段的源映射。")
    member_ids = tuple(item_id for document_id in ordered_ids for item_id in members_by_document[document_id])
    inventories = _inventory_view(store, ready)
    documents = _DocumentView(inventories)

    def load_member(item_id: str) -> tuple[RequestMember, int]:
        raw = state.read(store.root / "members" / f"{safe_id(item_id)}.json")
        member = parse_contract(raw, RequestMember, "epubox-member-1")
        if member.item_id != item_id or canonical_hash(member) != ready.plan.member_hashes[item_id]:
            raise IdentityMismatch("atomic ready member changed during lazy load")
        return member, len(raw) * _MEMBER_MODEL_EXPANSION

    member_view: RecordView[RequestMember] = RecordView(member_ids, load_member, max_bytes=_MEMBER_CACHE_BYTES)

    def load_text(item_id: str) -> tuple[str, int]:
        member = member_view[item_id]
        document_id, source_views = text_sources[item_id]
        value = (
            plain_text(member.source_projection)
            if source_views is None
            else "\n".join(inventories[document_id].document.source_views[view_id].text for view_id in source_views)
        )
        return value, len(value.encode("utf-8")) * 2

    text_view: RecordView[str] = RecordView(member_ids, load_text, max_bytes=_TEXT_CACHE_BYTES)
    index = MemberIndex.from_verified_catalog(
        report,
        member_ids,
        members=member_view,
        member_sequence=RecordSequence(member_view),
        metadata=member_metadata,
        document_order=ordered_ids,
        resource_paths=resource_paths,
        inventories=inventories,
        documents=documents,
        texts=text_view,
        has_text=has_text,
    )
    index.inventories = RecordSequence(inventories)
    unit_ids = tuple(unit_id for document_id in ordered_ids for unit_id in unit_ids_by_document[document_id])
    members_by_unit: defaultdict[str, list[str]] = defaultdict(list)
    for item_id in member_ids:
        members_by_unit[member_metadata[item_id][2]].append(item_id)
    unit_members = {unit: tuple(members_by_unit[unit]) for unit in unit_ids}
    if ready.plan.unit_ids != unit_ids or ready.plan.unit_members != unit_members:
        raise IdentityMismatch("atomic ready parent membership changed")
    if progress:
        progress(f"准备：源映射已核对，加载 {len(ready.plan.batch_hashes)} 个已保存请求批次。")
    _exact_files(store.root / "batches", set(ready.plan.batch_hashes))
    covered: list[str] = []
    owners: dict[str, str] = {}
    manifests: dict[str, str] = {}
    for request_id in ready.plan.batch_hashes:
        batch = _read(store.root / "batches" / f"{safe_id(request_id)}.json", MemberBatch, "epubox-batch-2")
        if batch.manifest.request_id != request_id or canonical_hash(batch) != ready.plan.batch_hashes[request_id]:
            raise IdentityMismatch("atomic ready batch changed")
        if (
            batch.manifest.stage != "translate"
            or batch.manifest.sparse
            or batch.manifest.freeze_id != ready.plan.freeze_id
            or batch.manifest.glossary_file_sha256 != ready.plan.glossary_file_sha256
        ):
            raise IdentityMismatch("atomic ready batch ownership changed")
        manifests[request_id] = canonical_hash(batch.manifest)
        for member in batch.items:
            if member.unit_id in ready.plan.derived_sources or index.items_by_id.get(member.item_id) != member:
                raise IdentityMismatch("atomic ready batch member changed")
            covered.append(member.item_id)
            owners[member.item_id] = request_id
    expected_ids = tuple(
        item_id for item_id in member_ids if member_metadata[item_id][2] not in ready.plan.derived_sources
    )
    if len(covered) != len(set(covered)) or set(covered) != set(expected_ids):
        raise IdentityMismatch("atomic ready batches do not exactly cover dispatchable members")
    _exact_files(store.root / "results", set(ready.plan.member_hashes))
    for item_id in member_ids:
        result = cast(ItemRecord, _read(store.root / "results" / f"{safe_id(item_id)}.json", ItemRecord, None))
        if result.item_id != item_id or result.segment_id != item_id:
            raise IdentityMismatch("atomic ready result ownership changed")
    _terminal_requests(store)
    return index, _batch_view(store, ready, index), owners, manifests


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


# These budgets bound retained source snapshots and request payloads independently.
# Decoded Python models can be substantially larger than their serialized JSON.
_SOURCE_CACHE_BYTES = 64 * 1024 * 1024
_BATCH_CACHE_BYTES = 16 * 1024 * 1024
_MEMBER_CACHE_BYTES = 128 * 1024 * 1024
_TEXT_CACHE_BYTES = 128 * 1024 * 1024
_MODEL_EXPANSION = 8
_MEMBER_MODEL_EXPANSION = 2


def _inventory_view(store: RunStore, ready: AtomicPreparedInput) -> RecordView[AtomicDocument]:
    def load(document_id: str) -> tuple[AtomicDocument, int]:
        raw = state.read(store.root / "inventories" / f"{safe_id(document_id)}.json")
        if hashlib.sha256(raw).hexdigest() != ready.plan.inventory_hashes[document_id]:
            raise IdentityMismatch("atomic ready inventory changed during lazy load")
        value = parse_contract(raw, AtomicDocument, "epubox-atoms-1")
        if value.document.document_id != document_id:
            raise IdentityMismatch("atomic ready inventory ownership changed")
        return value, len(raw) * _MODEL_EXPANSION

    ordered = (
        *ready.preparation.reading_order,
        *(key for key in ready.plan.inventory_hashes if key not in ready.preparation.reading_order),
    )
    return RecordView(ordered, load, max_bytes=_SOURCE_CACHE_BYTES)


class _DocumentView(Mapping[str, object]):
    def __init__(self, inventories: Mapping[str, AtomicDocument]) -> None:
        self._inventories = inventories

    def __iter__(self):
        return iter(self._inventories)

    def __len__(self):
        return len(self._inventories)

    def __getitem__(self, key):
        return self._inventories[key].document


def _bound_inventories(store: RunStore, ready: AtomicPreparedInput, index: MemberIndex) -> None:
    inventories = _inventory_view(store, ready)
    index.inventories = RecordSequence(inventories)
    index.inventories_by_id = inventories
    index.documents = _DocumentView(inventories)


def _batch_view(store: RunStore, ready: AtomicPreparedInput, index: MemberIndex) -> RecordView[MemberBatch]:
    def load(request_id: str) -> tuple[MemberBatch, int]:
        raw = state.read(store.root / "batches" / f"{safe_id(request_id)}.json")
        batch = parse_contract(raw, MemberBatch, "epubox-batch-2")
        if batch.manifest.request_id != request_id or canonical_hash(batch) != ready.plan.batch_hashes[request_id]:
            raise IdentityMismatch("atomic ready batch changed during lazy load")
        if any(index.items_by_id.get(item.item_id) != item for item in batch.items):
            raise IdentityMismatch("atomic ready batch member changed during lazy load")
        # Keep the single verified member instance, not a second registry tree.
        batch = batch.model_copy(update={"items": tuple(index.items_by_id[item.item_id] for item in batch.items)})
        return batch, len(raw) * _MODEL_EXPANSION

    return RecordView(tuple(ready.plan.batch_hashes), load, max_bytes=_BATCH_CACHE_BYTES)


def _verify_restored_batch(session, batch, capacity, targets, sent_request) -> None:
    """Verify a sent frame without re-tokenizing its already reserved variant."""
    from engine.item.budget import measure_budget

    saved_found = False
    prior_budget = None
    last_payload: dict[str, Any] | None = None
    for payload in _payload_candidates(session, batch, targets):
        last_payload = payload
        if payload == batch.payload:
            saved_found = True
            break
        prior_budget = measure_budget(
            stage=batch.manifest.stage,
            payload=payload,
            limits=capacity,
            tokenizer_model=_model(session.prepared.preparation),
        )
        if prior_budget.fits:
            raise IdentityMismatch("workflow payload differs from its frozen source, terms or context")
    if not saved_found and last_payload is not None and capacity.output_version in {6, 7}:
        cleared = copy.deepcopy(last_payload)
        for wire in cast(list[dict[str, Any]], cleared["items"]):
            wire["constraints"] = {}
        variants = [cleared]
        if batch.manifest.stage == "review":
            compact_review = copy.deepcopy(cleared)
            compact_review["wire_version"] = "epubox-wire-8"
            variants.append(compact_review)
        compact_hints = copy.deepcopy(cleared)
        compact_hints["wire_version"] = "epubox-wire-9"
        variants.append(compact_hints)
        for payload in variants:
            if payload == batch.payload:
                saved_found = True
                break
            prior_budget = measure_budget(
                stage=batch.manifest.stage,
                payload=payload,
                limits=capacity,
                tokenizer_model=_model(session.prepared.preparation),
            )
            if prior_budget.fits:
                raise IdentityMismatch("workflow payload differs from its frozen source, terms or context")
    if not saved_found:
        raise IdentityMismatch("workflow payload differs from its frozen source, terms or context")
    measured = measure_budget(
        stage=batch.manifest.stage,
        payload=batch.payload,
        limits=capacity,
        tokenizer_model=_model(session.prepared.preparation),
    )
    if not measured.fits or measured != batch.budget:
        raise IdentityMismatch("workflow request exceeds or differs from its frozen capacity")


def _payload_candidates(session, batch, targets) -> tuple[dict[str, Any], ...]:
    from engine.item.members import build_member_payload

    capacity = limits_from_config(
        session.prepared.plan.translation_config,
        context_unlimited=batch.manifest.context_unlimited,
        output_unlimited=batch.manifest.output_unlimited,
        source_hard_limit=batch.manifest.source_hard_limit,
    )
    payloads: list[dict[str, Any]] = []
    for count, compact in ((2, False), (1, False), (0, False), (0, True)):
        if batch.manifest.sparse and count:
            continue
        if compact and len(batch.items) != 1 and not capacity.minimum_source_tokens:
            break
        payload = build_member_payload(
            batch.manifest.stage,
            batch.items,
            session.prepared.glossary,
            session.index,
            request_id=batch.manifest.request_id,
            targets=targets,
            revisions=batch.manifest.revisions if batch.manifest.stage == "review" else None,
            context_count=count,
            compact_constraints=compact,
            relaxed_adjacency=bool(capacity.minimum_source_tokens),
            sparse=batch.manifest.sparse,
            feedback=batch.manifest.feedback_by_item,
        )
        if capacity.output_version in {5, 6, 7}:
            payload["wire_version"] = "epubox-wire-5"
        if not payloads or payload != payloads[-1]:
            payloads.append(payload)
    return tuple(payloads)


class ReadySession:
    """Reuse one verified source index while immutable dependency stats agree."""

    def __init__(self, store: RunStore, progress: Callable[[str], None] | None = None):
        self.store = store
        self._signature = self._fingerprint()
        self._checkpoint = self._quick_fingerprint()
        self._saved_batches: BudgetCache[tuple[object, ...], MemberBatch] = BudgetCache(_BATCH_CACHE_BYTES)
        self._frame_requests: BudgetCache[tuple[str, int], RequestManifest] = BudgetCache(_BATCH_CACHE_BYTES)
        self.prepared, self.index, batches, self._initial_batch_ids, self._initial_manifest_hashes = _read_ready(
            store, progress
        )
        self._prepared_batches: Mapping[str, MemberBatch] = batches
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
        self._saved_batches = BudgetCache(_BATCH_CACHE_BYTES)
        self._frame_requests = BudgetCache(_BATCH_CACHE_BYTES)
        self.prepared = prepared
        self.index = index
        _bound_inventories(store, prepared, index)
        self._prepared_batches = _batch_view(store, prepared, index)
        self._initial_batch_ids = {
            item.item_id: batch.manifest.request_id for batch in batches for item in batch.items
        }
        self._initial_manifest_hashes = {
            batch.manifest.request_id: canonical_hash(batch.manifest) for batch in batches
        }
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
            # Drop references to an older reload's equivalent source strings.
            self._signature = self._checkpoint = checkpoint
            return self.prepared
        self._saved_batches.clear()
        self._frame_requests.clear()
        signature = self._fingerprint()
        if signature != self._signature:
            current, index, batches, owners, manifests = _read_ready(self.store)
            if current != self.prepared:
                raise IdentityMismatch("workflow source task changed after it became ready")
            self.index = index
            self._prepared_batches = batches
            self._initial_batch_ids = owners
            self._initial_manifest_hashes = manifests
            if signature != self._fingerprint():
                raise IdentityMismatch("ready dependencies changed during workflow verification")
            self._signature = signature
        self._checkpoint = checkpoint
        return self.prepared

    def initial_batch(self, item_id: str) -> MemberBatch | None:
        # Root indexes are collected while verifying batches. Small part/preview
        # sessions build this lookup once from their already materialized batches.
        if not hasattr(self, "_initial_batch_ids"):
            owners: dict[str, str] = {}
            for batch in self._prepared_batches.values():
                for member_id in batch.manifest.item_ids:
                    if member_id in owners:
                        raise IdentityMismatch("member has more than one canonical initial batch")
                    owners[member_id] = batch.manifest.request_id
            self._initial_batch_ids = owners
        request_id = self._initial_batch_ids.get(item_id)
        return self._prepared_batches[request_id] if request_id is not None else None

    def verifies_initial_request(self, request: RequestManifest) -> bool:
        expected = getattr(self, "_initial_manifest_hashes", {}).get(request.request_id)
        return expected is not None and canonical_hash(request.model_copy(update={"attempts": ()})) == expected

    def owns(self, member: RequestMember) -> bool:
        plan = self.prepared.plan
        return (
            self.index.source_hash == plan.source_hash
            and self.index.members_by_id.get(member.item_id) == member
            and plan.member_hashes.get(member.item_id) == canonical_hash(member)
            and member.item_id in plan.unit_members.get(member.unit_id, ())
            and plan.unit_documents.get(member.unit_id) == member.document_id
        )

    def result_path(self, item_id: str) -> Path:
        if item_id not in self.index.members_by_id:
            raise IdentityMismatch("result references an unknown ready member")
        return self.store.root / "results" / f"{safe_id(item_id)}.json"

    def verify_batch(self, batch, *, initial: bool = False, sent_request: RequestManifest | None = None) -> None:
        prepared = self.verify()
        configured = prepared.plan.translation_config.get("source_hard_limit")
        if configured is not None and (
            batch.manifest.source_hard_limit is None or batch.manifest.source_hard_limit > configured
        ):
            raise IdentityMismatch("request source hard limit exceeds the frozen plan")
        capacity = limits_from_config(
            prepared.plan.translation_config,
            context_unlimited=batch.manifest.context_unlimited,
            output_unlimited=batch.manifest.output_unlimited,
            source_hard_limit=batch.manifest.source_hard_limit,
        )
        self.index.validate_items(
            batch.items,
            relaxed_adjacency=bool(capacity.minimum_source_tokens),
            sparse=batch.manifest.sparse,
        )
        if (
            batch.manifest.freeze_id != prepared.glossary.freeze_id
            or batch.manifest.glossary_file_sha256 != prepared.plan.glossary_file_sha256
        ):
            raise IdentityMismatch("workflow batch uses another frozen glossary")
        for member in batch.items:
            if member.unit_id in prepared.plan.derived_sources:
                raise IdentityMismatch("workflow cannot dispatch a derived navigation member")
            if not self.owns(member):
                raise IdentityMismatch("workflow batch uses an unprepared member")
        if initial and prepared.plan.batch_hashes.get(batch.manifest.request_id) != canonical_hash(batch):
            raise IdentityMismatch("workflow initial batch differs from the ready plan")
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
        if batch.payload.get("prompt_version") != prepared.plan.translation_config.get("prompt_version"):
            raise IdentityMismatch("workflow prompt differs from the frozen task")
        if initial:
            # The immutable ready plan already binds this complete MemberBatch,
            # including its measured budget and canonical payload.
            return
        _verify_restored_batch(self, batch, capacity, targets, sent_request)

    def _fingerprint(self, *, include_prepared: bool = True):
        root = self.store.root
        if state.compact(root):
            return self._compact_fingerprint(include_prepared=include_prepared)
        paths = [
            state.snapshot(root),
            *(
                root / value
                for value in (
                    "preparation.json",
                    "prepared.json",
                    "glossary.json",
                    "plans/book.json",
                    str(receipt_path(root).relative_to(root)),
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
        return self._compact_fingerprint()

    def _compact_fingerprint(self, *, include_prepared: bool = True):
        root = self.store.root
        exact = [
            "preparation.json",
            "glossary.json",
            "plans/book.json",
            str(receipt_path(root).relative_to(root)),
        ]
        if include_prepared:
            exact.append("prepared.json")
        try:
            dependencies = state.dependency_fingerprint(
                root,
                exact=exact,
                prefixes=("documents/", "inventories/", "members/", "batches/", "glossary/"),
            )
            source = state.snapshot(root).stat()
            return dependencies, (
                source.st_dev,
                source.st_ino,
                source.st_size,
                source.st_mtime_ns,
                source.st_ctime_ns,
            )
        except (OSError, state.StateError) as error:
            raise IdentityMismatch(f"workflow ready dependency is unavailable: {error}") from error
