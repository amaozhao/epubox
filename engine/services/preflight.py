"""Zero-model atomic writeback and request-budget preflight."""

from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from weakref import WeakKeyDictionary

from pydantic import Field

from engine.epub.fill import fill_resource
from engine.epub.parsing import parse_resource
from engine.epub.ranges import index_resource
from engine.item.atoms import _SUPPORTED_EXTRACTOR_VERSIONS, _safe_boundaries, extract_resource
from engine.item.atoms import EXTRACTOR_VERSION as ATOMIC_EXTRACTOR_VERSION
from engine.item.budget import measure_budget
from engine.item.inline import events_to_projection, parse_projection
from engine.schemas.base import MAX_JSON_BYTES
from engine.schemas.bridge import AtomicDocument, AtomicItem, ByteSpan, PreflightCheck
from engine.schemas.budget import BUDGET_VERSION, BudgetLimits, BudgetResult
from engine.schemas.contracts import (
    DOCUMENT_FORMAT,
    PREPARATION_FORMAT,
    DocumentPlan,
    FrozenModel,
    PreparationPlan,
    canonical_hash,
    canonical_json_bytes,
    parse_contract,
)
from engine.schemas.internal import Event
from engine.services import state
from engine.services.atomic import AtomicStore, CorruptRecord, IdentityMismatch, StaleWrite

if TYPE_CHECKING:
    from engine.services.store import RunStore

PREFLIGHT_FORMAT = "epubox-preflight-1"
PREFLIGHT_VERSION = 1
_proofs: WeakKeyDictionary[object, tuple[tuple[str, int, int, int, int, int], ...]] = WeakKeyDictionary()


class PreflightPiece(FrozenModel):
    piece_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    resource_path: str = Field(min_length=1)
    source_span: ByteSpan
    source_projection: str = Field(min_length=1)
    translate: BudgetResult
    review: BudgetResult


class PreflightDiagnostic(FrozenModel):
    item_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    resource_path: str = Field(min_length=1)
    source_span: ByteSpan
    atomic_tag: str | None
    status: Literal["passed", "split", "blocked"]
    source_tokens: int = Field(ge=0, strict=True)
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)
    context_tokens: int = Field(ge=0, strict=True)
    failures: tuple[str, ...] = ()
    piece_ids: tuple[str, ...] = ()


class PreflightReport(FrozenModel):
    format: Literal["epubox-preflight-1"] = PREFLIGHT_FORMAT
    version: Literal[1] = PREFLIGHT_VERSION
    model: str = Field(min_length=1)
    limits: dict[str, int | float | bool]
    source_hash: str = Field(min_length=1)
    resource_hashes: dict[str, str]
    map_hashes: dict[str, str]
    atoms_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    pieces: tuple[PreflightPiece, ...]
    diagnostics: tuple[PreflightDiagnostic, ...]
    check: PreflightCheck | None = None

    @property
    def passed(self) -> bool:
        return self.check is not None


class _PreflightRecord(FrozenModel):
    format: Literal["epubox-preflight-record-1"] = "epubox-preflight-record-1"
    preparation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    translation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    report: PreflightReport


