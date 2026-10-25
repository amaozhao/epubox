"""Split oversized table requests at complete outer rows and preserve parent markup."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engine.epub.parsing import parse_resource
from engine.epub.ranges import index_resource
from engine.item.inline import events_to_projection, parse_projection, validate_projection
from engine.schemas.bridge import AtomicDocument, AtomicItem, ByteSpan
from engine.schemas.budget import BudgetLimits
from engine.schemas.contracts import canonical_hash
from engine.schemas.internal import Event
from engine.services import state
from engine.services.atomic import IdentityMismatch

if TYPE_CHECKING:
    from engine.services.preflight import PreflightPiece

_SECTIONS = frozenset({"thead", "tbody", "tfoot"})


def row_receipt(root: Path) -> Path:
    return root / "checks" / "rows.json"


def upgrade_receipt(base: Any, target: Path, record: Any) -> Path:
    """Keep the old failed receipt and add a source-bound table-row proof before HTTP."""
    from engine.services.preflight import _PreflightRecord, _read

    if state.is_file(rows := row_receipt(base.root)):
        return rows  # The immutable write below still rejects any changed proof.
    if not state.is_file(target):
        return target
    previous = _read(target, _PreflightRecord, "epubox-preflight-record-1")
    allowed = {
        item.item_id for item in previous.report.diagnostics if item.atomic_tag == "table" and item.status == "blocked"
    }
    if previous.report.check is not None or not allowed or previous == record:
        return target
    if (
        previous.preparation_hash != record.preparation_hash
        or previous.translation_hash != record.translation_hash
        or any(
            getattr(previous.report, key) != getattr(record.report, key)
            for key in ("source_hash", "model", "limits", "map_hashes", "resource_hashes", "atoms_hash")
        )
        or tuple(item for item in previous.report.diagnostics if item.item_id not in allowed)
        != tuple(item for item in record.report.diagnostics if item.item_id not in allowed)
        or tuple(piece for piece in previous.report.pieces if piece.item_id not in allowed)
        != tuple(piece for piece in record.report.pieces if piece.item_id not in allowed)
        or state.is_file(base.root / "prepared.json")
        or state.is_file(base.root / "bookplan.json")
        or list(state.glob(base.root / "requests", "*.json"))
        or list(state.glob(base.root / "members", "*.json"))
    ):
        raise IdentityMismatch("table-row preflight upgrade requires unchanged inputs and no dispatched work")
    return row_receipt(base.root)


def unpad(piece: Any, projection: str) -> str:
    """Remove only the exact virtual row-group wrappers frozen with this piece."""
    prefix = getattr(piece, "source_prefix", "")
    suffix = getattr(piece, "source_suffix", "")
    if not projection.startswith(prefix) or not projection.endswith(suffix):
        raise ValueError("table piece virtual wrappers changed")
    end = len(projection) - len(suffix) if suffix else len(projection)
    if len(prefix) >= end:
        raise ValueError("table piece must retain its original row projection")
    return projection[len(prefix) : end]


def _layout(parent: AtomicItem) -> tuple[tuple[Event, ...], dict[int, tuple[str, ...]], tuple[str, ...]]:
    sections = {
        ref: entry
        for ref, entry in parent.registry.items()
        if entry.kind == "g" and entry.hints.get("element") in _SECTIONS and entry.parent_ref == parent.node_key
    }
    row_parents = {parent.node_key, *sections, *(entry.source_node_key for entry in sections.values())}
    rows = {
        ref
        for ref, entry in parent.registry.items()
        if entry.kind == "g" and entry.hints.get("element") == "tr" and entry.parent_ref in row_parents
    }
    events = validate_projection(parent, parent.source_projection, parent.registry)
    stack: list[str] = []
    cuts: dict[int, tuple[str, ...]] = {0: ()}
    ordered: list[str] = []
    for position, event in enumerate(events, 1):
        if event.kind != "marker" or event.value[:1] not in {"+", "-"}:
            continue
        edge, ref = event.value[0], event.value[1:]
        if edge == "+":
            stack.append(ref)
        else:
            stack.pop()  # validate_projection already proved matching nesting.
            if ref in rows:
                if any(ancestor not in sections for ancestor in stack):
                    raise ValueError("table row is not directly owned by its table or row group")
                cuts[position] = tuple(stack)
                ordered.append(ref)
    if set(ordered) != rows:
        raise ValueError("table projection does not cover every outer row")
    # The last piece also owns the original closing groups and trailing content.
    if ordered:
        cuts.pop(next(reversed(cuts)))
    cuts[len(events)] = ()
    return tuple(events), cuts, tuple(ordered)


def _padding(stack: Sequence[str], *, close: bool = False) -> str:
    return events_to_projection(
        Event(kind="marker", value=("-" if close else "+") + ref) for ref in (reversed(stack) if close else stack)
    )


def split_table(
    inventory: AtomicDocument,
    raw: bytes,
    parent: AtomicItem,
    limits: BudgetLimits,
    model: str,
) -> tuple[PreflightPiece, ...]:
    """Keep each outer tr whole; virtual section wrappers never reach parent output."""
    from engine.services.preflight import _piece

    events, cuts, rows = _layout(parent)
    if len(rows) < 2:
        return ()
    index = index_resource(parse_resource(raw, inventory.document.resource.media_type))
    row_entries = {ref: parent.registry[ref] for ref in rows}
    byte_ends = {
        ref: index.nodes[inventory.document.nodes[entry.source_node_key].element_path].full.end
        for ref, entry in row_entries.items()
    }
    positions = tuple(cuts)
    byte_cuts = (parent.source_span.byte_start, *(byte_ends[ref] for ref in rows[:-1]), parent.source_span.byte_end)
    if len(positions) != len(byte_cuts) or tuple(sorted(byte_cuts)) != byte_cuts:
        raise ValueError("table row markers differ from their source-byte order")
    result: list[PreflightPiece] = []
    start = 0
    while start < len(positions) - 1:
        chosen = None
        chosen_end = start + 1
        for end in range(start + 1, len(positions)):
            prefix = _padding(cuts[positions[start]])
            suffix = _padding(cuts[positions[end]], close=True)
            projection = prefix + events_to_projection(events[positions[start] : positions[end]]) + suffix
            span = ByteSpan(byte_start=byte_cuts[start], byte_end=byte_cuts[end])
            piece_id = (
                "pc-"
                + canonical_hash(
                    {
                        "item_id": parent.item_id,
                        "bytes": (span.byte_start, span.byte_end),
                        "source": canonical_hash(projection),
                    }
                )[:32]
            )
            candidate = _piece(inventory, parent, piece_id, projection, span, limits, model).model_copy(
                update={"source_prefix": prefix, "source_suffix": suffix}
            )
            if not candidate.translate.fits or not candidate.review.fits:
                break
            chosen, chosen_end = candidate, end
        if chosen is None:
            return ()
        result.append(chosen)
        start = chosen_end
    return tuple(result) if len(result) > 1 else ()


def validate_table_pieces(parent: AtomicItem, pieces: Sequence[Any]) -> None:
    """Prove row seams and padding against the unchanged complete parent projection."""
    events, cuts, rows = _layout(parent)
    if len(rows) < 2:
        raise ValueError("split tables require multiple complete outer rows")
    position = 0
    seen: list[str] = []
    for piece in pieces:
        if piece.source_prefix != _padding(cuts[position]):
            raise ValueError("table piece prefix differs from its frozen row-group boundary")
        core = parse_projection(unpad(piece, piece.source_projection))
        end = position + len(core)
        if end not in cuts or tuple(core) != events[position:end]:
            raise ValueError("table pieces must cut only between complete outer rows")
        if piece.source_suffix != _padding(cuts[end], close=True):
            raise ValueError("table piece suffix differs from its frozen row-group boundary")
        validate_projection(piece.source_projection)
        contained = [
            event.value[1:]
            for event in core
            if event.kind == "marker" and event.value.startswith("+g") and event.value[1:] in rows
        ]
        if not contained:
            raise ValueError("each split table piece must contain complete outer rows")
        seen.extend(contained)
        position = end
    if position != len(events) or tuple(seen) != rows:
        raise ValueError("table pieces must exactly cover every outer row in source order")


__all__ = ["row_receipt", "split_table", "unpad", "upgrade_receipt", "validate_table_pieces"]
