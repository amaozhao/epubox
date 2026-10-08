"""Shared immutable models and canonical JSON helpers."""

from __future__ import annotations

import hashlib
import json
import math
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

DOCUMENT_FORMAT = "epubox-document-3"
UNIT_FORMAT = "epubox-unit-3"
BOOK_FORMAT = "epubox-book-3"
PREPARATION_FORMAT = "epubox-preparation-1"
TERM_PLAN_FORMAT = "epubox-term-plan-1"
EXTRACTION_RECORD_FORMAT = "epubox-extraction-record-1"
CANDIDATES_FORMAT = "epubox-candidates-1"
FREEZE_FORMAT = "epubox-freeze-1"
GLOSSARY_FORMAT = "epubox-glossary-1"
REQUEST_FORMAT = "epubox-request-2"
REVIEW_PROTOCOL = "epubox-review-2"
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_JSON_DEPTH = 64


class UnsupportedFormatError(ValueError):
    """Raised when a persisted object or protocol is from another contract."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ItemStatus(StrEnum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    CANDIDATE = "candidate"
    LOCAL_VALID = "local_valid"
    REVIEWED = "reviewed"
    RETRY_WAIT = "retry_wait"
    NEEDS_ATTENTION = "needs_attention"
    BLOCKED_DEPENDENCY = "blocked_dependency"


def _hash_payload(value: BaseModel | dict[str, Any], field: str) -> str:
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
    data.pop(field, None)
    return canonical_hash(data)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {key: _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(child) for child in value]
    return value


def _validate_json(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ValueError(f"JSON nesting exceeds {MAX_JSON_DEPTH}")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            _validate_json(child, depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate_json(child, depth + 1)
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise TypeError(f"not a JSON value: {type(value).__name__}")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON numbers are forbidden")


def canonical_json_bytes(value: Any, *, max_bytes: int | None = MAX_JSON_BYTES) -> bytes:
    value = _json_value(value)
    _validate_json(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if max_bytes is not None and len(encoded) > max_bytes:
        raise ValueError(f"JSON exceeds {max_bytes} bytes")
    return encoded


def canonical_hash(value: Any) -> str:
    value = _json_value(value)
    _validate_json(value)
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    for part in encoder.iterencode(value):
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()


def strict_json_loads(data: str | bytes, *, max_bytes: int | None = MAX_JSON_BYTES) -> JsonValue:
    raw = data if isinstance(data, bytes) else data.encode()
    if max_bytes is not None and len(raw) > max_bytes:
        raise ValueError(f"JSON exceeds {max_bytes} bytes")
    value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    _validate_json(value)
    return value


def parse_contract[ModelT: BaseModel](
    data: str | bytes | dict[str, Any],
    model: type[ModelT],
    expected_format: str,
    *,
    max_bytes: int | None = MAX_JSON_BYTES,
) -> ModelT:
    value = strict_json_loads(data, max_bytes=max_bytes) if isinstance(data, (str, bytes)) else data
    if not isinstance(value, dict):
        raise TypeError("contract root must be a JSON object")
    actual = value.get("format")
    if actual != expected_format:
        raise UnsupportedFormatError(f"unsupported format {actual!r}; expected {expected_format!r}")
    return model.model_validate(value)


def require_protocol(data: dict[str, Any], expected_protocol: str) -> None:
    actual = data.get("protocol")
    if actual != expected_protocol:
        raise UnsupportedFormatError(f"unsupported protocol {actual!r}; expected {expected_protocol!r}")