def preflight_atomic_resources(
    inventories: Sequence[AtomicDocument],
    raw_by_resource: Mapping[str, bytes],
    limits: BudgetLimits,
    model: str,
) -> PreflightReport:
    """Prove byte-local replay and conservative translation/review budgets."""
    ordered = tuple(sorted(inventories, key=lambda value: value.document.document_id))
    if not ordered:
        raise ValueError("preflight requires at least one atomic inventory")
    source_hashes = {inventory.document.source_hash for inventory in ordered}
    if len(source_hashes) != 1:
        raise ValueError("atomic inventories must share one source identity")
    paths = [inventory.document.resource.path for inventory in ordered]
    document_ids = [inventory.document.document_id for inventory in ordered]
    if len(set(document_ids)) != len(document_ids):
        raise ValueError("atomic document IDs must be unique")
    if len(set(paths)) != len(paths) or set(raw_by_resource) != set(paths):
        raise ValueError("raw resources must exactly match the atomic inventory")

    pieces: list[PreflightPiece] = []
    diagnostics: list[PreflightDiagnostic] = []
    for inventory in ordered:
        path = inventory.document.resource.path
        raw = raw_by_resource[path]
        targets = {item.item_id: item.source_projection for item in inventory.items}
        fill_resource(raw, inventory, targets, identity=True)
        for item in inventory.items:
            item_pieces, diagnostic = _preflight_item(inventory, raw, item, limits, model)
            pieces.extend(item_pieces)
            diagnostics.append(diagnostic)

    map_hashes = {inventory.document.document_id: canonical_hash(inventory.source_map) for inventory in ordered}
    atoms_hash = canonical_hash(ordered)
    budget_payload = {
        "version": BUDGET_VERSION,
        "preflight_version": PREFLIGHT_VERSION,
        "model": model,
        "limits": limits.to_dict(),
        "pieces": tuple(piece.model_dump(mode="json") for piece in pieces),
    }
    budget_hash = canonical_hash(budget_payload)
    blocked = any(diagnostic.status == "blocked" for diagnostic in diagnostics)
    check = None
    if not blocked:
        check = PreflightCheck(
            source_hash=next(iter(source_hashes)),
            map_hashes=map_hashes,
            atoms_hash=atoms_hash,
            budget_hash=budget_hash,
            passed=True,
        )
    return PreflightReport(
        model=model,
        limits=limits.to_dict(),
        source_hash=next(iter(source_hashes)),
        resource_hashes={path: hashlib.sha256(raw_by_resource[path]).hexdigest() for path in sorted(paths)},
        map_hashes=map_hashes,
        atoms_hash=atoms_hash,
        budget_hash=budget_hash,
        pieces=tuple(pieces),
        diagnostics=tuple(diagnostics),
        check=check,
    )


def write_preflight(
    store: AtomicStore | RunStore,
    inventories: Sequence[AtomicDocument],
    raw_by_resource: Mapping[str, bytes],
    limits: BudgetLimits,
    model: str,
) -> PreflightReport:
    """Persist immutable inventories and their identity-bound preflight result."""
    base = _store(store)
    report = preflight_atomic_resources(inventories, raw_by_resource, limits, model)
    preparation, preparation_hash = _preparation(base.root)
    _verify_snapshot(base.root, preparation, inventories, raw_by_resource)
    record = _PreflightRecord(
        preparation_hash=preparation_hash,
        translation_hash=canonical_hash(preparation.translation_config),
        report=report,
    )
    with base.lock(), state.batch(base.root):
        for inventory in inventories:
            _write_immutable(
                base.path("inventories", inventory.document.document_id),
                inventory,
                AtomicDocument,
                "epubox-atoms-1",
            )
        _write_immutable(
            base.root / "checks" / "preflight.json",
            record,
            _PreflightRecord,
            "epubox-preflight-record-1",
        )
    return report


def prepare_preflight(
    store: AtomicStore | RunStore,
    limits: BudgetLimits,
    model: str,
) -> PreflightReport:
    """Rebuild T05 inventories from every frozen prepared resource and persist T08."""
    base = _store(store)
    preparation, _preparation_hash = _preparation(base.root)
    documents = _prepared_documents(base.root, preparation)
    try:
        with zipfile.ZipFile(state.snapshot(base.root)) as archive:
            raw = {document.resource.path: archive.read(document.resource.path) for document in documents}
    except (OSError, KeyError, zipfile.BadZipFile) as error:
        raise CorruptRecord(f"cannot read prepared resources: {error}") from error
    inventories = tuple(
        extract_resource(
            raw[document.resource.path],
            document.resource.path,
            preparation.source_hash,
            document.resource.media_type,
            extractor_version=_extractor_version(document),
        )
        for document in documents
    )
    return write_preflight(base, inventories, raw, limits, model)


