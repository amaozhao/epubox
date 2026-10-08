"""Canonical hashes cover whole books without a single-record encoding cap."""

import hashlib
import json

import pytest

from engine.schemas.base import (
    MAX_JSON_BYTES,
    FrozenModel,
    canonical_hash,
    canonical_json_bytes,
    strict_json_loads,
)


class Example(FrozenModel):
    title: str
    values: tuple[int, float, bool, None]


@pytest.mark.parametrize(
    "value",
    (
        {"z": "中文 🧠", "a": [1, -0.0, 1.5, True, False, None, {"quote": '\\"\n'}]},
        ("b", "a", {}),
        Example(title="书名", values=(3, 2.5, True, None)),
    ),
)
def test_streamed_hash_keeps_existing_canonical_identity(value):
    assert canonical_hash(value) == hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def test_book_aggregate_hash_exceeds_record_cap_without_using_capped_encoder(monkeypatch):
    # Each individual document fits; their aggregate is larger than 32 MiB.
    documents = [{"source": "x" * (MAX_JSON_BYTES // 8)}] * 9
    expected = hashlib.sha256(
        json.dumps(
            documents,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="JSON exceeds"):
        canonical_json_bytes(documents)
    monkeypatch.setattr(
        "engine.schemas.base.canonical_json_bytes",
        lambda *args, **kwargs: pytest.fail("hashing must not use the capped whole-record encoder"),
    )
    assert canonical_hash(documents) == expected


@pytest.mark.parametrize("value", ({1: "invalid key"}, {"nan": float("nan")}))
def test_hash_still_rejects_invalid_json(value):
    with pytest.raises((TypeError, ValueError)):
        canonical_hash(value)


def test_untrusted_json_parsing_still_checks_size_and_duplicate_keys():
    with pytest.raises(ValueError, match="JSON exceeds"):
        strict_json_loads('"1234"', max_bytes=3)
    with pytest.raises(ValueError, match="duplicate JSON key"):
        strict_json_loads('{"key":1,"key":2}')
