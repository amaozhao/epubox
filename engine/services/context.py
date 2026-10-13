"""Bounded body-proof caches and persisted proof dependency binding."""

from __future__ import annotations

import hashlib
from collections.abc import MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engine.schemas.contracts import canonical_hash
from engine.services import state
from engine.services.atomic import IdentityMismatch
from engine.services.working import BudgetCache

if TYPE_CHECKING:
    from engine.services.journal import BodyJournal

_CACHE_BYTES = 16 * 1024 * 1024
_PROOF_ROOTS = frozenset({"frames", "manual", "parts", "requests", "responses", "results"})


def proof_cache() -> MutableMapping[Any, Any]:
    return BudgetCache(_CACHE_BYTES)


def clear_proof_caches(journal: BodyJournal) -> None:
    journal._responses.clear()
    journal._translations.clear()
    journal._reviews.clear()
    journal._translation_batches.clear()
    journal._review_batches.clear()
    journal._review_records.clear()


def proof_fingerprint(root: Path) -> str:
    """Hash every mutable JSON dependency that can prove a body result."""
    if state.compact(root):
        return canonical_hash(
            state.dependency_fingerprint(root, prefixes=tuple(f"{name}/" for name in sorted(_PROOF_ROOTS)))
        )
    digest = hashlib.sha256()
    for path in state.glob(root, "**/*.json"):
        relative_path = path.relative_to(root)
        if not relative_path.parts or relative_path.parts[0] not in _PROOF_ROOTS:
            continue
        relative = relative_path.as_posix().encode()
        raw = state.read(path)
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def memory_fingerprint(journal: BodyJournal) -> str:
    return canonical_hash(
        {
            "requests": {key: canonical_hash(value) for key, value in journal._requests.items()},
            "results": {key: canonical_hash(value) for key, value in journal._records.items()},
        }
    )


def verify_persisted_proofs(journal: BodyJournal) -> None:
    """Skip repeated proof construction only when every persisted input is identical."""
    from engine.services.proofs import validate_records

    with journal.store.lock():
        journal.session.verify()
        current = proof_fingerprint(journal.store.root)
        memory = memory_fingerprint(journal)
        if (
            getattr(journal, "_proof_fingerprint", None) == current
            and getattr(journal, "_proof_memory_fingerprint", None) == memory
        ):
            return
        requests = journal._load_requests()
        records = journal._load_records()
        # Runtime usage values and their persisted schema counterparts can have
        # different model classes. Compare their canonical JSON identities.
        if requests.keys() != journal._requests.keys() or any(
            canonical_hash(request) != canonical_hash(journal._requests[key]) for key, request in requests.items()
        ):
            raise IdentityMismatch("body requests changed after workflow verification")
        if records != journal._records:
            raise IdentityMismatch("body results changed after workflow verification")
        clear_proof_caches(journal)
        validate_records(journal, tuple(records.values()))
        verified = proof_fingerprint(journal.store.root)
        if verified != current:
            raise IdentityMismatch("body proof dependencies changed during publication verification")
        journal._proof_fingerprint = verified
        journal._proof_memory_fingerprint = memory_fingerprint(journal)


__all__ = ["clear_proof_caches", "memory_fingerprint", "proof_cache", "proof_fingerprint", "verify_persisted_proofs"]