def preflight_fingerprint(
    store: AtomicStore | RunStore,
) -> tuple[tuple[str, int, int, int, int, int], ...]:
    """Fingerprint only the source and immutable records covered by preflight."""
    base = _store(store)
    paths = [
        state.snapshot(base.root),
        base.root / "preparation.json",
        base.root / "checks" / "preflight.json",
        *sorted(state.glob(base.root / "documents", "*.json")),
        *sorted(state.glob(base.root / "inventories", "*.json")),
    ]
    return tuple(
        (str(path), status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)
        for path in paths
        for status in (state.stat(path),)
    )


def preflight_verified(store: AtomicStore | RunStore) -> bool:
    """Return whether this store object still has an unchanged full proof."""
    try:
        return _proofs.get(store) == preflight_fingerprint(store)
    except OSError:
        return False


def read_preflight(store: AtomicStore | RunStore, limits: BudgetLimits, model: str) -> PreflightReport:
    """Read a saved receipt and check its frozen preparation identity."""
    base = _store(store)
    _preparation, report = _saved_preflight(base, limits, model)
    return report


def load_preflight(
    store: AtomicStore | RunStore,
    limits: BudgetLimits,
    model: str,
) -> tuple[PreflightReport, tuple[AtomicDocument, ...]]:
    """Load one passed receipt without repeating extraction or token measurement."""
    before = preflight_fingerprint(store)
    base = _store(store)
    preparation, report = _saved_preflight(base, limits, model)
    if report.check is None:
        raise IdentityMismatch("preflight did not pass for the current source")
    snapshot = state.snapshot(base.root)
    if not state.is_file(snapshot) or _file_hash(snapshot) != preparation.source_hash:
        raise IdentityMismatch("preflight source snapshot differs from preparation")

    document_ids = set(preparation.document_hashes)
    disk_documents = {path.stem for path in state.glob(base.root / "documents", "*.json")}
    if disk_documents != document_ids or any(
        hashlib.sha256(state.read(base.path("documents", document_id))).hexdigest() != expected
        for document_id, expected in preparation.document_hashes.items()
    ):
        raise IdentityMismatch("prepared document files changed")
    inventory_ids = set(report.check.map_hashes)
    disk_ids = {path.stem for path in state.glob(base.root / "inventories", "*.json")}
    if inventory_ids != document_ids or disk_ids != inventory_ids:
        raise IdentityMismatch("preflight inventory does not cover every prepared document")
    inventory_raw: dict[str, bytes] = {}
    by_id: dict[str, AtomicDocument] = {}
    for document_id in inventory_ids:
        path = base.path("inventories", document_id)
        raw = state.read(path)
        try:
            inventory = parse_contract(raw, AtomicDocument, "epubox-atoms-1")
        except Exception as error:
            raise CorruptRecord(f"invalid {path}: {error}") from error
        inventory_raw[document_id] = raw
        by_id[document_id] = inventory
    if any(
        by_id[document_id].document.document_id != document_id
        or by_id[document_id].document.source_hash != preparation.source_hash
        or hashlib.sha256(canonical_json_bytes(by_id[document_id].document)).hexdigest()
        != preparation.document_hashes[document_id]
        for document_id in inventory_ids
    ):
        raise IdentityMismatch("preflight inventory documents differ from preparation")

    map_hashes = {document_id: canonical_hash(by_id[document_id].source_map) for document_id in inventory_ids}
    ordered_atoms = tuple(by_id[document_id] for document_id in sorted(inventory_ids))
    atoms_hash = _sequence_hash(tuple(inventory_raw[document_id] for document_id in sorted(inventory_ids)))
    resource_hashes = {
        inventory.document.resource.path: inventory.document.resource.source_sha256 for inventory in ordered_atoms
    }
    budget_hash = canonical_hash(
        {
            "version": BUDGET_VERSION,
            "preflight_version": PREFLIGHT_VERSION,
            "model": model,
            "limits": limits.to_dict(),
            "pieces": tuple(piece.model_dump(mode="json") for piece in report.pieces),
        }
    )
    expected = PreflightCheck(
        source_hash=preparation.source_hash,
        map_hashes=map_hashes,
        atoms_hash=atoms_hash,
        budget_hash=budget_hash,
        passed=True,
    )
    if (
        report.resource_hashes != resource_hashes
        or report.map_hashes != map_hashes
        or report.atoms_hash != atoms_hash
        or report.budget_hash != budget_hash
        or report.check != expected
    ):
        raise IdentityMismatch("preflight receipt hashes do not match its saved dependencies")
    ordered_ids = (
        *preparation.reading_order,
        *(document_id for document_id in preparation.document_hashes if document_id not in preparation.reading_order),
    )
    after = preflight_fingerprint(store)
    if after != before:
        raise IdentityMismatch("preflight dependencies changed during verification")
    _proofs[store] = after
    return report, tuple(by_id[document_id] for document_id in ordered_ids)


