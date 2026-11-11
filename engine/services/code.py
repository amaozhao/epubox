"""Source-proven preservation of code misclassified by historical extractors."""

from __future__ import annotations

import hashlib
from zipfile import ZipFile

from engine.core.code import is_rendered_python_block
from engine.core.markup import find_by_element_path
from engine.epub.parsing import XHTML_NAMESPACE, parse_resource
from engine.item.members import validate_member_target
from engine.item.policy import is_numbered_shell_block, is_styled_code_block
from engine.schemas.contracts import ItemRecord, ItemStatus, canonical_hash, canonical_json_bytes
from engine.services import state
from engine.services.atomic import IdentityMismatch, StaleWrite


def _source_code_version(session, member) -> int | None:
    # Frozen whole members retain their source spans and complete registry.
    # Attributes and split fragments cannot claim whole-block protection.
    if member.kind not in {"p", "div_text_region"} or member.channel != "body" or member.piece_count != 1:
        return None
    document = session.index.documents[member.document_id]
    with ZipFile(state.snapshot(session.store.root)) as archive:
        raw = archive.read(document.resource.path)
    if hashlib.sha256(raw).hexdigest() != document.resource.source_sha256:
        raise IdentityMismatch("preserved code source resource changed")
    parsed = parse_resource(raw, document.resource.media_type)
    if parsed.kind != "xhtml" or parsed.tree is None:
        return None
    node = find_by_element_path(parsed.tree, document.nodes[member.root_node_key].element_path)
    if (
        member.kind == "div_text_region"
        and not member.registry
        and node.tag == f"{{{XHTML_NAMESPACE}}}div"
        and "FixedLine" in (node.get("class") or "").split()
        and not len(node)
        and node.text == member.source_projection
        and any(
            ancestor.tag == f"{{{XHTML_NAMESPACE}}}div" and "ProgramCode" in (ancestor.get("class") or "").split()
            for ancestor in node.iterancestors()
        )
    ):
        return 1
    if member.kind == "p" and is_numbered_shell_block(node):
        version = 4
    elif member.kind == "p" and is_styled_code_block(node):
        version = 2
    elif member.kind == "div_text_region" and is_rendered_python_block(node):
        version = 3
    else:
        return None
    inventory = session.index.inventories_by_id[member.document_id]
    parent = next((item for item in inventory.items if item.item_id == member.parent_item_id), None)
    if (
        parent is not None
        and member.parent_hash == canonical_hash(parent)
        and parent.node_key == member.root_node_key
        and parent.source_projection == member.source_projection
        and parent.registry == member.registry
        and parent.source_span == member.source_span
        and (
            version in {2, 4}
            or parent.region.get("type") == "content"
            and parent.region.get("after_node_key") is None
            and parent.region.get("before_node_key") is None
        )
    ):
        return version
    return None


def preserved_code_proof(session, record: ItemRecord) -> None:
    """Re-prove source custody and exact preservation, without claiming model review."""
    from engine.agents.workflow import _saved_batch, _validate_record_frame

    member = session.index.members_by_id.get(record.item_id)
    if member is None or not session.owns(member) or record.segment_id != member.item_id:
        raise IdentityMismatch("preserved code member ownership changed")
    proof = record.checks.get("preserved_code")
    version = proof.get("version") if isinstance(proof, dict) else None
    if (
        record.status != ItemStatus.REVIEWED
        or record.stage != "reviewed"
        or record.target_projection != member.source_projection
        or record.target_hash != canonical_hash(member.source_projection)
        or record.failure is not None
        or record.next_action is not None
        or type(version) is not int
        or version not in {1, 2, 3, 4}
        or proof != {"version": version, "member_hash": canonical_hash(member)}
        or set(record.checks) - {"preserved_code", "translation_frame", "translation_epoch"}
    ):
        raise IdentityMismatch("preserved code result differs from its canonical source")
    if version in {3, 4} and record.request_id is None and "translation_frame" not in record.checks:
        initial = session.initial_batch(member.item_id)
        if initial is None:
            raise IdentityMismatch("preserved code member has no canonical initial batch")
        _validate_record_frame(member, record, initial)
    else:
        _saved_batch(session, member, record)
    validate_member_target(member, member.source_projection)
    source_version = _source_code_version(session, member)
    if source_version != version and (source_version, version) != (1, 2):
        raise IdentityMismatch("preserved code member is not a proven source code block")


def recover_preserved_code(journal) -> tuple[str, ...]:
    """Recover source-proven code from a frozen translation inventory."""
    journal.session.verify()
    changes = []
    for prior in journal._records.values():
        member = journal.session.index.members_by_id[prior.item_id]
        shell_candidate = member.kind == "p" and "#!" in member.source_projection
        if "preserved_code" in prior.checks or (
            prior.status not in {ItemStatus.PENDING, ItemStatus.NEEDS_ATTENTION} and not shell_candidate
        ):
            continue
        message = str((prior.failure or {}).get("message", ""))
        id_mismatch = (
            prior.stage == "translate"
            and prior.request_id is not None
            and message == f"translate:{prior.request_id}: request_id mismatch"
        )
        legacy_failure = (
            prior.status == ItemStatus.NEEDS_ATTENTION
            and prior.stage in {"translate", "review"}
            and (message.startswith("untranslated English remains:") or id_mismatch)
        )
        if not legacy_failure and member.kind != "div_text_region" and not shell_candidate:
            continue
        version = _source_code_version(journal.session, member)
        if version is None or (version not in {3, 4} and not legacy_failure):
            continue
        checks = {
            name: prior.checks[name] for name in ("translation_frame", "translation_epoch") if name in prior.checks
        }
        checks["preserved_code"] = {
            "version": version,
            "member_hash": canonical_hash(member),
        }
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
