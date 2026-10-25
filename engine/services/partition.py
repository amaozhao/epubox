"""Refine one oversized legacy list member without changing the ready plan."""

from __future__ import annotations

import copy
import re
import zipfile
from collections.abc import Mapping
from itertools import pairwise
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast
from weakref import WeakKeyDictionary

from engine.epub.parsing import parse_resource
from engine.epub.ranges import ResourceIndex, index_resource
from engine.item.budget import request_source_tokens
from engine.item.inline import events_to_projection, parse_projection, plain_text
from engine.item.members import _local_registry, build_member_payload, pack_members, validate_member_target
from engine.schemas.bridge import AtomicDocument, AtomicItem, ByteSpan, batch_item_hash
from engine.schemas.contracts import ItemRecord, RequestManifest, canonical_hash, canonical_json_bytes, parse_contract
from engine.schemas.internal import Event
from engine.schemas.members import RequestMember
from engine.schemas.parts import PartPlan
from engine.services import state
from engine.services.atomic import IdentityMismatch, safe_id
from engine.services.ready import ReadySession, limits_from_config, request_source_limit
from engine.services.working import BudgetCache

_root_hashes: WeakKeyDictionary[ReadySession, str] = WeakKeyDictionary()
_RESOURCE_CACHE_BYTES = 64 * 1024 * 1024
_resource_indexes: WeakKeyDictionary[ReadySession, BudgetCache[str, tuple[ResourceIndex, bytes, str]]] = (
    WeakKeyDictionary()
)


def plan_path(root: Path, parent_id: str) -> Path:
    return root / "parts" / safe_id(parent_id) / "plan.json"


def parent_id(child_id: str) -> str | None:
    if not child_id.startswith("pc-"):
        return None
    parent, digest = child_id[3:].rsplit("-", 1) if "-" in child_id[3:] else ("", "")
    if not parent.startswith(("u-", "pc-")) or len(digest) != 32:
        return None
    if any(char not in "0123456789abcdef" for char in digest):
        return None
    return parent


def response_members(store, manifest: RequestManifest) -> dict[str, RequestMember]:
    members: dict[str, RequestMember] = {}
    for item_id in manifest.item_ids:
        member = _response_member(store, item_id, seen=set(), depth=0)
        if member.item_id != item_id:
            raise IdentityMismatch("response source member identity changed")
        members[item_id] = member
    return members


def _response_member(store, item_id: str, *, seen: set[str], depth: int) -> RequestMember:
    if item_id in seen or depth > 3:
        raise IdentityMismatch("response source part ancestry is cyclic or too deep")
    seen.add(item_id)
    member_path = store._path("members", item_id)
    if state.exists(member_path):
        return RequestMember.model_validate_json(state.read(member_path))
    owner_id = parent_id(item_id)
    if owner_id is None:
        raise IdentityMismatch("response source member is unavailable")
    path = plan_path(store.root, owner_id)
    if not state.exists(path):
        raise IdentityMismatch("response source part plan is unavailable")
    plan = parse_contract(state.read(path), PartPlan, "epubox-parts-1")
    parent = _response_member(store, owner_id, seen=seen, depth=depth + 1)
    if parent.item_id != owner_id or plan.parent_id != owner_id or plan.parent_hash != canonical_hash(parent):
        raise IdentityMismatch("response source part parent identity changed")
    member = next((child for child in plan.children if child.item_id == item_id), None)
    if member is None:
        raise IdentityMismatch("response source child is not in its part plan")
    return member


def response_sources(store, manifest: RequestManifest) -> dict[str, str]:
    return {item_id: member.source_projection for item_id, member in response_members(store, manifest).items()}


def response_hints(store, manifest: RequestManifest) -> dict[str, dict[str, dict[str, object]]]:
    return {
        item_id: {ref: dict(entry.hints) for ref, entry in member.registry.items()}
        for item_id, member in response_members(store, manifest).items()
    }