def _saved_preflight(
    base: AtomicStore,
    limits: BudgetLimits,
    model: str,
) -> tuple[PreparationPlan, PreflightReport]:
    record = _read(
        base.root / "checks" / "preflight.json",
        _PreflightRecord,
        "epubox-preflight-record-1",
    )
    preparation, preparation_hash = _preparation(base.root)
    report = record.report
    if record.preparation_hash != preparation_hash or record.translation_hash != canonical_hash(
        preparation.translation_config
    ):
        raise IdentityMismatch("preflight preparation identity changed")
    if report.model != model or report.limits != limits.to_dict():
        raise IdentityMismatch("preflight model or budget limits changed")
    if report.source_hash != preparation.source_hash:
        raise IdentityMismatch("preflight source identity changed")
    return preparation, report


def require_preflight(store: AtomicStore | RunStore, limits: BudgetLimits, model: str) -> PreflightReport:
    """Recompute a saved pass receipt from the frozen snapshot before dispatch."""
    base = _store(store)
    record = _read(
        base.root / "checks" / "preflight.json",
        _PreflightRecord,
        "epubox-preflight-record-1",
    )
    preparation, preparation_hash = _preparation(base.root)
    if record.preparation_hash != preparation_hash or record.translation_hash != canonical_hash(
        preparation.translation_config
    ):
        raise IdentityMismatch("preflight preparation identity changed")
    if record.report.check is None:
        raise IdentityMismatch("preflight did not pass")
    inventories = tuple(
        _read(base.path("inventories", document_id), AtomicDocument, "epubox-atoms-1")
        for document_id in sorted(record.report.check.map_hashes)
    )
    raw = _snapshot_resources(state.snapshot(base.root), inventories)
    _verify_snapshot(base.root, preparation, inventories, raw)
    actual = preflight_atomic_resources(inventories, raw, limits, model)
    if actual != record.report or actual.check is None:
        raise IdentityMismatch("preflight receipt does not match the current source, atoms, or budget")
    return actual


