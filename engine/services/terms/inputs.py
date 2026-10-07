"""Strict, model-free loading of user terminology rules."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from engine.schemas.contracts import (
    DocumentPlan,
    TermScope,
    UserTerm,
    canonical_hash,
    strict_json_loads,
    validate_term_scopes,
)
from engine.services import state

_TERM_FIELDS = {"source", "target", "aliases", "scope", "mode", "match_policy", "note"}


def load_user_terms(
    path: str | Path | None,
    *,
    document_ids: Collection[str],
    unit_ids: Collection[str],
    unit_documents: Mapping[str, str] | None = None,
) -> tuple[tuple[UserTerm, ...], str]:
    """Load a legacy user term snapshot with its established normalization."""
    return _load_terms(
        path,
        document_ids=document_ids,
        unit_ids=unit_ids,
        unit_documents=unit_documents,
        preserve_note=False,
    )


def _load_terms(
    path: str | Path | None,
    *,
    document_ids: Collection[str],
    unit_ids: Collection[str],
    unit_documents: Mapping[str, str] | None,
    preserve_note: bool,
) -> tuple[tuple[UserTerm, ...], str]:
    if path is None:
        terms: tuple[UserTerm, ...] = ()
        return terms, canonical_hash(terms)

    source_path = Path(path)
    raw = strict_json_loads(state.read(source_path))
    entries = _entries(raw)
    normalized: dict[str, UserTerm] = {}
    for index, entry in enumerate(entries):
        term = _normalize_entry(entry, index=index, preserve_note=preserve_note)
        existing = normalized.get(term.term_id)
        if existing is not None and existing != term:  # pragma: no cover - truncated hashes remain detectable
            raise ValueError(f"user term identity collision at item {index}")
        normalized[term.term_id] = term

    terms = tuple(sorted(normalized.values(), key=lambda term: term.term_id))
    documents = set(document_ids)
    units = set(unit_ids)
    validate_term_scopes(terms, documents, units)
    if unit_documents is not None:
        if set(unit_documents) != units or not set(unit_documents.values()).issubset(documents):
            raise ValueError("unit_documents must exactly describe the supplied book scope")
        _validate_conflicts(terms, unit_documents)
    return terms, canonical_hash(terms)


def load_atomic_terms(
    path: str | Path | None,
    documents: Sequence[DocumentPlan],
) -> tuple[tuple[UserTerm, ...], str]:
    """Load rules against the actual immutable documents that define their scopes."""
    document_ids = [document.document_id for document in documents]
    if len(set(document_ids)) != len(document_ids):
        raise ValueError("document IDs must be unique")
    unit_documents: dict[str, str] = {}
    for document in documents:
        for unit in document.units:
            if unit.document_id != document.document_id or unit.unit_id in unit_documents:
                raise ValueError("document Units must have unique IDs and match their document")
            unit_documents[unit.unit_id] = document.document_id
    return _load_terms(
        path,
        document_ids=document_ids,
        unit_ids=unit_documents,
        unit_documents=unit_documents,
        preserve_note=True,
    )


def _entries(raw: Any) -> list[Mapping[str, Any]]:
    if isinstance(raw, dict):
        if not all(isinstance(source, str) and isinstance(target, str) for source, target in raw.items()):
            raise TypeError("user term mapping must contain only source-to-target strings")
        return [{"source": source, "target": target} for source, target in raw.items()]
    if not isinstance(raw, list):
        raise TypeError("user term file must be a source-to-target object or a list of term objects")
    if not all(isinstance(entry, dict) for entry in raw):
        raise TypeError("each user term must be an object")
    return raw


def _normalize_entry(entry: Mapping[str, Any], *, index: int, preserve_note: bool) -> UserTerm:
    unknown = set(entry) - _TERM_FIELDS
    if unknown:
        raise ValueError(f"user term {index} contains unknown fields: {sorted(unknown)}")

    source = entry.get("source")
    mode = entry.get("mode", "preferred")
    target = entry.get("target", source if mode == "keep_source" else None)
    aliases = entry.get("aliases", [])
    scope = entry.get("scope", {"kind": "book"})
    note = entry.get("note", "")
    match_policy = entry.get("match_policy", "exact")
    if not isinstance(source, str) or not isinstance(target, str):
        raise TypeError(f"user term {index} source and target must be strings")
    if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
        raise TypeError(f"user term {index} aliases must be a string array")
    if not isinstance(scope, dict):
        raise TypeError(f"user term {index} scope must be an object")
    if not isinstance(mode, str) or not isinstance(match_policy, str) or not isinstance(note, str):
        raise TypeError(f"user term {index} mode, match_policy, and note must be strings")

    source = source.strip()
    target = target.strip()
    aliases = [alias.strip() for alias in aliases]
    note = note if preserve_note else note.strip()
    normalized_scope = TermScope.model_validate(scope)
    identity_payload = {
        "source": source,
        "target": target,
        "aliases": sorted(set(aliases)),
        "scope": normalized_scope.model_dump(mode="json"),
        "mode": mode,
        "match_policy": match_policy,
        "note": note,
    }
    return UserTerm.model_validate({"term_id": f"ut-{canonical_hash(identity_payload)[:24]}", **identity_payload})


def _validate_conflicts(terms: tuple[UserTerm, ...], unit_documents: Mapping[str, str]) -> None:
    for index, term in enumerate(terms):
        for other in terms[index + 1 :]:
            if (
                term.target != other.target
                and _scope_units(term.scope, unit_documents) & _scope_units(other.scope, unit_documents)
                and _spellings_overlap(term, other)
            ):
                raise ValueError(f"conflicting user terminology rules: {term.source!r}")


def _scope_units(scope: TermScope, unit_documents: Mapping[str, str]) -> set[str]:
    if scope.kind == "book":
        return set(unit_documents)
    if scope.kind == "documents":
        return {unit_id for unit_id, document_id in unit_documents.items() if document_id in scope.document_ids}
    return set(scope.unit_ids)


def _spellings_overlap(left: UserTerm, right: UserTerm) -> bool:
    left_values = {left.source, *left.aliases}
    right_values = {right.source, *right.aliases}
    if left.match_policy == "casefold" or right.match_policy == "casefold":
        return bool({value.casefold() for value in left_values} & {value.casefold() for value in right_values})
    return bool(left_values & right_values)


__all__ = ["load_atomic_terms", "load_user_terms"]
