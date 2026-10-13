"""Opt-in instrumentation for the cold-recovery benchmark.

The production path never imports this module.  Baseline measurements therefore
have no wrappers, counters, or allocation tracing installed.
"""

from __future__ import annotations

import importlib
import os
import resource
import subprocess
import sys
import time
from collections import defaultdict
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from functools import wraps
from typing import Any, Self
from unittest.mock import patch


@dataclass
class CallMetric:
    calls: int = 0
    errors: int = 0
    wall_seconds: float = 0.0
    cpu_seconds: float = 0.0
    input_bytes: int = 0


def memory_snapshot() -> dict[str, int | str | None]:
    """Return comparable RSS data and a best-effort native private footprint."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    child_peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    if sys.platform != "darwin":
        peak *= 1024
        child_peak *= 1024
    current: int | None = None
    private: int | None = None
    source = "unavailable"
    if sys.platform == "linux":
        try:
            with open("/proc/self/statm", encoding="ascii") as status:
                resident_pages = int(status.read().split()[1])
            current = resident_pages * os.sysconf("SC_PAGE_SIZE")
            source = "procfs; private footprint unavailable"
        except (IndexError, OSError, ValueError):
            source = "procfs unavailable; private footprint unavailable"
    elif sys.platform == "darwin":
        try:
            output = subprocess.run(
                ("ps", "-o", "rss=", "-o", "footprint=", "-p", str(os.getpid())),
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.split()
            if len(output) == 2:
                current, private = (int(value) * 1024 for value in output)
                source = "macOS ps rss/footprint"
            else:
                source = "macOS ps footprint unavailable"
        except (OSError, subprocess.SubprocessError, ValueError):
            source = "macOS ps footprint unavailable"
    return {
        "current_rss_bytes": current,
        "peak_rss_bytes": peak,
        "peak_child_rss_bytes": child_peak or None,
        "private_footprint_bytes": private,
        "memory_source": source,
    }


class RecoveryProfiler(AbstractContextManager["RecoveryProfiler"]):
    """Patch selected recovery call sites and aggregate low-volume metrics."""

    _CALLS = (
        ("json.index", "engine.services.stream", "indexed_object"),
        ("json.index", "engine.services.shards", "indexed_object"),
        ("json.record_decode", "engine.services.stream", "RecordRef.read"),
        ("pydantic.contract_parse", "engine.services.ready", "parse_contract"),
        ("pydantic.contract_parse", "engine.services.parts", "parse_contract"),
        ("packing.pack_members", "engine.services.journal", "pack_members"),
        ("packing.pack_members", "engine.services.partition", "pack_members"),
        ("packing.pack_members", "engine.services.proofs", "pack_members"),
        ("tokenizer.count", "engine.item.budget", "_count"),
        ("proof.translation", "engine.services.journal", "BodyJournal._translation_proof"),
        ("proof.review", "engine.services.journal", "BodyJournal._require_review_proof"),
        ("proof.part_composite", "engine.services.parts", "verify_composite"),
    )
    _HASH_MODULES = (
        "engine.item.members",
        "engine.schemas.bridge",
        "engine.services.journal",
        "engine.services.parts",
        "engine.services.partition",
        "engine.services.proofs",
        "engine.services.ready",
    )

    def __init__(self) -> None:
        self.metrics: dict[str, CallMetric] = defaultdict(CallMetric)
        self._patches: list[Any] = []

    def __enter__(self) -> Self:
        for label, module_name, attribute in self._CALLS:
            self._install(label, module_name, attribute)
        for module_name in self._HASH_MODULES:
            module = importlib.import_module(module_name)
            if hasattr(module, "canonical_hash"):
                self._install("hash.canonical", module_name, "canonical_hash")
        self._install_cache_metrics("engine.services.working", "RecordView")
        self._install_cache_metrics("engine.services.working", "BudgetCache")
        for active in self._patches:
            active.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        for active in reversed(self._patches):
            active.stop()

    def _install(self, label: str, module_name: str, attribute: str) -> None:
        module = importlib.import_module(module_name)
        owner: Any = module
        parts = attribute.split(".")
        for part in parts[:-1]:
            owner = getattr(owner, part)
        name = parts[-1]
        original = getattr(owner, name)
        metric = self.metrics[label]

        @wraps(original)
        def tracked(*args, **kwargs):
            size = _input_bytes(label, args)
            wall = time.perf_counter()
            cpu = time.process_time()
            metric.calls += 1
            metric.input_bytes += size
            try:
                return original(*args, **kwargs)
            except BaseException:
                metric.errors += 1
                raise
            finally:
                metric.wall_seconds += time.perf_counter() - wall
                metric.cpu_seconds += time.process_time() - cpu

        self._patches.append(patch.object(owner, name, tracked))

    def _install_cache_metrics(self, module_name: str, class_name: str) -> None:
        owner = getattr(importlib.import_module(module_name), class_name)
        original_get = owner.__getitem__
        profiler = self

        @wraps(original_get)
        def tracked_get(cache, key):
            suffix = "hit" if key in cache._cache else "miss"
            profiler.metrics[f"cache.{class_name}.{suffix}"].calls += 1
            return original_get(cache, key)

        self._patches.append(patch.object(owner, "__getitem__", tracked_get))
        if class_name != "BudgetCache":
            return
        original_set = owner.__setitem__

        @wraps(original_set)
        def tracked_set(cache, key, value):
            before = set(cache._cache)
            result = original_set(cache, key, value)
            evicted = before - set(cache._cache) - {key}
            profiler.metrics["cache.BudgetCache.eviction"].calls += len(evicted)
            return result

        self._patches.append(patch.object(owner, "__setitem__", tracked_set))

    def snapshot(self) -> dict[str, dict[str, int | float]]:
        return {
            label: {
                **asdict(metric),
                "wall_seconds": round(metric.wall_seconds, 6),
                "cpu_seconds": round(metric.cpu_seconds, 6),
            }
            for label, metric in sorted(self.metrics.items())
            if metric.calls
        }


def _input_bytes(label: str, args: tuple[Any, ...]) -> int:
    if label == "json.record_decode" and args:
        ref = args[0]
        return max(0, int(ref.end) - int(ref.start))
    if label == "json.index" and args:
        try:
            return int(args[0].stat().st_size)
        except (AttributeError, OSError):
            return 0
    if label == "tokenizer.count" and args and isinstance(args[0], str):
        return len(args[0].encode("utf-8"))
    return 0


__all__ = ["RecoveryProfiler", "memory_snapshot"]
