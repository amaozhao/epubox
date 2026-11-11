"""Split nested list requests while retaining their original ancestor markup."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from engine.epub.parsing import parse_resource
from engine.epub.ranges import index_resource
from engine.item.inline import events_to_projection, parse_projection, validate_projection
from engine.schemas.bridge import AtomicDocument, AtomicItem, ByteSpan
from engine.schemas.budget import BudgetLimits
from engine.schemas.internal import Event
from engine.services.tables import _padding, split_padded_groups, unpad

if TYPE_CHECKING:
    from engine.services.preflight import PreflightPiece

_LIST_TAGS = {"ul", "ol", "li"}


def split_list(
    inventory: AtomicDocument,
    raw: bytes,
    parent: AtomicItem,
    limits: BudgetLimits,
    model: str,
) -> tuple[PreflightPiece, ...]:
    direct = _split_direct(inventory, raw, parent, limits, model)
    if direct:
        return direct
    # Preserve successful historical piece identities before trying new seams.
    for protected_domains in (False, True):
        events, cuts, anchors = _nested_layout(parent, protected_domains=protected_domains)
        if len(cuts) < 3:
            continue
        offsets = {
            position: (
                inventory.source_map.node_spans[parent.registry[ref].source_node_key].byte_start
                if opening
                else inventory.source_map.node_spans[parent.registry[ref].source_node_key].byte_end
            )
            for position, (ref, opening) in anchors.items()
        }
        offsets[0] = parent.source_span.byte_start
        offsets[len(events)] = parent.source_span.byte_end
        pieces = split_padded_groups(
            inventory, parent, events, cuts, tuple(offsets[pos] for pos in cuts), limits, model
        )
        if pieces:
            return pieces
    return ()


def _nested_layout(
    parent: AtomicItem,
    *,
    protected_domains: bool = True,
) -> tuple[tuple[Event, ...], dict[int, tuple[str, ...]], dict[int, tuple[str, bool]]]:
    events = validate_projection(parent)
    stack: list[str] = []
    cuts: dict[int, tuple[str, ...]] = {0: ()}
    anchors: dict[int, tuple[str, bool]] = {}
    payload = False
    for position, event in enumerate(events):
        if event.kind == "text":
            payload |= bool(event.value.strip())
            continue
        edge, ref = event.value[0], event.value[1:]
        entry = parent.registry[ref]
        if edge == "=":
            payload |= bool(entry.source_text.strip())
            continue
        tag = entry.hints.get("element")
        opening_list = edge == "+" and tag in {"ul", "ol"}
        closing_item = edge == "-" and tag == "li"
        if edge == "-":
            stack.pop()  # Source validation already proves marker nesting.
        if (
            payload
            and (opening_list or closing_item)
            and all(
                (
                    parent.registry[ancestor].kind == "g"
                    and parent.registry[ancestor].hints.get("element") in _LIST_TAGS
                )
                # A hard interval labels the prose beside an opaque code atom;
                # it is not an element whose content must stay in one request.
                # Balance it virtually and remove that padding before merging.
                or (
                    protected_domains
                    and parent.registry[ancestor].kind == "b"
                    and parent.registry[ancestor].boundary_type == "hard_interval"
                )
                for ancestor in stack
            )
        ):
            seam = position if opening_list else position + 1
            cuts[seam] = tuple(stack)
            anchors[seam] = (ref, opening_list)
            payload = False
        if edge == "+":
            stack.append(ref)
    # Keep closing-only source tails with the final real list item.
    if not payload and len(cuts) > 1:
        cuts.pop(next(reversed(cuts)))
    cuts[len(events)] = ()
    return events, cuts, anchors


def validate_list_pieces(
    parent: AtomicItem, pieces: Sequence[Any], *, node_spans: Mapping[str, ByteSpan] | None = None
) -> None:
    if not any(piece.source_prefix or piece.source_suffix for piece in pieces):
        _validate_direct(parent, pieces)
        return
    events, cuts, anchors = _nested_layout(parent)
    offsets = {0: parent.source_span.byte_start, len(events): parent.source_span.byte_end}
    if node_spans is not None:
        offsets.update(
            (
                position,
                node_spans[parent.registry[ref].source_node_key].byte_start
                if opening
                else node_spans[parent.registry[ref].source_node_key].byte_end,
            )
            for position, (ref, opening) in anchors.items()
            if position in cuts
        )
    position = 0
    for piece in pieces:
        if position not in cuts or piece.source_prefix != _padding(cuts[position]):
            raise ValueError("list piece prefix differs from its source list boundary")
        core = parse_projection(unpad(piece, piece.source_projection))
        end = position + len(core)
        if end not in cuts or tuple(core) != events[position:end]:
            raise ValueError("list pieces must cut only at complete list entry boundaries")
        if piece.source_suffix != _padding(cuts[end], close=True):
            raise ValueError("list piece suffix differs from its source list boundary")
        if node_spans is not None and (
            piece.source_span.byte_start != offsets[position] or piece.source_span.byte_end != offsets[end]
        ):
            raise ValueError("list piece byte span differs from its source list boundary")
        validate_projection(piece.source_projection)
        position = end
    if position != len(events):
        raise ValueError("list pieces must exactly cover their parent in source order")


def _split_direct(
    inventory: AtomicDocument,
    raw: bytes,
    item: AtomicItem,
    limits: BudgetLimits,
    model: str,
) -> tuple[PreflightPiece, ...]:
    from engine.services.preflight import _pack_fragments

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


def _validate_direct(parent: AtomicItem, pieces: Sequence[PreflightPiece]) -> None:
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
