"""Frozen, resumable chapter-coherence windows for the v2.5 executor."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from engine.schemas.contracts import DocumentPlan, JsonValue, UnitRecord, canonical_hash, strict_json_loads
from engine.services.atomic_store import CorruptRecord
from engine.services.store import RunStore

CHECK_FORMAT = "epubox-check-3"
LIMITS_FORMAT = "epubox-run-limits-1"


def prepare_document_check(
    store: RunStore,
    document: DocumentPlan,
    records: Mapping[str, UnitRecord],
) -> dict[str, Any]:
    """Create or refresh one document snapshot without consuming a request."""
    path = store._path("checks", document.document_id)
    if path.exists():
        check = _read(path)
    else:
        windows = _windows(document, records)
        check = {
            "format": CHECK_FORMAT,
            "document_id": document.document_id,
            "windows": list(windows),
            "http_limit": 6 * len(windows),
            "candidate_versions": {},
            "dependency_ids": [],
            "status": "pending" if windows else "valid",
            "checks": {},
            "issues": [],
            "coherence_revision_rounds": 0,
            "record_hash": None,
        }
    participants = {unit_id for window in check["windows"] for unit_id in _string_list(window, "unit_ids")}
    missing = sorted(
        unit_id
        for unit_id in participants
        if unit_id not in records or records[unit_id].accepted_revision != records[unit_id].revision
    )
    if missing:
        check.update(status="blocked_dependency", dependency_ids=missing)
    else:
        vector = {unit_id: records[unit_id].revision for unit_id in sorted(participants)}
        if check["candidate_versions"] != vector:
            check.update(candidate_versions=vector, checks={}, issues=[], status="pending")
        else:
            issues = check.get("issues", [])
            blocking = isinstance(issues, list) and any(
                isinstance(issue, dict) and issue.get("severity") in {"major", "critical"} for issue in issues
            )
            completed = check.get("checks", {})
            check["status"] = (
                "needs_attention"
                if blocking
                else "valid"
                if isinstance(completed, dict) and len(completed) == len(check["windows"])
                else "pending"
            )
        check.update(
            dependency_ids=[],
        )
        if not check["windows"]:
            check["status"] = "valid"
    return save_document_check(store, check)


def pending_windows(check: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    completed = check.get("checks", {})
    if not isinstance(completed, dict) or check.get("status") != "pending":
        return ()
    return tuple(window for window in check["windows"] if window["item_id"] not in completed)


def window_payload(window: Mapping[str, Any], records: Mapping[str, UnitRecord]) -> dict[str, Any]:
    unit_ids = _string_list(window, "unit_ids")
    segment_ids = window.get("segment_item_ids")
    if isinstance(segment_ids, list) and len(unit_ids) == 1:
        record = records[unit_ids[0]]
        target = [
            _snippet(record.items[item_id].target_projection or "", tail=index == 0)
            for index, item_id in enumerate(segment_ids)
            if isinstance(item_id, str) and item_id in record.items
        ]
    else:
        target = [
            _snippet(records[unit_id].candidate or "", tail=index == 0) for index, unit_id in enumerate(unit_ids)
        ]
    return {
        "item_id": str(window["item_id"]),
        "unit_ids": unit_ids,
        "source": window["source"],
        "target": target,
    }


def save_window_result(
    store: RunStore,
    check: Mapping[str, Any],
    item_id: str,
    issues: Sequence[Mapping[str, JsonValue]],
) -> dict[str, Any]:
    updated = dict(check)
    checks = dict(updated["checks"])
    checks[item_id] = {"issues": [dict(issue) for issue in issues]}
    all_issues = [issue for value in checks.values() for issue in value["issues"]]
    blocking = any(issue.get("severity") in {"major", "critical"} for issue in all_issues)
    updated.update(
        checks=checks,
        issues=all_issues,
        status="needs_attention" if blocking else "valid" if len(checks) == len(updated["windows"]) else "pending",
    )
    return save_document_check(store, updated)


def save_document_check(store: RunStore, check: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(check)
    value["record_hash"] = None
    value["record_hash"] = canonical_hash({key: item for key, item in value.items() if key != "record_hash"})
    store._atomic_write(store._path("checks", str(value["document_id"])), value)
    return value


def load_budget_overrides(store: RunStore) -> dict[str, Any]:
    path = store._path("checks", "run-limits")
    if not path.exists():
        return {
            "format": LIMITS_FORMAT,
            "add_run_http": 0,
            "add_unit_http": {},
            "add_check_http": {},
            "authorizations": {},
            "record_hash": None,
        }
    value = strict_json_loads(path.read_bytes())
    if not isinstance(value, dict) or value.get("format") != LIMITS_FORMAT:
        raise CorruptRecord("invalid run limit override")
    expected = canonical_hash({key: item for key, item in value.items() if key != "record_hash"})
    if value.get("record_hash") != expected:
        raise CorruptRecord("run limit override hash mismatch")
    value.setdefault("authorizations", {})
    if (
        type(value.get("add_run_http")) is not int
        or not isinstance(value.get("add_unit_http"), dict)
        or not isinstance(value.get("add_check_http"), dict)
        or not isinstance(value["authorizations"], dict)
    ):
        raise CorruptRecord("invalid run limit override fields")
    return value


def add_http_budget(
    store: RunStore,
    *,
    authorization_id: str,
    action_context_hash: str = "",
    add_run_http: int = 0,
    add_unit_http: Mapping[str, int] | None = None,
    add_check_http: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    add_unit_http = add_unit_http or {}
    add_check_http = add_check_http or {}
    if any(
        type(value) is not int or value < 0
        for value in (add_run_http, *add_unit_http.values(), *add_check_http.values())
    ):
        raise ValueError("HTTP budget additions must be non-negative integers")
    if not authorization_id:
        raise ValueError("authorization_id cannot be empty")
    if not isinstance(action_context_hash, str):
        raise TypeError("action_context_hash must be a string")
    if any(not isinstance(key, str) or not key for key in (*add_unit_http, *add_check_http)):
        raise ValueError("HTTP budget identities must be non-empty strings")
    action_hash = canonical_hash(
        {
            "add_run_http": add_run_http,
            "add_unit_http": dict(add_unit_http),
            "add_check_http": dict(add_check_http),
            "action_context_hash": action_context_hash,
        }
    )
    with store.lock():
        value = load_budget_overrides(store)
        authorizations = dict(value["authorizations"])
        existing = authorizations.get(authorization_id)
        if existing is not None:
            if existing != action_hash:
                raise ValueError("authorization_id was already used for a different HTTP budget action")
            return value
        units = dict(value["add_unit_http"])
        checks = dict(value["add_check_http"])
        for unit_id, amount in add_unit_http.items():
            units[unit_id] = int(units.get(unit_id, 0)) + amount
        for document_id, amount in add_check_http.items():
            checks[document_id] = int(checks.get(document_id, 0)) + amount
        authorizations[authorization_id] = action_hash
        updated = {
            "format": LIMITS_FORMAT,
            "add_run_http": int(value["add_run_http"]) + add_run_http,
            "add_unit_http": units,
            "add_check_http": checks,
            "authorizations": authorizations,
        }
        updated["record_hash"] = canonical_hash(updated)
        store._atomic_write(store._path("checks", "run-limits"), updated)
        return updated


def retry_document_check(store: RunStore, document_id: str) -> dict[str, Any]:
    path = store._path("checks", document_id)
    if not path.exists():
        raise FileNotFoundError(path)
    check = _read(path)
    if check.get("status") != "needs_attention":
        return check
    checks = {
        item_id: result
        for item_id, result in check["checks"].items()
        if not any(issue.get("severity") in {"major", "critical"} for issue in result["issues"])
    }
    check.update(checks=checks, issues=[], status="pending")
    return save_document_check(store, check)


def _read(path) -> dict[str, Any]:
    value = strict_json_loads(path.read_bytes())
    if not isinstance(value, dict) or value.get("format") != CHECK_FORMAT:
        raise CorruptRecord("invalid v2.5 coherence record")
    expected = canonical_hash({key: item for key, item in value.items() if key != "record_hash"})
    if value.get("record_hash") != expected:
        raise CorruptRecord("coherence record hash mismatch")
    value.setdefault("coherence_revision_rounds", 0)
    return value


def _windows(document: DocumentPlan, records: Mapping[str, UnitRecord]) -> tuple[dict[str, JsonValue], ...]:
    windows: list[dict[str, JsonValue]] = []
    seen: set[str] = set()
    for boundary in document.boundaries:
        edges = boundary.get("relation_edges")
        if not isinstance(edges, list):
            continue
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            left, right, kind = edge.get("from_unit_id"), edge.get("to_unit_id"), edge.get("kind")
            if not all(isinstance(value, str) and value for value in (left, right, kind)):
                continue
            item_id = "cw-" + canonical_hash([document.document_id, left, right, kind])[:24]
            if item_id in seen:
                continue
            seen.add(item_id)
            units = {unit.unit_id: unit for unit in document.units}
            if left not in units or right not in units:
                continue
            windows.append(
                {
                    "item_id": item_id,
                    "unit_ids": [left, right],
                    "source": [
                        _snippet(units[left].source_projection, tail=True),
                        _snippet(units[right].source_projection),
                    ],
                    "kind": kind,
                }
            )
    for unit in document.units:
        record = records.get(unit.unit_id)
        if record is None or record.cut_plan is None:
            continue
        for index, (left, right) in enumerate(zip(record.cut_plan.segments, record.cut_plan.segments[1:])):
            windows.append(
                {
                    "item_id": "cw-" + canonical_hash([document.document_id, unit.unit_id, "seam", index])[:24],
                    "unit_ids": [unit.unit_id],
                    "source": [_snippet(left.source_projection, tail=True), _snippet(right.source_projection)],
                    "kind": "segment_seam",
                    "segment_item_ids": [left.item_id, right.item_id],
                }
            )
    return tuple(windows)


def _snippet(value: str, *, tail: bool = False, limit: int = 800) -> str:
    return value[-limit:] if tail else value[:limit]


def _string_list(value: Mapping[str, Any], key: str) -> list[str]:
    raw = value.get(key)
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise CorruptRecord(f"coherence {key} must be a string list")
    return raw


__all__ = [
    "add_http_budget",
    "load_budget_overrides",
    "pending_windows",
    "prepare_document_check",
    "retry_document_check",
    "save_window_result",
    "window_payload",
]
