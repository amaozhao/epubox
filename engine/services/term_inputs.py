"""Strict, model-free loading of user terminology rules."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from engine.schemas.contracts import (
    TermScope,
    UserTerm,
    canonical_hash,
    strict_json_loads,
    validate_term_scopes,
)

_TERM_FIELDS = {"source", "target", "aliases", "scope", "mode", "match_policy", "note"}


def load_user_terms(
    path: str | Path | None,
    *,
    document_ids: Collection[str],
    unit_ids: Collection[str],
) -> tuple[tuple[UserTerm, ...], str]:
    """Load and normalize a user term file before any model work starts."""
    if path is None:
        terms: tuple[UserTerm, ...] = ()
        return terms, canonical_hash(terms)

    source_path = Path(path)
    raw = strict_json_loads(source_path.read_bytes())
    entries = _entries(raw)
    normalized: dict[str, UserTerm] = {}
    for index, entry in enumerate(entries):
        term = _normalize_entry(entry, index=index)
        existing = normalized.get(term.term_id)
        if existing is not None and existing != term:  # pragma: no cover - truncated hashes remain detectable
            raise ValueError(f"user term identity collision at item {index}")
        normalized[term.term_id] = term

    terms = tuple(sorted(normalized.values(), key=lambda term: term.term_id))
    validate_term_scopes(terms, set(document_ids), set(unit_ids))
    return terms, canonical_hash(terms)


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


def _normalize_entry(entry: Mapping[str, Any], *, index: int) -> UserTerm:
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
    note = note.strip()
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


__all__ = ["load_user_terms"]