def build_plan(
    base: ReadySession,
    parent: RequestMember,
    seed: ItemRecord | None = None,
    *,
    trigger_stage: str | None = None,
    trigger_count: int | None = None,
    restart: bool = False,
) -> PartPlan | None:
    base.verify()
    if base.index.members_by_id.get(parent.item_id) != parent:
        raise IdentityMismatch("part parent is not owned by the ready session")
    _validate_seed(parent, seed)
    limit = request_source_limit(base.prepared.plan.translation_config)
    adaptive = trigger_stage is not None or trigger_count is not None
    if adaptive:
        if (
            trigger_stage not in {"translate", "review"}
            or trigger_count is None
            or trigger_count < (1 if restart else 10)
        ):
            raise ValueError("adaptive part plans require ten durable content failures")
        derived = _derive_adaptive_children(base, parent, limit)
        if derived is None:
            return None
        strategy, children, separators = derived
    else:
        if base.index.source_tokens[parent.item_id] <= limit or parent.atomic_tag not in {"ul", "ol"}:
            return None
        strategy, children, separators = "legacy_list", _derive_children(base, parent, limit), ()
    parent_plan = getattr(base, "plan", None)
    depth = parent_plan.depth + 1 if adaptive and isinstance(parent_plan, PartPlan) else 1 if adaptive else 0
    if depth > 3:
        return None
    plan = PartPlan(
        source_hash=base.prepared.plan.source_hash,
        parent_id=parent.item_id,
        parent_hash=canonical_hash(parent),
        root_plan_hash=_root_plan_hash(base),
        source_limit=limit,
        children=children,
        seed=seed,
        strategy=cast(Literal["legacy_list", "list_items", "sentences", "table_cells"], strategy),
        separators=separators,
        depth=depth,
        trigger_stage=cast(Any, trigger_stage),
        trigger_count=trigger_count,
    )
    if seed is not None and seed.target_projection is not None:
        try:
            split_target(parent, plan.children, seed.target_projection, plan=plan)
        except ValueError:
            return None
    return plan


def validate_plan(base: ReadySession, plan: PartPlan, *, measure_tokens: bool = True) -> None:
    base.verify()
    parent = base.index.members_by_id.get(plan.parent_id)
    if parent is None or canonical_hash(parent) != plan.parent_hash:
        raise IdentityMismatch("part plan parent identity changed")
    if (
        plan.source_hash != base.prepared.plan.source_hash
        or plan.root_plan_hash != _root_plan_hash(base)
        or plan.source_limit != request_source_limit(base.prepared.plan.translation_config)
    ):
        raise IdentityMismatch("part plan differs from the ready source or capacity")
    _validate_seed(parent, plan.seed)
    expected = (
        ("legacy_list", _derive_children(base, parent, plan.source_limit, measure_tokens=measure_tokens), ())
        if plan.strategy == "legacy_list"
        else _derive_adaptive_children(base, parent, plan.source_limit, measure_tokens=measure_tokens)
    )
    if expected is None or expected != (plan.strategy, plan.children, plan.separators):
        raise IdentityMismatch("part plan children differ from their deterministic source derivation")


def split_target(
    parent: RequestMember,
    children: tuple[RequestMember, ...],
    target: str,
    *,
    plan: PartPlan | None = None,
) -> dict[str, str]:
    strategy = plan.strategy if plan is not None else "legacy_list"
    _validate_children(parent, children, strategy=strategy, separators=plan.separators if plan else ())
    validate_member_target(parent, target)
    if strategy == "sentences":
        return _split_sentence_target(parent, children, target)
    if strategy == "table_cells":
        fragments = _target_cell_fragments(target, children)
        return {child.item_id: fragment for child, fragment in zip(children, fragments, strict=True)}
    refs_by_child = tuple(_direct_refs(parent, child) for child in children)
    if any(not refs for refs in refs_by_child):
        raise ValueError("part child does not contain a complete direct list item")
    ordered = tuple(ref for refs in refs_by_child for ref in refs)
    positions = _group_closes(target, set(ordered))
    if tuple(positions) != ordered:
        raise ValueError("part target direct list groups differ from the source order")
    events = parse_projection(target)
    cuts = (0, *(positions[ref] for ref in ordered[:-1]), len(events))
    fragments = tuple(events_to_projection(events[start:end]) for start, end in pairwise(cuts))
    result: dict[str, str] = {}
    offset = 0
    for child, refs in zip(children, refs_by_child, strict=True):
        value = "".join(fragments[offset : offset + len(refs)])
        validate_member_target(child, value)
        result[child.item_id] = value
        offset += len(refs)
    if offset != len(fragments) or "".join(result.values()) != target:
        raise ValueError("part targets do not exactly reconstruct their parent")
    return result


def merge_targets(
    parent: RequestMember,
    plan: PartPlan,
    targets: Mapping[str, str],
) -> str:
    """Rebuild one complete parent while preserving frozen structure between tasks."""
    if set(targets) != {child.item_id for child in plan.children}:
        raise ValueError("part targets must exactly cover the frozen children")
    if plan.strategy == "legacy_list":
        merged = "".join(targets[child.item_id] for child in plan.children)
    else:
        pieces = [plan.separators[0]]
        for index, child in enumerate(plan.children):
            target = targets[child.item_id]
            validate_member_target(child, target)
            pieces.extend((target, plan.separators[index + 1]))
        merged = "".join(pieces)
    validate_member_target(parent, merged)
    return merged