def _preflight_item(
    inventory: AtomicDocument,
    raw: bytes,
    item: AtomicItem,
    limits: BudgetLimits,
    model: str,
) -> tuple[tuple[PreflightPiece, ...], PreflightDiagnostic]:
    whole = _piece(inventory, item, item.item_id, item.source_projection, item.source_span, limits, model)
    whole_failures = (*whole.translate.failures, *whole.review.failures)
    selected: tuple[PreflightPiece, ...] = (whole,)
    status: Literal["passed", "split", "blocked"] = "passed"
    failures = whole_failures
    if whole_failures:
        selected = ()
        if item.atomic_tag is None:
            selected = _split_virtual(inventory, raw, item, limits, model)
        elif item.atomic_tag in {"ul", "ol"}:
            selected = _split_list(inventory, raw, item, limits, model)
        if selected and all(piece.translate.fits and piece.review.fits for piece in selected):
            status = "split"
        else:
            status = "blocked"
            failures = whole_failures or ("no safe virtual source grouping fits the request budget",)
    return selected, PreflightDiagnostic(
        item_id=item.item_id,
        document_id=item.document_id,
        resource_path=inventory.document.resource.path,
        source_span=item.source_span,
        atomic_tag=item.atomic_tag,
        status=status,
        source_tokens=whole.translate.source_tokens,
        input_tokens=max(whole.translate.input_reserve, whole.review.input_reserve),
        output_tokens=max(whole.translate.output_tokens, whole.review.output_tokens),
        context_tokens=max(whole.translate.context_tokens, whole.review.context_tokens),
        failures=tuple(dict.fromkeys(failures)),
        piece_ids=tuple(piece.piece_id for piece in selected),
    )


def _piece(
    inventory: AtomicDocument,
    item: AtomicItem,
    piece_id: str,
    source: str,
    span: ByteSpan,
    limits: BudgetLimits,
    model: str,
) -> PreflightPiece:
    base = {"request_id": "preflight-" + canonical_hash(piece_id)[:24], "context": []}
    wire = [{"item_id": piece_id, "source": source}]
    translate_payload = base | {"protocol": "epubox-text-1", "items": wire}
    review_payload = base | {"protocol": "epubox-review-2", "items": wire}
    if limits.output_version in {5, 6}:
        translate_payload["prompt_version"] = "epubox-members-1"
        review_payload["prompt_version"] = "epubox-members-1"
    return PreflightPiece(
        piece_id=piece_id,
        item_id=item.item_id,
        document_id=item.document_id,
        resource_path=inventory.document.resource.path,
        source_span=span,
        source_projection=source,
        translate=measure_budget(stage="translate", payload=translate_payload, limits=limits, tokenizer_model=model),
        review=measure_budget(
            stage="review",
            payload=review_payload,
            limits=limits,
            review_targets="estimated",
            tokenizer_model=model,
        ),
    )


def _split_virtual(
    inventory: AtomicDocument,
    raw: bytes,
    item: AtomicItem,
    limits: BudgetLimits,
    model: str,
) -> tuple[PreflightPiece, ...]:
    boundaries = item.region.get("safe_boundaries")
    if not isinstance(boundaries, list) or not boundaries:
        return ()
    parsed = parse_resource(raw, inventory.document.resource.media_type)
    index = index_resource(parsed)
    lexical = {(slot.path, slot.field, slot.attribute_name, slot.special_index): slot for slot in index.slots}
    if boundaries != _safe_boundaries(item, inventory.document, lexical):
        raise IdentityMismatch(f"safe source boundaries changed: {item.item_id}")
    events = parse_projection(item.source_projection)
    cut_points = _projection_cuts(item, inventory, events, boundaries)
    if cut_points is None:
        return ()
    fragments = _projection_fragments(events, tuple(point for point, _byte in cut_points))
    byte_cuts = (item.source_span.byte_start, *(byte for _point, byte in cut_points), item.source_span.byte_end)
    if len(fragments) + 1 != len(byte_cuts):
        return ()
    return _pack_fragments(inventory, item, fragments, byte_cuts, limits, model)


