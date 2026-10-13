"""Import and prove explicitly authorized Codex-authored body translations."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engine.agents.workflow import _saved_batch, _target_error, _wire_items
from engine.schemas.contracts import ItemRecord, ItemStatus, canonical_hash, canonical_json_bytes, parse_contract
from engine.schemas.manual import ManualArtifact
from engine.schemas.members import RequestMember
from engine.services import state
from engine.services.atomic import IdentityMismatch, StaleWrite

if TYPE_CHECKING:
    from engine.services.journal import BodyJournal

_IMPORTABLE = {ItemStatus.PENDING, ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE, ItemStatus.NEEDS_ATTENTION}
_PRESERVED_CHECKS = ("translation_frame", "translation_epoch", "review_epoch")


def import_codex_translation(
    journal: BodyJournal,
    member: RequestMember,
    existing: ItemRecord,
    target_projection: str,
    *,
    user_authorized: bool,
) -> ItemRecord:
    """Commit one locally validated manual target without inventing provider evidence."""
    journal.session.verify()
    if user_authorized is not True:
        raise PermissionError("manual translation import requires explicit user authorization")
    if not journal.session.owns(member):
        raise IdentityMismatch("manual translation member is not canonical for this session")
    current = journal._records.get(member.item_id)
    if current != existing:
        raise StaleWrite("manual translation prior record changed")
    if existing.status not in _IMPORTABLE:
        raise StaleWrite("manual translation can replace only pending, draft, or needs-attention state")
    _validate_target(journal.session, member, target_projection)
    artifact = ManualArtifact(
        source_hash=journal.session.prepared.plan.source_hash,
        member_id=member.item_id,
        member_hash=canonical_hash(member),
        source_projection_hash=canonical_hash(member.source_projection),
        prior=existing,
        target_projection=target_projection,
        target_hash=canonical_hash(target_projection),
    )
    digest = canonical_hash(artifact)
    result = existing.model_copy(
        update={
            "stage": "reviewed",
            "status": ItemStatus.REVIEWED,
            "target_projection": target_projection,
            "target_hash": artifact.target_hash,
            "checks": _checks(existing, digest),
            "request_id": existing.request_id if "translation_frame" in existing.checks else None,
            "failure": None,
            "next_action": None,
        }
    )
    manual_proof(journal.session, result, artifact=artifact)
    artifact_path = _artifact_path(journal.store.root, digest)
    result_path = journal._result_path(member.item_id)
    with journal.store.lock(), state.batch(journal.store.root):
        persisted = ItemRecord.model_validate_json(state.read(result_path))
        if persisted != existing:
            raise StaleWrite("manual translation prior record changed on disk")
        if state.exists(artifact_path):
            saved = parse_contract(state.read(artifact_path), ManualArtifact, "epubox-manual-1")
            if saved != artifact:
                raise StaleWrite("manual translation artifact hash collision")
        else:
            journal.store._base.atomic_write_bytes(artifact_path, canonical_json_bytes(artifact))
        journal.store._base.atomic_write_bytes(result_path, canonical_json_bytes(result))
    journal._update_result(existing, result)
    return result


def manual_proof(session, record: ItemRecord, *, artifact: ManualArtifact | None = None) -> None:
    """Verify a reviewed record solely from canonical source and manual evidence."""
    pointer = record.checks.get("manual")
    if not isinstance(pointer, Mapping) or set(pointer) != {"artifact_hash"}:
        raise IdentityMismatch("manual result lacks its exact artifact hash")
    digest = pointer.get("artifact_hash")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise IdentityMismatch("manual result has an invalid artifact hash")
    if artifact is None:
        try:
            artifact = parse_contract(
                state.read(_artifact_path(session.store.root, digest)), ManualArtifact, "epubox-manual-1"
            )
        except Exception as error:
            raise IdentityMismatch(f"manual translation artifact is invalid: {error}") from error
    if canonical_hash(artifact) != digest:
        raise IdentityMismatch("manual translation artifact hash changed")
    member = session.index.members_by_id.get(record.item_id)
    if member is None or not session.owns(member) or artifact.member_id != member.item_id:
        raise IdentityMismatch("manual translation artifact belongs to another member")
    if (
        artifact.source_hash != session.prepared.plan.source_hash
        or artifact.member_hash != canonical_hash(member)
        or artifact.source_projection_hash != canonical_hash(member.source_projection)
    ):
        raise IdentityMismatch("manual translation source identity changed")
    prior = artifact.prior
    if prior.status not in _IMPORTABLE:
        raise IdentityMismatch("manual translation replaced an ineligible prior state")
    manifest, wire = _canonical_manifest_and_wire(session, member, prior)
    raw_terms = wire.get("terms")
    terms = tuple(term for term in raw_terms if isinstance(term, Mapping)) if isinstance(raw_terms, list) else ()
    if (
        prior.selected_term_ids != manifest.term_ids_by_item[member.item_id]
        or prior.term_applicability != {str(term["term_id"]): term["role"] for term in terms}
        or prior.terms_hash != manifest.terms_hashes[member.item_id]
        or prior.context_hash != manifest.context_hashes[member.item_id]
    ):
        raise IdentityMismatch("manual translation prior metadata differs from its canonical batch")
    if "translation_frame" in prior.checks:
        _saved_batch(session, member, prior)
    elif (
        prior.stage != "translation"
        or prior.status != ItemStatus.PENDING
        or prior.target_projection is not None
        or not _valid_retried_pending_checks(prior.checks)
        or prior.request_id is not None
        or prior.failure is not None
        or prior.next_action is not None
    ):
        raise IdentityMismatch("manual translation prior state lacks canonical provenance")
    if any(
        getattr(record, name) != getattr(prior, name)
        for name in (
            "item_id",
            "segment_id",
            "selected_term_ids",
            "term_applicability",
            "terms_hash",
            "context_hash",
        )
    ):
        raise IdentityMismatch("manual result changed frozen source, terms, or context metadata")
    if (
        record.stage != "reviewed"
        or record.status != ItemStatus.REVIEWED
        or record.target_projection != artifact.target_projection
        or record.target_hash != artifact.target_hash
        or record.failure is not None
        or record.next_action is not None
        or record.checks != _checks(prior, digest)
        or record.request_id != (prior.request_id if "translation_frame" in prior.checks else None)
    ):
        raise IdentityMismatch("manual result differs from its immutable artifact")
    _validate_target(session, member, artifact.target_projection, wire)


def _checks(prior: ItemRecord, digest: str) -> dict[str, Any]:
    return {
        **{name: prior.checks[name] for name in _PRESERVED_CHECKS if name in prior.checks},
        "manual": {"artifact_hash": digest},
    }


def _validate_target(session, member: RequestMember, target: str, wire=None) -> None:
    if wire is None:
        _manifest, wire = _canonical_manifest_and_wire(session, member)
    error = _target_error(member, target, wire)
    if error is not None:
        raise ValueError(f"manual translation failed local validation: {error}")


def _canonical_manifest_and_wire(session, member: RequestMember, prior: ItemRecord | None = None):
    if prior is not None and "translation_frame" in prior.checks:
        batch = _saved_batch(session, member, prior)
        return batch.manifest, _wire_items(batch)[member.item_id]
    batch = session.initial_batch(member.item_id)
    if batch is not None:
        return batch.manifest, _wire_items(batch)[member.item_id]
    restore = getattr(session, "initial_request_wire", None)
    restored = restore(member.item_id, prior) if restore is not None else None
    if restored is None:
        raise IdentityMismatch("manual translation member has no unique canonical batch")
    return restored


def _valid_retried_pending_checks(checks: Mapping[str, Any]) -> bool:
    if not checks:
        return True
    if not set(checks).issubset({"translation_epoch", "retry_feedback"}):
        return False
    epoch = checks.get("translation_epoch")
    feedback = checks.get("retry_feedback")
    return (
        type(epoch) is int and epoch > 0 and (feedback is None or isinstance(feedback, str) and len(feedback) <= 1200)
    )


def _artifact_path(root: Path, digest: str) -> Path:
    return root / "manual" / f"{digest}.json"


__all__ = ["import_codex_translation", "manual_proof"]