class PartSession(ReadySession):
    """A verified ReadySession view containing only one plan's derived children."""

    def __init__(
        self,
        base: ReadySession,
        plan: PartPlan,
        *,
        restore_only: bool = False,
        validated_plan: bool = False,
    ):
        if not validated_plan:
            validate_plan(base, plan, measure_tokens=not restore_only)
        saved_text = state.text(plan_path(base.store.root, plan.parent_id))
        if not validated_plan:
            saved = parse_contract(saved_text, PartPlan, "epubox-parts-1")
            if saved != plan:
                raise IdentityMismatch("saved part plan differs from the requested plan")
        elif saved_text.encode() != canonical_json_bytes(plan):
            raise IdentityMismatch("saved part plan changed after snapshot validation")
        self.base = base
        self.plan = plan
        self._plan_text = saved_text
        self._parent_hash = plan.parent_hash
        self._child_hashes = {child.item_id: canonical_hash(child) for child in plan.children}
        self.store = base.store
        self.prepared = base.prepared
        self._saved_batches = BudgetCache(16 * 1024 * 1024)
        self._frame_requests = BudgetCache(16 * 1024 * 1024)
        self.index = _scoped_index(base, plan.children, measure_tokens=not restore_only)
        self._source_tokens_verified = not restore_only
        if restore_only:
            # Completed children carry content-bound request frames. Proof
            # recovery validates those exact frames on demand; it must not run
            # the request planner again merely to rediscover the same batches.
            self._prepared_batches = {}
            self._proof_requests: Mapping[str, RequestManifest] = {}
            return
        limits = limits_from_config(
            self.prepared.plan.translation_config,
            context_unlimited=True,
            output_unlimited=True,
            source_hard_limit=plan.source_limit,
        )
        packing = pack_members(
            "translate",
            plan.children,
            self.prepared.glossary,
            self.index,
            limits,
            tokenizer_model=_model(self.prepared.preparation),
            sparse=True,
        )
        if not packing.ready or any(batch.budget.source_tokens > plan.source_limit for batch in packing.batches):
            raise IdentityMismatch("part children do not fit the frozen source capacity")
        self._prepared_batches = {batch.manifest.request_id: batch for batch in packing.batches}

    def bind_proof_requests(
        self,
        requests: Mapping[str, RequestManifest],
        requests_by_item: Mapping[str, tuple[RequestManifest, ...]] | None = None,
    ) -> None:
        """Expose persisted requests to proof-only manual validation."""
        self._proof_requests = requests
        self._proof_requests_by_item = requests_by_item
        self._proof_wire_by_item: dict[str, tuple[RequestManifest, Mapping[str, Any]]] = {}

    def initial_request_wire(
        self, item_id: str, prior: ItemRecord | None = None
    ) -> tuple[RequestManifest, Mapping[str, Any]] | None:
        """Restore one initial wire without re-running token-budget packing."""
        if not hasattr(self, "_proof_requests"):
            return None
        if item_id in self._proof_wire_by_item:
            return self._proof_wire_by_item[item_id]
        members = self.index.members_by_id
        candidates: list[tuple[RequestManifest, Mapping[str, Mapping[str, Any]]]] = []
        requests = (
            self._proof_requests_by_item.get(item_id, ())
            if self._proof_requests_by_item is not None
            else self._proof_requests.values()
        )
        for request in requests:
            if (
                request.stage != "translate"
                or item_id not in request.item_ids
                or not set(request.item_ids).issubset(members)
            ):
                continue
            if prior is not None and (
                request.term_ids_by_item.get(item_id) != prior.selected_term_ids
                or request.terms_hashes.get(item_id) != prior.terms_hash
                or request.context_hashes.get(item_id) != prior.context_hash
            ):
                continue
            request_members = tuple(members[value] for value in request.item_ids)
            for payload in _translation_payload_candidates(self, request, request_members):
                wires = {
                    str(value["item_id"]): cast(Mapping[str, Any], value)
                    for value in cast(list[dict[str, Any]], payload["items"])
                }
                context_hash = canonical_hash(payload["context"])
                if any(
                    request.context_hashes[value] != context_hash
                    or request.term_ids_by_item[value]
                    != tuple(sorted(str(term["term_id"]) for term in wires[value].get("terms", ())))
                    or request.terms_hashes[value] != canonical_hash(wires[value].get("terms", []))
                    or request.input_hashes[value]
                    != batch_item_hash(members[value], request.freeze_id or "", wires[value], context_hash)
                    for value in request.item_ids
                ):
                    continue
                candidates.append((request, wires))
        unique = {
            canonical_hash(
                {
                    "wire": wires[item_id],
                    "term_ids": request.term_ids_by_item[item_id],
                    "terms_hash": request.terms_hashes[item_id],
                    "context_hash": request.context_hashes[item_id],
                    "input_hash": request.input_hashes[item_id],
                }
            ): (request, wires)
            for request, wires in candidates
        }
        if len(unique) != 1:
            return None
        request, wires = next(iter(unique.values()))
        self._proof_wire_by_item.update(
            {value: (request, wires[value]) for value in request.item_ids if value in wires}
        )
        return self._proof_wire_by_item[item_id]

    def verify(self):
        prepared = self.base.verify()
        if state.text(plan_path(self.store.root, self.plan.parent_id)) != self._plan_text:
            raise IdentityMismatch("saved part plan changed during execution")
        parent = self.base.index.members_by_id.get(self.plan.parent_id)
        if parent is None or canonical_hash(parent) != self._parent_hash:
            raise IdentityMismatch("part plan parent identity changed during execution")
        return prepared

    def owns(self, member: RequestMember) -> bool:
        parent = self.base.index.members_by_id.get(self.plan.parent_id)
        owned = self.index.members_by_id.get(member.item_id)
        return (
            parent is not None
            and canonical_hash(parent) == self._parent_hash
            and owned == member
            and canonical_hash(member) == self._child_hashes.get(member.item_id)
        )

    def result_path(self, item_id: str) -> Path:
        if item_id not in self.index.members_by_id:
            raise IdentityMismatch("part result references an unknown child")
        return self.store.root / "parts" / safe_id(self.plan.parent_id) / "results" / f"{safe_id(item_id)}.json"