def _split_list(
    inventory: AtomicDocument,
    raw: bytes,
    item: AtomicItem,
    limits: BudgetLimits,
    model: str,
) -> tuple[PreflightPiece, ...]:
    parsed = parse_resource(raw, inventory.document.resource.media_type)
    index = index_resource(parsed)
    parent_path = inventory.document.nodes[item.node_key].element_path
    children = sorted(
        (
            (node.element_path, node.node_key)
            for node in inventory.document.nodes.values()
            if node.element_path[:-1] == parent_path
        ),
        key=lambda value: value[0],
    )
    if len(children) < 2 or any(index.nodes[path].qname.rsplit("}", 1)[-1] != "li" for path, _key in children):
        return ()
    refs = {
        entry.source_node_key: ref
        for ref, entry in item.registry.items()
        if entry.parent_ref == item.node_key and entry.hints.get("element") == "li"
    }
    if set(refs) != {key for _path, key in children}:
        return ()
    direct_refs = set(refs.values())
    events = parse_projection(item.source_projection)
    closes: dict[str, int] = {}
    groups: list[str] = []
    for position, event in enumerate(events, 1):
        if event.kind != "marker" or event.value[:1] not in {"+", "-"} or event.value[1:2] != "g":
            continue
        edge, ref = event.value[0], event.value[1:]
        if edge == "+":
            if ref in direct_refs and groups:
                return ()
            groups.append(ref)
        elif not groups or groups.pop() != ref:
            return ()
        elif not groups and ref in direct_refs:
            closes[ref] = position
    ordered_refs = tuple(refs[key] for _path, key in children)
    if groups or set(closes) != set(ordered_refs):
        return ()
    positions = (0, *(closes[ref] for ref in ordered_refs[:-1]), len(events))
    if tuple(sorted(positions)) != positions or len(set(positions)) != len(positions):
        return ()
    fragments = tuple(events_to_projection(events[start:end]) for start, end in pairwise(positions))
    byte_cuts = (
        item.source_span.byte_start,
        *(index.nodes[path].full.end for path, _key in children[:-1]),
        item.source_span.byte_end,
    )
    return _pack_fragments(inventory, item, fragments, byte_cuts, limits, model)


def validate_list_pieces(parent: AtomicItem, pieces: Sequence[PreflightPiece]) -> None:
    """Require every split list piece to contain complete direct li groups."""
    direct = {
        ref
        for ref, entry in parent.registry.items()
        if entry.parent_ref == parent.node_key and entry.hints.get("element") == "li"
    }
    seen: set[str] = set()
    for piece in pieces:
        groups: list[str] = []
        for event in parse_projection(piece.source_projection):
            if event.kind != "marker" or event.value[1:2] != "g" or event.value[:1] not in {"+", "-"}:
                continue
            edge, ref = event.value[0], event.value[1:]
            if edge == "+":
                groups.append(ref)
                if ref in direct:
                    if len(groups) != 1 or ref in seen:
                        raise ValueError("split list pieces must start at complete direct li boundaries")
                    seen.add(ref)
            elif not groups or groups.pop() != ref:
                raise ValueError("split list pieces must preserve marker nesting")
        if groups:
            raise ValueError("split list pieces must end at complete direct li boundaries")
    if not direct or seen != direct:
        raise ValueError("split list pieces must exactly cover every direct li")


def _pack_fragments(
    inventory: AtomicDocument,
    item: AtomicItem,
    fragments: Sequence[str],
    byte_cuts: Sequence[int],
    limits: BudgetLimits,
    model: str,
) -> tuple[PreflightPiece, ...]:
    if len(fragments) + 1 != len(byte_cuts):
        return ()
    result: list[PreflightPiece] = []
    start = 0
    while start < len(fragments):
        chosen: PreflightPiece | None = None
        chosen_end = start + 1
        for end in range(start + 1, len(fragments) + 1):
            byte_start, byte_end = byte_cuts[start], byte_cuts[end]
            source = "".join(fragments[start:end])
            piece_id = (
                "pc-"
                + canonical_hash(
                    {"item_id": item.item_id, "bytes": (byte_start, byte_end), "source": canonical_hash(source)}
                )[:32]
            )
            candidate = _piece(
                inventory,
                item,
                piece_id,
                source,
                ByteSpan(byte_start=byte_start, byte_end=byte_end),
                limits,
                model,
            )
            if not candidate.translate.fits or not candidate.review.fits:
                break
            chosen, chosen_end = candidate, end
        if chosen is None:
            return ()
        result.append(chosen)
        start = chosen_end
    return tuple(result)


