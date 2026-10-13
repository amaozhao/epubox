"""Storage/progress benchmark; run against a five-JSON checkpoint copy.

Example: .venv/bin/python -m benchmarks.storage /path/to/checkpoint-copy --ready
Default checks are read-only. --journal can persist reconstructed request frames
in the copy. No model requests, report writes or EPUB publication are performed.
"""

import argparse
import json
import resource
import time
import tracemalloc
from pathlib import Path

from benchmarks.profiling import RecoveryProfiler, memory_snapshot
from engine.services import state
from engine.services.ready import ReadySession
from engine.services.report import _atomic_progress
from engine.services.store import RunStore
from engine.services.stream import indexed_object


class _NullProfiler:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None


def measure(name, operation, profiler=None):
    before = profiler.snapshot() if profiler is not None else {}
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = children.ru_utime + children.ru_stime
    wall, cpu = time.perf_counter(), time.process_time() + child_cpu
    result = operation()
    wall_seconds = time.perf_counter() - wall
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_seconds = time.process_time() + children.ru_utime + children.ru_stime - cpu
    record = {
        "stage": name,
        "wall_seconds": round(wall_seconds, 4),
        "cpu_seconds": round(cpu_seconds, 4),
        **memory_snapshot(),
    }
    if profiler is not None:
        record["profile_delta"] = _profile_delta(before, profiler.snapshot())
    print(json.dumps(record), flush=True)
    return result


def _profile_delta(before, after):
    values = {}
    for label, current in after.items():
        previous = before.get(label, {})
        delta = {name: value - previous.get(name, 0) for name, value in current.items()}
        if delta.get("calls", 0) or delta.get("errors", 0):
            values[label] = delta
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--ready", action="store_true", help="Also validate and restore the full source plan.")
    parser.add_argument(
        "--journal", action="store_true", help="Verify all body proofs in the copy; may save derived JSON frames."
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--profile",
        action="store_true",
        help="Install selected call counters/timers. Run separately from the uninstrumented baseline.",
    )
    modes.add_argument(
        "--allocations",
        action="store_true",
        help="Enable tracemalloc peak sampling. This is intentionally a separate, higher-overhead mode.",
    )
    options = parser.parse_args()
    mode = "profile" if options.profile else "allocations" if options.allocations else "baseline"
    print(json.dumps({"benchmark_mode": mode}), flush=True)
    root = options.root.resolve(strict=True)
    paths = tuple(root / f"{name}.json" for name in ("origin", "mapping", "plan", "terms", "state"))
    if not all(path.is_file() and not path.is_symlink() for path in paths):
        parser.error("root must be a copy of an existing five-JSON checkpoint")
    # Index without invoking recovery before opening the public store API.
    # A benchmark must not activate a pending transaction, even in a copy.
    manifest, _ = indexed_object(root / "state.json", dict)
    if "transaction" in manifest.get("storage", {}):
        parser.error("checkpoint has a pending transaction; recover it before benchmarking")
    signatures = tuple((path.stat().st_size, path.stat().st_mtime_ns) for path in paths)
    profiler = RecoveryProfiler() if options.profile else None
    if options.allocations:
        tracemalloc.start()
    try:
        with profiler or _NullProfiler():
            measure("cold_storage", lambda: state.header(root), profiler)
            store = RunStore(root)
            if state.exists(root / "prepared.json"):
                for index in range(3):
                    progress, unresolved = measure(f"body_progress_{index}", lambda: _atomic_progress(store), profiler)
                    print(
                        json.dumps(
                            {
                                "accepted_units": progress["accepted_units"],
                                "required_units": progress["required_units"],
                                "pending_items": progress["pending_items"],
                                "unresolved": len(unresolved),
                            }
                        ),
                        flush=True,
                    )
                if options.journal:
                    from engine.services.journal import BodyJournal

                    journal = measure("full_journal_recovery", lambda: BodyJournal(store), profiler)
                    print(json.dumps(journal.progress_snapshot()), flush=True)
                    print(json.dumps({"proof_metrics": dict(journal._proof_metrics)}), flush=True)
                    measure("unchanged_proof_reuse", journal.verify_persisted_proofs, profiler)
                elif options.ready:
                    session = measure("ready_recovery", lambda: ReadySession(store), profiler)
                    print(
                        json.dumps({"members": len(session.index.members), "batches": len(session._prepared_batches)})
                    )
    finally:
        if profiler is not None:
            print(json.dumps({"profile": profiler.snapshot()}), flush=True)
        if options.allocations:
            current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            print(
                json.dumps({"allocations": {"current_traced_bytes": current, "peak_traced_bytes": peak}}),
                flush=True,
            )
    if not options.journal and signatures != tuple((path.stat().st_size, path.stat().st_mtime_ns) for path in paths):
        raise RuntimeError("checkpoint files changed during benchmark")


if __name__ == "__main__":
    main()
