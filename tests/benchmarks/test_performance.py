from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from benchmarks.profiling import RecoveryProfiler, memory_snapshot
from benchmarks.storage import _profile_delta
from engine.services import ready, state
from engine.services.working import BudgetCache, RecordView


class _Tokenizer:
    def encode(self, value: str) -> list[int]:
        return list(value.encode("utf-8"))


def test_profiler_counts_selected_calls_and_restores_wrappers() -> None:
    from engine.item import budget

    original = budget._count
    profiler = RecoveryProfiler()
    with profiler:
        assert budget._count("é", _Tokenizer()) == 2
        ready.canonical_hash({"value": 1})
        view = RecordView(("one",), lambda _key: ("value", 5), max_bytes=10)
        assert view["one"] == "value"
        assert view["one"] == "value"
        cache: BudgetCache[str, str] = BudgetCache(200)
        cache["one"] = "value"
        assert cache["one"] == "value"

    metrics = profiler.snapshot()
    assert metrics["tokenizer.count"]["calls"] == 1
    assert metrics["tokenizer.count"]["input_bytes"] == 2
    assert metrics["hash.canonical"]["calls"] == 1
    assert metrics["cache.RecordView.miss"]["calls"] == 1
    assert metrics["cache.RecordView.hit"]["calls"] == 1
    assert metrics["cache.BudgetCache.hit"]["calls"] == 1
    assert budget._count is original


def test_memory_snapshot_has_explicit_private_footprint_status() -> None:
    snapshot = memory_snapshot()
    assert isinstance(snapshot["peak_rss_bytes"], int)
    assert snapshot["peak_child_rss_bytes"] is None or isinstance(snapshot["peak_child_rss_bytes"], int)
    assert snapshot["current_rss_bytes"] is None or isinstance(snapshot["current_rss_bytes"], int)
    assert snapshot["private_footprint_bytes"] is None or isinstance(snapshot["private_footprint_bytes"], int)
    assert isinstance(snapshot["memory_source"], str)
    assert snapshot["memory_source"]


def test_profile_delta_separates_stage_work() -> None:
    before = {"hash.canonical": {"calls": 2, "errors": 0, "wall_seconds": 0.1, "cpu_seconds": 0.1}}
    after = {"hash.canonical": {"calls": 5, "errors": 0, "wall_seconds": 0.4, "cpu_seconds": 0.3}}
    assert _profile_delta(before, after)["hash.canonical"]["calls"] == 3


def test_storage_benchmark_direct_help_lists_separate_modes() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        (sys.executable, "-m", "benchmarks.storage", "--help"),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "--profile" in result.stdout
    assert "--allocations" in result.stdout
    assert "[--profile | --allocations]" in result.stdout


def test_storage_benchmark_modes_run_read_only_on_checkpoint(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"epub")
    checkpoint = tmp_path / "checkpoint"
    state.initialize(checkpoint, source, hashlib.sha256(b"epub").hexdigest(), "benchmark")
    files = tuple(checkpoint / f"{name}.json" for name in ("origin", "mapping", "plan", "terms", "state"))
    before = tuple((path.read_bytes(), path.stat().st_mtime_ns) for path in files)
    root = Path(__file__).resolve().parents[2]

    for arguments, expected in (((), "baseline"), (("--profile",), "profile"), (("--allocations",), "allocations")):
        result = subprocess.run(
            (sys.executable, "-m", "benchmarks.storage", str(checkpoint), *arguments),
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        records = tuple(json.loads(line) for line in result.stdout.splitlines())
        assert records[0] == {"benchmark_mode": expected}
        assert any(record.get("stage") == "cold_storage" for record in records)
        assert any("profile" in record for record in records) is (expected == "profile")
        assert any("allocations" in record for record in records) is (expected == "allocations")

    assert before == tuple((path.read_bytes(), path.stat().st_mtime_ns) for path in files)