def _projection_cuts(
    item: AtomicItem,
    inventory: AtomicDocument,
    events: Sequence[Event],
    boundaries: list,
) -> tuple[tuple[tuple[int, int], int], ...] | None:
    depth = 0
    top_text: list[tuple[int, str]] = []
    for index, event in enumerate(events):
        if event.kind == "text" and depth == 0:
            top_text.append((index, event.value))
        elif event.kind == "marker" and event.value.startswith(("+g", "+b")):
            depth += 1
        elif event.kind == "marker" and event.value.startswith(("-g", "-b")):
            depth -= 1
    if depth:
        return None
    result: list[tuple[tuple[int, int], int]] = []
    for boundary in boundaries:
        if not isinstance(boundary, dict):
            return None
        slot_id = boundary.get("slot_id")
        offset, byte_offset = boundary.get("char_offset"), boundary.get("byte_offset")
        if not isinstance(slot_id, str) or type(offset) is not int or type(byte_offset) is not int:
            return None
        slot = inventory.document.source_slots.get(slot_id)
        if slot is None:
            return None
        owned = [part for part in slot.ranges if part.owner_unit_id == item.unit_id and part.start < offset < part.end]
        if len(owned) != 1:
            return None
        part = owned[0]
        fragment = slot.source_value[part.start : part.end]
        matches: list[tuple[int, int]] = []
        for event_index, text in top_text:
            position = text.find(fragment)
            while position >= 0:
                matches.append((event_index, position + offset - part.start))
                position = text.find(fragment, position + 1)
        if len(matches) != 1:
            return None
        result.append((matches[0], byte_offset))
    ordered = tuple(sorted(result, key=lambda value: value[1]))
    if tuple(value[0] for value in ordered) != tuple(sorted(value[0] for value in ordered)):
        return None
    return ordered


def _projection_fragments(events: Sequence[Event], cuts: tuple[tuple[int, int], ...]) -> tuple[str, ...]:
    by_event: dict[int, list[int]] = {}
    for index, offset in cuts:
        by_event.setdefault(index, []).append(offset)
    pieces: list[list[Event]] = [[]]
    for index, event in enumerate(events):
        offsets = sorted(by_event.get(index, ()))
        if event.kind != "text" or not offsets:
            pieces[-1].append(event)
            continue
        start = 0
        for offset in offsets:
            if offset <= start or offset >= len(event.value):
                return ()
            pieces[-1].append(Event(kind="text", value=event.value[start:offset]))
            pieces.append([])
            start = offset
        pieces[-1].append(Event(kind="text", value=event.value[start:]))
    if any(not piece for piece in pieces):
        return ()
    return tuple(events_to_projection(piece) for piece in pieces)


def _preparation(root: Path) -> tuple[PreparationPlan, str]:
    path = root / "preparation.json"
    try:
        raw = state.read(path)
        return parse_contract(raw, PreparationPlan, PREPARATION_FORMAT), hashlib.sha256(raw).hexdigest()
    except Exception as error:
        raise CorruptRecord(f"invalid {path}: {error}") from error


