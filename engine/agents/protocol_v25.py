"""Strict model responses for frozen terminology preparation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from engine.agents.protocol_v23 import (
    ProtocolError,
    _collect_items,
    _review_error,
    _valid_xml_text,
    strict_loads,
)

_CATEGORIES = {"term", "person", "organization", "product", "abbreviation", "other"}


@dataclass(frozen=True)
class TermsValidation:
    accepted: dict[str, tuple[dict[str, Any], ...]]
    rejected_candidates: dict[str, tuple[str, ...]]
    errors: dict[str, str]
    missing: tuple[str, ...]
    unknown: tuple[str, ...]


@dataclass(frozen=True)
class ReviewValidation:
    accepted: dict[str, dict[str, Any]]
    rejected_suggestions: dict[str, tuple[str, ...]]
    errors: dict[str, str]
    missing: tuple[str, ...]
    unknown: tuple[str, ...]


def _root(raw: str | bytes, protocol: str, request_id: str) -> list[Any]:
    value = strict_loads(raw)
    if not isinstance(value, dict) or set(value) != {"protocol", "request_id", "items"}:
        raise ProtocolError("response root must contain exactly protocol, request_id, and items")
    if value["protocol"] != protocol or value["request_id"] != request_id:
        raise ProtocolError("response protocol or request_id mismatch")
    if not isinstance(value["items"], list) or len(value["items"]) > 256:
        raise ProtocolError("items must be an array of at most 256 entries")
    return value["items"]


def _short_text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or not _valid_xml_text(value):
        raise ValueError(f"{name} must be non-empty valid text of at most {limit} characters")
    return value


def _candidate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not {"source", "target", "category", "evidence"}.issubset(value):
        raise ValueError("candidate requires source, target, category, and evidence")
    if set(value) - {"source", "target", "category", "aliases", "scope_hint", "note", "evidence"}:
        raise ValueError("candidate contains unknown fields")
    result = dict(value)
    _short_text(result["source"], "source", 500)
    _short_text(result["target"], "target", 500)
    if result["category"] not in _CATEGORIES:
        raise ValueError("invalid candidate category")
    aliases = result.setdefault("aliases", [])
    if not isinstance(aliases, list) or len(aliases) > 64:
        raise ValueError("aliases must be an array of at most 64 strings")
    for alias in aliases:
        _short_text(alias, "alias", 500)
    if result.setdefault("scope_hint", "document") not in {"document", "book"}:
        raise ValueError("invalid scope_hint")
    note = result.setdefault("note", "")
    if not isinstance(note, str) or len(note) > 2000 or not _valid_xml_text(note):
        raise ValueError("invalid note")
    evidence = result["evidence"]
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 64:
        raise ValueError("evidence must contain 1 to 64 citations")
    for citation in evidence:
        if not isinstance(citation, dict) or set(citation) != {"view_id", "source_quote"}:
            raise ValueError("evidence requires only view_id and source_quote")
        _short_text(citation["view_id"], "view_id", 200)
        _short_text(citation["source_quote"], "source_quote", 2000)
    return result


def validate_terms_response(raw: str | bytes, request_id: str, expected_item_ids: set[str]) -> TermsValidation:
    """Accept complete items independently; preserve malformed candidates as diagnostics."""
    items = _root(raw, "epubox-terms-1", request_id)
    candidates, errors, unknown, _ = _collect_items(items, expected_item_ids)
    accepted: dict[str, tuple[dict[str, Any], ...]] = {}
    rejected: dict[str, tuple[str, ...]] = {}
    for item_id, item in candidates.items():
        if set(item) != {"item_id", "candidates"} or not isinstance(item["candidates"], list):
            errors[item_id] = "term item must contain exactly item_id and candidates array"
            continue
        if len(item["candidates"]) > 256:
            errors[item_id] = "too many candidates"
            continue
        valid: list[dict[str, Any]] = []
        bad: list[str] = []
        for index, candidate in enumerate(item["candidates"]):
            try:
                valid.append(_candidate(candidate))
            except (ValueError, TypeError) as error:
                bad.append(f"candidate {index}: {error}")
        accepted[item_id] = tuple(valid)
        if bad:
            rejected[item_id] = tuple(bad)
    return TermsValidation(
        accepted,
        rejected,
        errors,
        tuple(sorted(expected_item_ids - set(candidates) - set(errors))),
        unknown,
    )


def validate_resolution_response(
    raw: str | bytes,
    request_id: str,
    group_id: str,
    candidate_ids: set[str],
    allowed_unit_ids: set[str],
) -> dict[str, Any]:
    value = strict_loads(raw)
    required = {"protocol", "request_id", "group_id", "decision", "selected_candidate_ids", "reason"}
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(required),
        frozenset(required | {"restricted_unit_ids"}),
    }:
        raise ProtocolError("resolution response has missing or unknown fields")
    if (value["protocol"], value["request_id"], value["group_id"]) != (
        "epubox-term-resolution-1",
        request_id,
        group_id,
    ):
        raise ProtocolError("resolution identity mismatch")
    if value["decision"] not in {"select", "defer"}:
        raise ProtocolError("invalid resolution decision")
    selected = value["selected_candidate_ids"]
    restricted = value.get("restricted_unit_ids", [])
    if (
        not isinstance(selected, list)
        or any(not isinstance(item, str) for item in selected)
        or len(selected) != len(set(selected))
        or not set(selected).issubset(candidate_ids)
        or not isinstance(restricted, list)
        or any(not isinstance(item, str) for item in restricted)
        or len(restricted) != len(set(restricted))
        or not set(restricted).issubset(allowed_unit_ids)
    ):
        raise ProtocolError("resolution selection exceeds request scope")
    if (value["decision"] == "defer" and (selected or restricted)) or (
        value["decision"] == "select" and len(selected) != 1
    ):
        raise ProtocolError("resolution decision and selection disagree")
    _short_text(value["reason"], "reason", 2000)
    return value


def validate_review_response_v25(
    raw: str | bytes,
    request_id: str,
    expected_items: Mapping[str, Mapping[str, Any]],
) -> ReviewValidation:
    """Keep quality issues even if a non-binding term suggestion is malformed."""
    items = _root(raw, "epubox-review-2", request_id)
    candidates, errors, unknown, _ = _collect_items(items, set(expected_items))
    accepted: dict[str, dict[str, Any]] = {}
    rejected: dict[str, tuple[str, ...]] = {}
    for item_id, item in candidates.items():
        suggestions = item.get("term_suggestions", [])
        if not isinstance(suggestions, list) or len(suggestions) > 256:
            rejected[item_id] = ("term_suggestions must be an array of at most 256 entries",)
            suggestions = []
        valid_suggestions: list[dict[str, Any]] = []
        bad: list[str] = []
        for index, suggestion in enumerate(suggestions):
            try:
                valid_suggestions.append(_candidate(suggestion))
            except (ValueError, TypeError) as error:
                bad.append(f"suggestion {index}: {error}")
        if bad:
            rejected[item_id] = (*rejected.get(item_id, ()), *bad)
        quality = {key: value for key, value in item.items() if key != "term_suggestions"}
        error = _review_error(quality, expected_items[item_id])
        if error:
            errors[item_id] = error
        else:
            accepted[item_id] = quality | {"term_suggestions": valid_suggestions}
    return ReviewValidation(
        accepted,
        rejected,
        errors,
        tuple(sorted(set(expected_items) - set(candidates) - set(errors))),
        unknown,
    )
