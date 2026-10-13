"""Coverage and marker-localization checks for request-member materialization."""

from collections.abc import Mapping, Sequence
from itertools import pairwise
from types import MappingProxyType
from typing import Any

from engine.item.inline import projection_identities, validate_projection
from engine.schemas.bridge import AtomicItem
from engine.schemas.contracts import RegistryEntry, canonical_hash
from engine.schemas.members import RequestMember
from engine.services.preflight import validate_list_pieces


def initialize_verified_catalog(
    index: Any,
    report: Any,
    member_ids: Sequence[str],
    *,
    members: Mapping[str, RequestMember],
    member_sequence: Sequence[RequestMember],
    metadata: Mapping[str, tuple[str, str, str]],
    document_order: Sequence[str],
    resource_paths: Mapping[str, str],
    inventories: Mapping[str, Any],
    documents: Mapping[str, Any],
    texts: Mapping[str, str],
    has_text: Mapping[str, bool],
) -> None:
    ordered = tuple(member_ids)
    if not ordered or len(set(ordered)) != len(ordered) or set(ordered) != set(metadata):
        raise ValueError("member catalog requires unique complete metadata")
    if set(resource_paths) != set(document_order) or any(
        document_id not in resource_paths for document_id, _channel, _unit_id in metadata.values()
    ):
        raise ValueError("member index document catalog is incomplete")
    index.inventories_by_id = inventories
    index.inventories = ()
    index.documents = documents
    index.resource_paths = dict(resource_paths)
    index.members = member_sequence
    index.members_by_id = members
    index.items_by_id = members
    index._glossary_digest = None
    index.source_hash = report.source_hash
    index.source_tokens = MappingProxyType({piece.piece_id: piece.translate.source_tokens for piece in report.pieces})
    if set(index.source_tokens) != set(ordered):
        raise ValueError("member index token catalog differs from verified members")
    body_tokens: dict[str, int] = {}
    for item_id, (document_id, channel, _unit_id) in metadata.items():
        if channel == "body":
            body_tokens[document_id] = body_tokens.get(document_id, 0) + index.source_tokens[item_id]
    index.body_tokens = MappingProxyType(body_tokens)
    index.document_order = tuple(document_order)
    index._texts = texts
    if set(texts) != set(ordered) or set(has_text) != set(ordered):
        raise ValueError("member text catalog differs from verified members")
    lane_positions: dict[tuple[str, str], int] = {}
    channels: dict[tuple[str, str], tuple[str, ...]] = {}
    index._lane_positions = {}
    index._previous_ids = {}
    for item_id in ordered:
        document_id, channel, _unit_id = metadata[item_id]
        lane = (document_id, channel)
        index._lane_positions[item_id] = lane_positions.get(lane, 0)
        lane_positions[lane] = index._lane_positions[item_id] + 1
        prior = channels.get(lane, ())
        index._previous_ids[item_id] = prior
        if has_text[item_id]:
            channels[lane] = (*prior, item_id)[-2:]


def validate_coverage(parent: AtomicItem, pieces: Sequence[Any]) -> None:
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


def local_registry(parent: AtomicItem, projection: str) -> dict[str, RegistryEntry]:
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


__all__ = ["initialize_verified_catalog", "local_registry", "validate_coverage"]