def _verify_snapshot(
    root: Path,
    preparation: PreparationPlan,
    inventories: Sequence[AtomicDocument],
    raw: Mapping[str, bytes],
) -> None:
    snapshot = state.snapshot(root)
    if not state.is_file(snapshot) or hashlib.sha256(state.read(snapshot)).hexdigest() != preparation.source_hash:
        raise IdentityMismatch("preflight source snapshot differs from preparation")
    expected_documents = {document.resource.path: document for document in _prepared_documents(root, preparation)}
    expected_paths = set(expected_documents)
    by_path = {inventory.document.resource.path: inventory for inventory in inventories}
    if len(by_path) != len(inventories) or set(by_path) != expected_paths or set(raw) != expected_paths:
        raise IdentityMismatch("preflight inventory does not cover every prepared resource")
    archived = _snapshot_resources(snapshot, inventories)
    for path, inventory in by_path.items():
        if inventory.document.source_hash != preparation.source_hash:
            raise IdentityMismatch("preflight inventory source identity changed")
        if (
            raw[path] != archived[path]
            or hashlib.sha256(raw[path]).hexdigest() != inventory.document.resource.source_sha256
        ):
            raise IdentityMismatch("preflight resource bytes changed")
        document = expected_documents[path]
        canonical = extract_resource(
            raw[path],
            path,
            preparation.source_hash,
            document.resource.media_type,
            extractor_version=_extractor_version(document),
        )
        if inventory != canonical:
            raise IdentityMismatch("preflight inventory differs from canonical extraction")


def _prepared_documents(root: Path, preparation: PreparationPlan) -> tuple[DocumentPlan, ...]:
    result: list[DocumentPlan] = []
    base = AtomicStore(root)
    for document_id, expected_hash in preparation.document_hashes.items():
        path = base.path("documents", document_id)
        if not state.is_file(path) or hashlib.sha256(state.read(path)).hexdigest() != expected_hash:
            raise IdentityMismatch("prepared document file changed")
        result.append(_read(path, DocumentPlan, DOCUMENT_FORMAT))
    return tuple(result)


def _extractor_version(document: DocumentPlan) -> str:
    return (
        document.extractor_version
        if document.extractor_version in _SUPPORTED_EXTRACTOR_VERSIONS
        else ATOMIC_EXTRACTOR_VERSION
    )


def _snapshot_resources(path: Path, inventories: Sequence[AtomicDocument]) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(path) as archive:
            return {
                inventory.document.resource.path: archive.read(inventory.document.resource.path)
                for inventory in inventories
            }
    except (OSError, KeyError, zipfile.BadZipFile) as error:
        raise CorruptRecord(f"cannot read preflight resources from {path}: {error}") from error


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sequence_hash(values: Sequence[bytes]) -> str:
    digest = hashlib.sha256(b"[")
    for index, value in enumerate(values):
        if index:
            digest.update(b",")
        digest.update(value)
    digest.update(b"]")
    return digest.hexdigest()


def _write_immutable(path: Path, value, model, expected_format: str) -> None:
    if state.exists(path):
        if _read(path, model, expected_format) != value:
            raise StaleWrite(f"immutable {path.name} already exists")
        return
    # Whole-book reports are trusted local artifacts, not bounded provider responses.
    maximum = None if model is _PreflightRecord else MAX_JSON_BYTES
    AtomicStore.atomic_write_bytes(path, canonical_json_bytes(value, max_bytes=maximum))


def _read[ModelT: FrozenModel](path: Path, model: type[ModelT], expected_format: str) -> ModelT:
    try:
        maximum = None if model is _PreflightRecord else MAX_JSON_BYTES
        return parse_contract(state.read(path), model, expected_format, max_bytes=maximum)
    except Exception as error:
        raise CorruptRecord(f"invalid {path}: {error}") from error


def _store(store: AtomicStore | RunStore) -> AtomicStore:
    root = getattr(store, "root", None)
    if not isinstance(root, Path):
        root = Path(root) if isinstance(root, str) else None
    if root is None:
        raise TypeError("preflight store requires a filesystem root")
    return store if isinstance(store, AtomicStore) else AtomicStore(root, directories=("checks", "inventories"))


__all__ = [
    "PREFLIGHT_FORMAT",
    "PreflightDiagnostic",
    "PreflightPiece",
    "PreflightReport",
    "load_preflight",
    "preflight_atomic_resources",
    "preflight_fingerprint",
    "preflight_verified",
    "prepare_preflight",
    "read_preflight",
    "require_preflight",
    "write_preflight",
]
