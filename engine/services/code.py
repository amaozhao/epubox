"""Source-proven preservation of code misclassified by historical extractors."""

from __future__ import annotations

import hashlib
from zipfile import ZipFile

from engine.core.markup import find_by_element_path
from engine.epub.parsing import XHTML_NAMESPACE, parse_resource
from engine.item.members import validate_member_target
from engine.schemas.contracts import ItemRecord, ItemStatus, canonical_hash, canonical_json_bytes
from engine.services import state
from engine.services.atomic import IdentityMismatch, StaleWrite


def _is_source_code(session, member) -> bool:
    # Only whole text regions can be preserved this way. Attributes, tails,
    # mixed prose, and split members need their ordinary translation proofs.
    if member.kind != "div_text_region" or member.piece_count != 1 or member.registry:
        return False
    document = session.index.documents[member.document_id]
    with ZipFile(state.snapshot(session.store.root)) as archive:
        raw = archive.read(document.resource.path)
    if hashlib.sha256(raw).hexdigest() != document.resource.source_sha256:
        raise IdentityMismatch("preserved code source resource changed")
    parsed = parse_resource(raw, document.resource.media_type)
    if parsed.kind != "xhtml" or parsed.tree is None:
        return False
    node = find_by_element_path(parsed.tree, document.nodes[member.root_node_key].element_path)
    if (
        node.tag != f"{{{XHTML_NAMESPACE}}}div"
        or "FixedLine" not in (node.get("class") or "").split()
        or len(node)
        or node.text != member.source_projection
    ):
        return False
    return any(
        ancestor.tag == f"{{{XHTML_NAMESPACE}}}div" and "ProgramCode" in (ancestor.get("class") or "").split()
        for ancestor in (node, *node.iterancestors())
    )


def preserved_code_proof(session, record: ItemRecord) -> None:
    """Re-prove source custody and exact preservation, without claiming model review."""
    from engine.agents.workflow import _saved_batch

    member = session.index.members_by_id.get(record.item_id)
    if member is None or not session.owns(member) or record.segment_id != member.item_id:
        raise IdentityMismatch("preserved code member ownership changed")
    if (
        record.status != ItemStatus.REVIEWED
        or record.stage != "reviewed"
        or record.target_projection != member.source_projection
        or record.target_hash != canonical_hash(member.source_projection)
        or record.failure is not None
        or record.next_action is not None
        or record.checks.get("preserved_code") != {"version": 1, "member_hash": canonical_hash(member)}
        or set(record.checks) - {"preserved_code", "translation_frame", "translation_epoch"}
    ):
        raise IdentityMismatch("preserved code result differs from its canonical source")
    _saved_batch(session, member, record)
    validate_member_target(member, member.source_projection)
    if not _is_source_code(session, member):
        raise IdentityMismatch("preserved code member is not a source ProgramCode text region")


def recover_preserved_code(journal) -> tuple[str, ...]:
    """Repair only legacy English-residual failures; keep all accepted records intact."""
    journal.session.verify()
    changes = []
    for prior in journal._records.values():
        if (
            prior.status != ItemStatus.NEEDS_ATTENTION
            or prior.stage != "translate"
            or not str((prior.failure or {}).get("message", "")).startswith("untranslated English remains:")
        ):
            continue
        member = journal.session.index.members_by_id[prior.item_id]
        if not _is_source_code(journal.session, member):
            continue
        checks = {
            name: prior.checks[name] for name in ("translation_frame", "translation_epoch") if name in prior.checks
        }
        checks["preserved_code"] = {"version": 1, "member_hash": canonical_hash(member)}
        result = prior.model_copy(
            update={
                "stage": "reviewed",
                "status": ItemStatus.REVIEWED,
                "target_projection": member.source_projection,
                "target_hash": canonical_hash(member.source_projection),
                "checks": checks,
                "failure": None,
                "next_action": None,
            }
        )
        preserved_code_proof(journal.session, result)
        changes.append((prior, result))
    if changes:
        with journal.store.lock(), state.batch(journal.store.root):
            for prior, result in changes:
                path = journal._result_path(prior.item_id)
                if ItemRecord.model_validate_json(state.read(path)) != prior:
                    raise StaleWrite("preserved code prior result changed")
                journal.store._base.atomic_write_bytes(path, canonical_json_bytes(result))
        for prior, result in changes:
            journal._update_result(prior, result)
    return tuple(result.item_id for _, result in changes)
