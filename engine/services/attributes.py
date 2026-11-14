"""Source-bound context for retrying ambiguous XML attributes."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from engine.core.markup import find_by_element_path, qname_local_name
from engine.epub.parsing import parse_resource
from engine.schemas.contracts import ItemRecord
from engine.services import state
from engine.services.atomic import IdentityMismatch

_CONTEXT_MAX = 700
_FEEDBACK_MAX = 1200


def source_retry_context(journal, item_id: str, *, max_chars: int = _CONTEXT_MAX) -> str:
    """Return deterministic source evidence for one frozen attribute member."""
    if type(max_chars) is not int or max_chars < 2:
        raise ValueError("attribute retry context limit must be at least two characters")
    session = journal.session
    member = session.index.members_by_id.get(item_id)
    if member is None or member.channel != "attribute":
        return ""
    preparation = session.verify().preparation
    member = session.index.members_by_id.get(item_id)
    if member is None or not session.owns(member):
        return ""
    inventory = session.index.inventories_by_id[member.document_id]
    parent = next((item for item in inventory.items if item.item_id == member.parent_item_id), None)
    if parent is None or parent.channel != "attribute" or parent.kind != "attribute" or len(parent.slot_ids) != 1:
        return ""
    attribute = parent.region.get("attribute_name")
    if not isinstance(attribute, str) or not attribute:
        return ""
    slot = inventory.document.source_slots[parent.slot_ids[0]]
    if slot.field != "attribute" or slot.attribute_name != attribute or slot.node_key != parent.node_key:
        raise IdentityMismatch("attribute retry source slot differs from its frozen item")

    # The ready session has validated the book. Rebind just this document,
    # then prove the exact node and attribute against the source resource.
    document = journal.store.read_document(
        member.document_id, expected_hash=preparation.document_hashes[member.document_id]
    )
    if (
        document != inventory.document
        or preparation.source_hash != document.source_hash
        or member.root_node_key != parent.node_key
    ):
        raise IdentityMismatch("attribute retry document differs from its frozen source")
    snapshot = state.snapshot(journal.store.root)
    if _file_hash(snapshot) != preparation.source_hash:
        raise IdentityMismatch("attribute retry source snapshot changed")
    try:
        with zipfile.ZipFile(snapshot) as archive:
            raw = archive.read(document.resource.path)
    except (KeyError, zipfile.BadZipFile) as error:
        raise IdentityMismatch("attribute retry source resource is unavailable") from error
    if hashlib.sha256(raw).hexdigest() != document.resource.source_sha256:
        raise IdentityMismatch("attribute retry source resource changed")
    parsed = parse_resource(raw, document.resource.media_type)
    if parsed.tree is None or parsed.diagnostics:
        raise IdentityMismatch("attribute retry source resource cannot be parsed")
    node_record = document.nodes[parent.node_key]
    node = find_by_element_path(parsed.tree, node_record.element_path)
    if node.tag != node_record.qname or node.get(attribute) != slot.source_value:
        raise IdentityMismatch("attribute retry node differs from its frozen source")

    attributes = {
        qname_local_name(name): _clip(value, 96)
        for name, value in sorted(node.attrib.items(), key=lambda value: qname_local_name(value[0]))
    }
    ancestor = node.getparent()
    if ancestor is None or not isinstance(ancestor.tag, str) or not " ".join("".join(ancestor.itertext()).split()):
        ancestor = None
    parent_value = (
        {
            "element": qname_local_name(ancestor.tag),
            "text": _clip(" ".join("".join(ancestor.itertext()).split()), 320),
        }
        if ancestor is not None
        else None
    )
    payload = {
        "source_context": {
            "resource": document.resource.path,
            "element": qname_local_name(node.tag),
            "attribute": attribute,
            "attributes": attributes,
            **({"parent": parent_value} if parent_value is not None else {}),
        }
    }
    rendered = _json(payload)
    if len(rendered) <= max_chars:
        return rendered
    context = payload["source_context"]
    assert isinstance(context, dict)
    parent = context.get("parent")
    if isinstance(parent, dict):
        text = parent.get("text")
        if isinstance(text, str):
            parent["text"] = _clip(text, max(0, len(text) - (len(rendered) - max_chars)))
    rendered = _json(payload)
    if len(rendered) <= max_chars:
        return rendered
    context.pop("parent", None)
    rendered = _json(payload)
    if len(rendered) <= max_chars:
        return rendered
    essential = {name: _clip(value, 64) for name, value in attributes.items() if name in {attribute, "src"}}
    context["attributes"] = essential
    rendered = _json(payload)
    if len(rendered) <= max_chars:
        return rendered
    context.pop("resource", None)
    rendered = _json(payload)
    if len(rendered) <= max_chars:
        return rendered
    context.pop("attributes", None)
    rendered = _json(payload)
    return rendered if len(rendered) <= max_chars else "{}"


def contextual_retry_feedback(journal, record: ItemRecord, message: str) -> str:
    """Append fresh source evidence without accumulating earlier retry context."""
    marker = '\n{"source_context":'
    if marker in message:
        message = message.split(marker, 1)[0]
    diagnostic = message[:_FEEDBACK_MAX]
    context = source_retry_context(journal, record.item_id)
    if not context:
        return diagnostic
    available = _FEEDBACK_MAX - len(context) - 1
    return diagnostic[: max(0, available)] + "\n" + context


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return "…"[:limit]
    return value[: limit - 1] + "…"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = ["contextual_retry_feedback", "source_retry_context"]