def _derive_children(
    base: ReadySession, parent: RequestMember, limit: int, *, measure_tokens: bool = True
) -> tuple[RequestMember, ...]:
    inventory = base.index.inventories_by_id.get(parent.document_id)
    if inventory is None:
        raise IdentityMismatch("part parent document is unavailable")
    index = _resource_index(base, inventory)
    parent_path = inventory.document.nodes[parent.node_key].element_path
    refs = {
        entry.source_node_key: ref
        for ref, entry in parent.registry.items()
        if entry.parent_ref == parent.node_key and entry.hints.get("element") == "li"
    }
    ordered = tuple(
        (node.element_path, refs[node.node_key])
        for node in sorted(inventory.document.nodes.values(), key=lambda value: value.element_path)
        if node.element_path[:-1] == parent_path and node.node_key in refs
    )
    if len(ordered) < 2 or any(index.nodes[path].qname.rsplit("}", 1)[-1] != "li" for path, _ref in ordered):
        raise ValueError("oversized legacy list has no complete direct li split")
    ordered_refs = tuple(ref for _path, ref in ordered)
    closes = _group_closes(parent.source_projection, set(ordered_refs))
    if tuple(closes) != ordered_refs:
        raise ValueError("legacy list projection differs from its direct li source order")
    events = parse_projection(parent.source_projection)
    projection_cuts = (0, *(closes[ref] for ref in ordered_refs[:-1]), len(events))
    byte_cuts = (
        parent.source_span.byte_start,
        *(index.nodes[path].full.end for path, _ref in ordered[:-1]),
        parent.source_span.byte_end,
    )
    if tuple(sorted(byte_cuts)) != byte_cuts or len(set(byte_cuts)) != len(byte_cuts):
        raise ValueError("legacy list byte ranges do not form ordered direct li seams")
    projections = tuple(events_to_projection(events[start:end]) for start, end in pairwise(projection_cuts))
    parent_hash = canonical_hash(parent)
    evidence = canonical_hash(
        {
            "source_hash": base.prepared.plan.source_hash,
            "root_plan_hash": _root_plan_hash(base),
            "parent_hash": parent_hash,
            "source_limit": limit,
            "pieces": tuple(
                {"bytes": span, "source": canonical_hash(source)}
                for span, source in zip(pairwise(byte_cuts), projections, strict=True)
            ),
        }
    )
    count = len(projections)
    model = _model(base.prepared.preparation)
    children = tuple(
        RequestMember(
            item_id=f"pc-{parent.item_id}-"
            + canonical_hash(
                {
                    "parent_hash": parent_hash,
                    "bytes": (byte_start, byte_end),
                    "source": canonical_hash(source),
                }
            )[:32],
            parent_item_id=parent.item_id,
            parent_hash=parent_hash,
            preflight_hash=evidence,
            unit_id=parent.unit_id,
            document_id=parent.document_id,
            kind=parent.kind,
            channel=parent.channel,
            ordinal=parent.ordinal,
            piece_index=piece_index,
            piece_count=count,
            root_node_key=parent.node_key,
            source_span=ByteSpan(byte_start=byte_start, byte_end=byte_end),
            source_projection=source,
            registry=_local_registry(cast(AtomicItem, parent), source),
            atomic_tag=parent.atomic_tag,
        )
        for piece_index, ((byte_start, byte_end), source) in enumerate(
            zip(pairwise(byte_cuts), projections, strict=True)
        )
    )
    if "".join(child.source_projection for child in children) != parent.source_projection:
        raise ValueError("part children do not reconstruct their parent source")
    if measure_tokens and any(
        request_source_tokens({"items": [{"item_id": child.item_id, "source": child.source_projection}]}, model)
        > limit
        for child in children
    ):
        raise ValueError("one direct li exceeds the frozen source capacity")
    return children


