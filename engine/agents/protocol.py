"""Strict wire validation for translated text and chapter coherence."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

MAX_RESPONSE_BYTES = 1_000_000
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 100_000
MAX_ITEMS = 256
CHECK_NAMES = frozenset({"accuracy", "fluency", "terminology", "bindings", "script"})
CHECK_VALUES = frozenset({"pass", "fail", "uncertain", "not_applicable"})
ISSUE_SEVERITIES = frozenset({"minor", "major", "critical"})


class ProtocolError(ValueError):
    """The whole response cannot be trusted or associated with its request."""


@dataclass(frozen=True)
class BatchValidation:
    accepted: dict[str, dict[str, Any]]
    errors: dict[str, str]
    missing: tuple[str, ...]
    unknown: tuple[str, ...]


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ProtocolError(f"non-finite JSON number: {value}")


def _check_shape(root: Any) -> None:
    nodes = 0
    stack = [(root, 1)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > MAX_JSON_DEPTH:
            raise ProtocolError(f"JSON depth exceeds {MAX_JSON_DEPTH}")
        if nodes > MAX_JSON_NODES:
            raise ProtocolError(f"JSON node count exceeds {MAX_JSON_NODES}")
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)


def strict_loads(raw: str | bytes) -> Any:
    if isinstance(raw, bytes):
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProtocolError("response exceeds byte limit")
        try:
            raw = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ProtocolError("response is not valid UTF-8") from exc
    elif not isinstance(raw, str):
        raise ProtocolError("response must be text or UTF-8 bytes")
    if len(raw.encode("utf-8", errors="surrogatepass")) > MAX_RESPONSE_BYTES:
        raise ProtocolError("response exceeds byte limit")
    try:
        value = json.loads(raw, object_pairs_hook=_object_no_duplicates, parse_constant=_reject_constant)
    except ProtocolError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ProtocolError(f"invalid complete JSON: {exc}") from exc
    _check_shape(value)
    return value


def _validate_root(value: Any, protocol: str, request_id: str) -> list[Any]:
    if not isinstance(value, dict):
        raise ProtocolError("response root must be an object")
    if set(value) != {"protocol", "request_id", "items"}:
        raise ProtocolError("response root must contain exactly protocol, request_id, and items")
    if value.get("protocol") != protocol:
        raise ProtocolError("protocol mismatch")
    if value.get("request_id") != request_id:
        raise ProtocolError("request_id mismatch")
    items = value.get("items")
    if not isinstance(items, list):
        raise ProtocolError("items must be an array")
    if len(items) > MAX_ITEMS:
        raise ProtocolError(f"items exceeds {MAX_ITEMS}")
    return items


def _valid_xml_text(value: str) -> bool:
    for char in value:
        codepoint = ord(char)
        if codepoint in (0x9, 0xA, 0xD) or 0x20 <= codepoint <= 0xD7FF:
            continue
        if 0xE000 <= codepoint <= 0xFFFD or 0x10000 <= codepoint <= 0x10FFFF:
            continue
        return False
    return True


def _collect_items(
    items: list[Any], expected_ids: set[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, str], tuple[str, ...], Counter[str]]:
    item_ids: list[str] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("item_id"), str):
            item_ids.append(item["item_id"])
    counts: Counter[str] = Counter(item_ids)
    accepted: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    unknown: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors[f"@{index}"] = "item must be an object"
            continue
        item_id = item.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            errors[f"@{index}"] = "item_id must be a non-empty string"
            continue
        if item_id not in expected_ids:
            unknown.append(item_id)
            continue
        if counts[item_id] > 1:
            errors[item_id] = "duplicate item_id"
            continue
        accepted[item_id] = item
    return accepted, errors, tuple(dict.fromkeys(unknown)), counts


def validate_translation_response(raw: str | bytes, request_id: str, expected_item_ids: set[str]) -> BatchValidation:
    items = _validate_root(strict_loads(raw), "epubox-text-1", request_id)
    candidates, errors, unknown, _ = _collect_items(items, expected_item_ids)
    accepted: dict[str, dict[str, Any]] = {}
    for item_id, item in candidates.items():
        if set(item) - {"item_id", "target"}:
            errors[item_id] = "translation item must contain exactly item_id and target"
            continue
        target = item.get("target")
        if not isinstance(target, str) or not target:
            errors[item_id] = "target must be a non-empty string"
        elif not _valid_xml_text(target):
            errors[item_id] = "target contains invalid XML characters"
        else:
            accepted[item_id] = {"item_id": item_id, "target": target}
    missing = tuple(sorted(expected_item_ids - set(candidates) - set(errors)))
    return BatchValidation(accepted, errors, missing, unknown)


def _validate_issues(issues: Any) -> str | None:
    if not isinstance(issues, list):
        return "issues must be an array"
    for issue in issues:
        if not isinstance(issue, dict):
            return "each issue must be an object"
        if not all(isinstance(issue.get(key), str) and issue[key] for key in ("code", "message")):
            return "issue code and message must be non-empty strings"
        severity = issue.get("severity")
        if not isinstance(severity, str) or severity not in ISSUE_SEVERITIES:
            return "invalid issue severity"
    return None


def _review_error(item: dict[str, Any], expected: Mapping[str, Any]) -> str | None:
    base_revision = item.get("base_revision")
    if type(base_revision) is not int or base_revision != expected.get("base_revision"):
        return "base_revision mismatch"
    decision = item.get("decision")
    if not isinstance(decision, str) or decision not in {"no_change", "replace", "needs_attention"}:
        return "invalid decision"
    target = item.get("target")
    if decision == "replace":
        if not isinstance(target, str) or not target or not _valid_xml_text(target):
            return "replace requires a complete valid target"
    elif "target" in item:
        return f"target is forbidden for {decision}"
    required_keys = {"item_id", "base_revision", "decision", "checks", "issues"}
    if set(item) != required_keys | ({"target"} if decision == "replace" else set()):
        return "review item has missing or unknown fields"

    checks = item.get("checks")
    if not isinstance(checks, dict) or set(checks) != CHECK_NAMES:
        return "checks must contain exactly accuracy, fluency, terminology, bindings, script"
    if any(not isinstance(value, str) or value not in CHECK_VALUES for value in checks.values()):
        return "invalid check value"
    for name in ("accuracy",):
        if checks[name] == "not_applicable":
            return f"{name} cannot be not_applicable"
    for name in ("fluency", "script", "terminology", "bindings"):
        if expected.get(f"{name}_applicable", True) and checks[name] == "not_applicable":
            return f"{name} is applicable"

    issue_error = _validate_issues(item.get("issues"))
    if issue_error:
        return issue_error
    if decision == "no_change":
        blocking = [name for name, value in checks.items() if value in {"fail", "uncertain"}]
        blocking.extend(issue["code"] for issue in item["issues"] if issue["severity"] in {"major", "critical"})
        if blocking:
            return f"blocking check or issue: {', '.join(blocking)}"
    return None


def review_applicability(member: Any, index: Any, wire: Mapping[str, Any]) -> dict[str, bool]:
    terms = wire.get("terms")
    if not isinstance(terms, list) or any(not isinstance(term, Mapping) for term in terms):
        raise ProtocolError("review terms are invalid")
    supplied = wire.get("applicability")
    expected = {
        "terminology": any(term.get("role") == "target" for term in terms),
        "bindings": bool(member.registry),
    }
    if supplied != expected:
        raise ProtocolError("review applicability differs from the trusted member")
    source, target = wire.get("source"), wire.get("target")
    resource = index.documents[member.document_id].resource.path
    filename = (
        member.kind == "head_title"
        and member.channel == "metadata"
        and not member.registry
        and isinstance(source, str)
        and source == member.source_projection == target == PurePosixPath(resource).name
    )
    return {
        "terminology_applicable": expected["terminology"],
        "bindings_applicable": expected["bindings"],
        "fluency_applicable": not filename,
        "script_applicable": not filename,
    }


def validate_coherence_response(
    raw: str | bytes,
    request_id: str,
    expected_items: Mapping[str, set[str] | tuple[str, ...] | list[str]],
) -> BatchValidation:
    items = _validate_root(strict_loads(raw), "epubox-coherence-1", request_id)
    candidates, errors, unknown, _ = _collect_items(items, set(expected_items))
    accepted: dict[str, dict[str, Any]] = {}
    for item_id, item in candidates.items():
        if set(item) != {"item_id", "unit_ids", "issues"}:
            errors[item_id] = "coherence item must contain exactly item_id, unit_ids, and issues"
            continue
        unit_ids = item.get("unit_ids")
        if (
            not isinstance(unit_ids, list)
            or any(not isinstance(unit_id, str) for unit_id in unit_ids)
            or not set(unit_ids).issubset(set(expected_items[item_id]))
        ):
            errors[item_id] = "coherence unit_ids are outside the request manifest"
            continue
        issue_error = _validate_issues(item.get("issues"))
        if issue_error:
            errors[item_id] = issue_error
        elif item["issues"] and not unit_ids:
            errors[item_id] = "coherence issues require at least one affected unit from the window"
        else:
            accepted[item_id] = item
    missing = tuple(sorted(set(expected_items) - set(candidates) - set(errors)))
    return BatchValidation(accepted, errors, missing, unknown)
