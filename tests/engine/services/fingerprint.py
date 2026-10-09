from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from engine.services import shards, state


def compact(tmp_path: Path) -> Path:
    source = tmp_path / "book.epub"
    source.write_bytes(b"epub")
    root = tmp_path / "book"
    state.initialize(root, source, hashlib.sha256(b"epub").hexdigest(), "run-1")
    return root


def test_dependency_fingerprint_ignores_runtime_records(tmp_path: Path) -> None:
    root = compact(tmp_path)
    state.write(root / "documents" / "d.json", b"document")
    before = state.dependency_fingerprint(root, prefixes=("documents/",))

    state.write(root / "report.json", b"runtime")

    assert state.dependency_fingerprint(root, prefixes=("documents/",)) == before
    state.write(root / "documents" / "d.json", b"changed")
    assert state.dependency_fingerprint(root, prefixes=("documents/",)) != before


def test_dependency_fingerprint_reads_pending_batch_dependencies(tmp_path: Path) -> None:
    root = compact(tmp_path)
    state.write(root / "documents" / "d.json", b"document")
    before = state.dependency_fingerprint(root, prefixes=("documents/",))

    with state.batch(root):
        state.write(root / "report.json", b"runtime")
        assert state.dependency_fingerprint(root, prefixes=("documents/",)) == before
        state.write(root / "documents" / "d.json", b"changed")
        assert state.dependency_fingerprint(root, prefixes=("documents/",)) != before


def test_dependency_fingerprint_reloads_and_rejects_physical_tampering(tmp_path: Path) -> None:
    root = compact(tmp_path)
    path = root / "documents" / "d.json"
    state.write(path, b"document")
    before = state.dependency_fingerprint(root, prefixes=("documents/",))
    physical = state.artifact(path)
    value = json.loads(physical.read_text())
    record = value["records"]["documents/d.json"]
    stamp = record["stamp"]
    record["data"] = "tampered"
    assert len(record["data"]) == len("document")
    assert record["stamp"] == stamp
    physical.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    with pytest.raises(state.StateError, match="does not match state.json"):
        state.dependency_fingerprint(root, prefixes=("documents/",))
    assert before[0][:2] == ("documents/d.json", stamp)


def test_dependency_fingerprint_uses_logical_records_after_rebalancing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(shards, "MAX_BYTES", 2_500)
    root = compact(tmp_path)
    payloads = {f"documents/d{index}.json": (str(index) * 240).encode() for index in range(12)}
    with state.batch(root):
        for key, payload in payloads.items():
            state.write(root / key, payload)

    fingerprint = state.dependency_fingerprint(root, prefixes=("documents/",))

    assert {key for key, _stamp, _data in fingerprint} == set(payloads)
    assert any(owner != "origin" for key, owner in state._owners[root].items() if key in payloads)


def test_dependency_fingerprint_survives_cache_reload_without_content_changes(tmp_path: Path) -> None:
    root = compact(tmp_path)
    state.write(root / "documents/d.json", b"document")
    before = state.dependency_fingerprint(root, prefixes=("documents/",))

    state._cache.clear()
    after = state.dependency_fingerprint(root, prefixes=("documents/",))

    assert after == before


def test_dependency_fingerprint_ignores_external_runtime_updates(tmp_path: Path) -> None:
    root = compact(tmp_path)
    state.write(root / "documents/d.json", b"document")
    state.write(root / "report.json", b"{}")
    before = state.dependency_fingerprint(root, prefixes=("documents/",))
    for counter in range(2):
        path = root / "state.json"
        value = json.loads(path.read_text())
        value["records"]["report.json"]["data"] = json.dumps({"counter": counter})
        path.write_text(json.dumps(value))
        assert state.dependency_fingerprint(root, prefixes=("documents/",)) == before