def _derive_adaptive_children(
    base: ReadySession, parent: RequestMember, limit: int, *, measure_tokens: bool = True
) -> tuple[str, tuple[RequestMember, ...], tuple[str, ...]] | None:
    inventory = base.index.inventories_by_id.get(parent.document_id)
    if inventory is None:
        raise IdentityMismatch("part parent document is unavailable")
    if parent.atomic_tag in {"ul", "ol"}:
        children = _derive_children(base, parent, limit, measure_tokens=measure_tokens)
        return "list_items", children, ("",) * (len(children) + 1)
    if parent.atomic_tag == "table" and (parent.piece_count == 1 or parent_id(parent.item_id) is not None):
        fragments = _table_cell_fragments(base, inventory, parent)
        if fragments is not None and len(fragments[0]) >= 2:
            strategy = "table_cells"
        else:
            fragments = _sentence_source_fragments(base, inventory, parent)
            strategy = "sentences"
    elif parent.atomic_tag in {"p", "em", "i"}:
        fragments = _sentence_source_fragments(base, inventory, parent)
        strategy = "sentences"
    else:
        return None
    if fragments is None or len(fragments[0]) < 2:
        return None
    sources, spans, separators = fragments
    children = _adaptive_members(
        base, parent, limit, strategy, sources, spans, separators, measure_tokens=measure_tokens
    )
    return strategy, children, separators


def _adaptive_members(
    base: ReadySession,
    parent: RequestMember,
    limit: int,
    strategy: str,
    sources: tuple[str, ...],
    spans: tuple[ByteSpan, ...],
    separators: tuple[str, ...],
    *,
    measure_tokens: bool = True,
) -> tuple[RequestMember, ...]:
    parent_hash = canonical_hash(parent)
    evidence = canonical_hash(
        {
            "source_hash": base.prepared.plan.source_hash,
            "root_plan_hash": _root_plan_hash(base),
            "parent_hash": parent_hash,
            "source_limit": limit,
            "strategy": strategy,
            "sources": tuple(canonical_hash(value) for value in sources),
            "spans": tuple((span.byte_start, span.byte_end) for span in spans),
            "separators": separators,
        }
    )
    count = len(sources)
    children = tuple(
        RequestMember(
            item_id=f"pc-{parent.item_id}-"
            + canonical_hash(
                {
                    "parent_hash": parent_hash,
                    "bytes": (span.byte_start, span.byte_end),
                    "source": canonical_hash(source),
                    "strategy": strategy,
                }
            )[:32],
            parent_item_id=parent.item_id,
            parent_hash=parent_hash,
            preflight_hash=evidence,
            unit_id=parent.unit_id,
            document_id=parent.document_id,
            kind=parent.kind,
            channel=parent.channel,
            ordinal=parent.ordinal,
            piece_index=index,
            piece_count=count,
            root_node_key=parent.node_key,
            source_span=span,
            source_projection=source,
            registry=_local_registry(cast(AtomicItem, parent), source),
            atomic_tag=parent.atomic_tag,
        )
        for index, (source, span) in enumerate(zip(sources, spans, strict=True))
    )
    model = _model(base.prepared.preparation)
    if measure_tokens and any(
        request_source_tokens({"items": [{"item_id": child.item_id, "source": child.source_projection}]}, model)
        > limit
        for child in children
    ):
        raise ValueError("one adaptive child exceeds the frozen source capacity")
    _validate_children(parent, children, strategy=strategy, separators=separators)
    return children


