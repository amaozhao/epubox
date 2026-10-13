from __future__ import annotations

import multiprocessing
import os
import threading
import time

import pytest

import engine.services.preflight as preflight_module
from engine.item.atoms import extract_resource
from engine.schemas.budget import BudgetLimits
from engine.services.parallel import ordered_map


def _process_identity(value: int) -> tuple[int, int]:
    return os.getpid(), value


def test_ordered_map_overlaps_workers_and_preserves_input_order() -> None:
    barrier = threading.Barrier(3)
    names: set[str] = set()

    def work(value: int) -> int:
        names.add(threading.current_thread().name)
        barrier.wait(timeout=2)
        time.sleep((2 - value) * 0.01)
        return value

    assert ordered_map(work, range(3), workers=3) == (0, 1, 2)
    assert len(names) == 3


def test_ordered_map_limits_eager_input_to_worker_count() -> None:
    release = threading.Event()
    pulled = 0

    def values():
        nonlocal pulled
        for value in range(6):
            pulled += 1
            if pulled > 2 and not release.is_set():
                raise AssertionError("input advanced beyond the bounded worker window")
            yield value

    def work(value: int) -> int:
        release.wait(timeout=2)
        return value

    timer = threading.Timer(0.05, release.set)
    timer.start()
    try:
        assert ordered_map(work, values(), workers=2) == tuple(range(6))
    finally:
        timer.cancel()


def test_ordered_map_stops_and_joins_workers_after_failure() -> None:
    prefix = "epubox-test-failure"

    def work(value: int) -> int:
        if value == 1:
            raise RuntimeError("broken")
        time.sleep(0.02)
        return value

    with pytest.raises(RuntimeError, match="broken"):
        ordered_map(work, range(20), workers=3, thread_name_prefix=prefix)

    assert not any(thread.name.startswith(prefix) for thread in threading.enumerate())


def test_ordered_map_uses_serial_path_for_one_item() -> None:
    caller = threading.get_ident()

    assert ordered_map(lambda _value: threading.get_ident(), (1,), workers=4) == (caller,)


def test_ordered_map_processes_overlap_and_are_cleaned_up() -> None:
    caller = os.getpid()
    before = {process.pid for process in multiprocessing.active_children()}

    result = ordered_map(_process_identity, range(6), workers=2, process=True)

    worker_ids = {process_id for process_id, _value in result}
    assert tuple(value for _process_id, value in result) == tuple(range(6))
    assert caller not in worker_ids and 1 <= len(worker_ids) <= 2
    assert {process.pid for process in multiprocessing.active_children()} == before


def test_parallel_preflight_is_identical_to_serial_and_overlaps_documents(monkeypatch) -> None:
    raws = {
        f"OPS/{name}.xhtml": (
            f'<html xmlns="http://www.w3.org/1999/xhtml"><head/><body><p>{name} paragraph.</p></body></html>'
        ).encode()
        for name in ("one", "two", "three")
    }
    inventories = tuple(extract_resource(raw, path, "book") for path, raw in raws.items())
    limits = BudgetLimits(source_tokens=2_000, input_tokens=20_000, output_tokens=4_096, context_tokens=30_000)
    serial = preflight_module.preflight_atomic_resources(
        inventories,
        raws,
        limits,
        "gpt-3.5-turbo",
        workers=1,
    )
    extraction = tuple(
        preflight_module._ExtractInput(inventory.document, raws[inventory.document.resource.path], "book")
        for inventory in inventories
    )
    assert ordered_map(preflight_module._extract_inventory, extraction, workers=1) == ordered_map(
        preflight_module._extract_inventory,
        extraction,
        workers=3,
        process=True,
    )
    active = maximum = 0
    lock = threading.Lock()
    original = preflight_module._preflight_document

    def observed(work):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.02)
            return original(work)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(preflight_module, "_preflight_document", observed)
    parallel = preflight_module.preflight_atomic_resources(
        tuple(reversed(inventories)),
        raws,
        limits,
        "gpt-3.5-turbo",
        workers=3,
    )

    assert maximum > 1
    assert parallel == serial
    assert parallel.pieces == serial.pieces
    assert parallel.diagnostics == serial.diagnostics
    assert parallel.atoms_hash == serial.atoms_hash
    assert parallel.budget_hash == serial.budget_hash