def _sentence_source_fragments(base, inventory: AtomicDocument, parent: RequestMember):
    events = parse_projection(parent.source_projection)
    unit = next((value for value in inventory.document.units if value.unit_id == parent.unit_id), None)
    if unit is None:
        return None
    locations = tuple(
        sorted(
            (
                location
                for location in inventory.source_map.locations
                if location.source_ref.slot_id in unit.slot_ids
                and parent.source_span.byte_start <= location.byte_span.byte_start
                and location.byte_span.byte_end <= parent.source_span.byte_end
                and any(
                    value.owner_unit_id == parent.unit_id
                    and value.start == location.source_ref.start
                    and value.end == location.source_ref.end
                    for value in inventory.document.source_slots[location.source_ref.slot_id].ranges
                )
            ),
            key=lambda value: value.byte_span.byte_start,
        )
    )
    texts = tuple(
        inventory.document.source_slots[location.source_ref.slot_id].source_value[
            location.source_ref.start : location.source_ref.end
        ]
        for location in locations
    )
    if not texts or "".join(texts) != plain_text(events):
        return None
    index = _resource_index(base, inventory)
    special = {
        value["slot_id"]: value["child_index"]
        for value in inventory.document.boundaries
        if value.get("kind") == "non_element_tail"
        and isinstance(value.get("slot_id"), str)
        and type(value.get("child_index")) is int
    }
    chunks: list[tuple[str, ByteSpan, int, int]] = []
    plain_offset = 0
    for location, text in zip(locations, texts, strict=True):
        slot = inventory.document.source_slots[location.source_ref.slot_id]
        path = inventory.document.nodes[slot.node_key].element_path
        lexical = next(
            (
                value
                for value in index.slots
                if value.path == path
                and value.field == slot.field
                and value.attribute_name == slot.attribute_name
                and value.special_index == special.get(slot.slot_id)
            ),
            None,
        )
        if lexical is None:
            return None
        projections = _sentence_fragments(events_to_projection((Event(kind="text", value=text),)))
        if not projections:
            projections = (events_to_projection((Event(kind="text", value=text),)),)
        local = 0
        for projection in projections:
            decoded = plain_text(projection)
            if not decoded.strip():
                return None
            length = len(decoded)
            try:
                span = lexical.source_span(
                    location.source_ref.start + local,
                    location.source_ref.start + local + length,
                )
            except ValueError:
                return None
            chunks.append((projection, span, plain_offset + local, plain_offset + local + length))
            local += length
        if local != len(text):
            return None
        plain_offset += len(text)
    if len(chunks) < 2:
        return None
    event_ranges: list[tuple[int, int, int, str]] = []
    serialized_offset = 0
    decoded_offset = 0
    for event in events:
        encoded = events_to_projection((event,))
        if event.kind == "text":
            event_ranges.append((decoded_offset, decoded_offset + len(event.value), serialized_offset, event.value))
            decoded_offset += len(event.value)
        serialized_offset += len(encoded)
    serialized_ranges: list[tuple[int, int]] = []
    for projection, _span, start, end in chunks:
        owner = next((value for value in event_ranges if value[0] <= start < end <= value[1]), None)
        if owner is None:
            return None
        decoded_start, _decoded_end, encoded_start, event_text = owner
        prefix = events_to_projection((Event(kind="text", value=event_text[: start - decoded_start]),))
        serialized_ranges.append((encoded_start + len(prefix), encoded_start + len(prefix) + len(projection)))
    separators: list[str] = []
    cursor = 0
    for start, end in serialized_ranges:
        separators.append(parent.source_projection[cursor:start])
        cursor = end
    separators.append(parent.source_projection[cursor:])
    sources = tuple(value[0] for value in chunks)
    if (
        "".join(part for pair in zip(separators, (*sources, ""), strict=True) for part in pair)
        != parent.source_projection
    ):
        return None
    return sources, tuple(value[1] for value in chunks), tuple(separators)


def _sentence_fragments(projection: str) -> tuple[str, ...]:
    events = parse_projection(projection)
    if len(events) != 1 or events[0].kind != "text":
        return ()
    text = events[0].value
    cuts = [
        match.end()
        for match in re.finditer(r"(?<=[.!?。！？])\s+|(?<=[。！？])(?=\S)", text)
        if match.end() < len(text)
    ]
    if not cuts:
        return ()
    bounds = (0, *cuts, len(text))
    values = tuple(
        events_to_projection((Event(kind="text", value=text[start:end]),)) for start, end in pairwise(bounds)
    )
    return values if all(plain_text(value).strip() for value in values) else ()


def _split_sentence_target(parent: RequestMember, children: tuple[RequestMember, ...], target: str) -> dict[str, str]:
    source_events = parse_projection(parent.source_projection)
    target_events = parse_projection(target)
    source_texts = tuple(event.value for event in source_events if event.kind == "text")
    target_texts = tuple(event.value for event in target_events if event.kind == "text")
    if len(source_texts) != len(target_texts):
        raise ValueError("translated text domains differ from the frozen sentence split")
    child_index = 0
    values: list[str] = []
    for source_text, target_text in zip(source_texts, target_texts, strict=True):
        owned: list[RequestMember] = []
        combined = ""
        while child_index < len(children) and len(combined) < len(source_text):
            child = children[child_index]
            combined += plain_text(child.source_projection)
            owned.append(child)
            child_index += 1
        if combined != source_text or not owned:
            raise ValueError("sentence children differ from their frozen text domains")
        target_projection = events_to_projection((Event(kind="text", value=target_text),))
        fragments = (target_projection,) if len(owned) == 1 else _sentence_fragments(target_projection)
        if len(fragments) != len(owned):
            raise ValueError("translated sentence count differs from the frozen source split")
        values.extend(fragments)
    if child_index != len(children):
        raise ValueError("sentence target does not cover every frozen child")
    result = {child.item_id: value for child, value in zip(children, values, strict=True)}
    for child, value in zip(children, values, strict=True):
        validate_member_target(child, value)
    return result


def _table_cell_fragments(base, inventory: AtomicDocument, parent: RequestMember):
    events = parse_projection(parent.source_projection)
    cells = {
        ref
        for ref, entry in parent.registry.items()
        if entry.kind == "g" and entry.hints.get("element") in {"td", "th"}
    }
    intervals = _group_intervals(events, cells)
    if len(intervals) < 2:
        return None
    index = _resource_index(base, inventory)
    sources = tuple(events_to_projection(events[start:end]) for _ref, start, end in intervals)
    spans = tuple(
        ByteSpan(
            byte_start=index.nodes[
                inventory.document.nodes[parent.registry[ref].source_node_key].element_path
            ].full.start,
            byte_end=index.nodes[inventory.document.nodes[parent.registry[ref].source_node_key].element_path].full.end,
        )
        for ref, _start, _end in intervals
    )
    separators: list[str] = []
    cursor = 0
    for _ref, start, end in intervals:
        separators.append(events_to_projection(events[cursor:start]))
        cursor = end
    separators.append(events_to_projection(events[cursor:]))
    if (
        "".join(part for pair in zip(separators, (*sources, ""), strict=True) for part in pair)
        != parent.source_projection
    ):
        # The projection codec is canonical; this catches overlapping/nested cells.
        return None
    return sources, spans, tuple(separators)


def _group_intervals(events: tuple[Event, ...], selected: set[str]) -> tuple[tuple[str, int, int], ...]:
    stack: list[tuple[str, int]] = []
    result: list[tuple[str, int, int]] = []
    for position, event in enumerate(events):
        if event.kind != "marker" or event.value[:1] not in {"+", "-"} or event.value[1:2] != "g":
            continue
        ref = event.value[1:]
        if event.value.startswith("+"):
            stack.append((ref, position))
            continue
        if not stack or stack[-1][0] != ref:
            raise ValueError("projection marker nesting changed")
        _opened, start = stack.pop()
        if ref in selected:
            result.append((ref, start, position + 1))
    if stack:
        raise ValueError("projection contains unclosed groups")
    result.sort(key=lambda value: value[1])
    if any(left[2] > right[1] for left, right in pairwise(result)):
        raise ValueError("adaptive groups overlap")
    return tuple(result)


def _target_cell_fragments(target: str, children: tuple[RequestMember, ...]) -> tuple[str, ...]:
    events = parse_projection(target)
    refs: list[str] = []
    for child in children:
        first = next((event for event in parse_projection(child.source_projection) if event.kind == "marker"), None)
        if first is None or not first.value.startswith("+g"):
            raise ValueError("table cell child lacks its opening marker")
        refs.append(first.value[1:])
    intervals = _group_intervals(events, set(refs))
    by_ref = {ref: (start, end) for ref, start, end in intervals}
    if set(by_ref) != set(refs):
        raise ValueError("translated table cells differ from the frozen source split")
    result = tuple(events_to_projection(events[by_ref[ref][0] : by_ref[ref][1]]) for ref in refs)
    for child, value in zip(children, result, strict=True):
        validate_member_target(child, value)
    return result


def _group_closes(projection: str, direct: set[str]) -> dict[str, int]:
    groups: list[str] = []
    closes: dict[str, int] = {}
    for position, event in enumerate(parse_projection(projection), 1):
        if event.kind != "marker" or event.value[:1] not in {"+", "-"} or event.value[1:2] != "g":
            continue
        edge, ref = event.value[0], event.value[1:]
        if edge == "+":
            if ref in direct and groups:
                raise ValueError("direct li marker begins inside another group")
            groups.append(ref)
        elif not groups or groups.pop() != ref:
            raise ValueError("list projection marker nesting changed")
        elif not groups and ref in direct:
            if ref in closes:
                raise ValueError("list projection repeats a direct li group")
            closes[ref] = position
    if groups or set(closes) != direct:
        raise ValueError("list projection does not exactly cover its direct li groups")
    return closes


def _direct_refs(parent: RequestMember, child: RequestMember) -> tuple[str, ...]:
    direct = {
        ref
        for ref, entry in parent.registry.items()
        if ref in child.registry and entry.parent_ref == parent.node_key and entry.hints.get("element") == "li"
    }
    return tuple(_group_closes(child.source_projection, direct))


def _validate_seed(parent: RequestMember, seed: ItemRecord | None) -> None:
    if seed is None:
        return
    if seed.item_id != parent.item_id or seed.segment_id != parent.item_id:
        raise ValueError("part seed must belong to its legacy parent")
    if seed.target_projection is not None:
        if seed.target_hash != canonical_hash(seed.target_projection):
            raise ValueError("part seed target hash changed")
        validate_member_target(parent, seed.target_projection)


def _validate_children(
    parent: RequestMember,
    children: tuple[RequestMember, ...],
    *,
    strategy: str = "legacy_list",
    separators: tuple[str, ...] = (),
) -> None:
    parent_hash = canonical_hash(parent)
    common_invalid = (
        len(children) < 2
        or tuple(child.piece_index for child in children) != tuple(range(len(children)))
        or any(
            child.parent_item_id != parent.item_id
            or child.parent_hash != parent_hash
            or child.piece_count != len(children)
            or child.preflight_hash != children[0].preflight_hash
            for child in children
        )
    )
    if common_invalid:
        raise ValueError("part children do not exactly cover their legacy parent")
    assert len(children) >= 2
    if strategy in {"legacy_list", "list_items"}:
        invalid = (
            children[0].source_span.byte_start != parent.source_span.byte_start
            or children[-1].source_span.byte_end != parent.source_span.byte_end
            or any(left.source_span.byte_end != right.source_span.byte_start for left, right in pairwise(children))
            or "".join(child.source_projection for child in children) != parent.source_projection
        )
    else:
        invalid = (
            len(separators) != len(children) + 1
            or "".join(
                part
                for pair in zip(separators, (*tuple(child.source_projection for child in children), ""), strict=True)
                for part in pair
            )
            != parent.source_projection
        )
    if invalid:
        raise ValueError("part children do not exactly reconstruct their parent")


def _scoped_index(base: ReadySession, children: tuple[RequestMember, ...], *, measure_tokens: bool = True):
    index = copy.copy(base.index)
    members = MappingProxyType({child.item_id: child for child in children})
    index.members = children
    index.members_by_id = members
    index.items_by_id = members
    index._lane_positions = {child.item_id: position for position, child in enumerate(children)}
    index._texts = MappingProxyType({child.item_id: plain_text(child.source_projection) for child in children})
    index._previous_ids = {child.item_id: () for child in children}
    model = _model(base.prepared.preparation)
    index.source_tokens = MappingProxyType(
        {
            child.item_id: (
                request_source_tokens(
                    {"items": [{"item_id": child.item_id, "source": child.source_projection}]}, model
                )
                if measure_tokens
                else 0
            )
            for child in children
        }
    )
    body: dict[str, int] = {}
    for child in children:
        if child.channel == "body":
            body[child.document_id] = body.get(child.document_id, 0) + index.source_tokens[child.item_id]
    index.body_tokens = MappingProxyType(body)
    return index


def _model(preparation) -> str:
    value = preparation.translation_config.get("model", preparation.extraction_config.get("model"))
    if not isinstance(value, str) or not value:
        raise IdentityMismatch("atomic preparation requires a frozen model")
    return value


def _root_plan_hash(base: ReadySession) -> str:
    value = _root_hashes.get(base)
    if value is None:
        value = canonical_hash(base.prepared.plan)
        _root_hashes[base] = value
    return value


def _translation_payload_candidates(
    session: PartSession,
    request: RequestManifest,
    members: tuple[RequestMember, ...],
) -> tuple[dict[str, Any], ...]:
    payloads: list[dict[str, Any]] = []
    for context_count in (2, 1, 0):
        if request.sparse and context_count:
            continue
        for compact in (False, True) if context_count == 0 else (False,):
            payload = build_member_payload(
                "translate",
                members,
                session.prepared.glossary,
                session.index,
                request_id=request.request_id,
                context_count=context_count,
                compact_constraints=compact,
                sparse=request.sparse,
                feedback=request.feedback_by_item,
            )
            payloads.append(payload)
            cleared = copy.deepcopy(payload)
            for wire in cast(list[dict[str, Any]], cleared["items"]):
                wire["constraints"] = {}
            payloads.append(cleared)
    return tuple(payloads)


def _resource_index(base: ReadySession, inventory: AtomicDocument) -> ResourceIndex:
    owner = base
    while isinstance(owner, PartSession):
        owner = owner.base
    cached = _resource_indexes.get(owner)
    if cached is None:
        cached = BudgetCache(_RESOURCE_CACHE_BYTES)
        _resource_indexes[owner] = cached
    document_id = inventory.document.document_id
    try:
        return cached[document_id][0]
    except KeyError:
        pass
    try:
        with zipfile.ZipFile(state.snapshot(base.store.root)) as archive:
            raw = archive.read(inventory.document.resource.path)
    except (OSError, KeyError, zipfile.BadZipFile) as error:
        raise IdentityMismatch(f"part source resource is unavailable: {error}") from error
    value = index_resource(parse_resource(raw, inventory.document.resource.media_type))
    # Include the two dominant buffers in byte accounting. The ResourceIndex
    # itself remains in the tuple so eviction releases its nodes and slots too.
    cached[document_id] = (value, value.raw, value.text)
    return value


__all__ = [
    "PartSession",
    "build_plan",
    "merge_targets",
    "parent_id",
    "plan_path",
    "response_hints",
    "response_members",
    "response_sources",
    "split_target",
    "validate_plan",
]
