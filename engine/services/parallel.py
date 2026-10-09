"""Small bounded parallel helpers for independent preparation work."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor, wait
from multiprocessing import get_context

PROCESS_MIN_BYTES = 256 * 1024


def default_workers() -> int:
    """Return the conservative preparation worker count."""
    return min(os.cpu_count() or 1, 4)


def ordered_map[Input, Output](
    function: Callable[[Input], Output],
    values: Iterable[Input],
    *,
    workers: int | None = None,
    thread_name_prefix: str = "epubox-prepare",
    process: bool = False,
) -> tuple[Output, ...]:
    """Map independent work concurrently with bounded input and ordered output."""
    limit = default_workers() if workers is None else workers
    if type(limit) is not int or limit < 1:
        raise ValueError("workers must be a positive integer")

    iterator = iter(values)
    try:
        first = next(iterator)
    except StopIteration:
        return ()
    if limit == 1:
        serial = [function(first)]
        serial.extend(function(value) for value in iterator)
        return tuple(serial)
    try:
        second = next(iterator)
    except StopIteration:
        return (function(first),)

    results: dict[int, Output] = {}
    pending: dict[Future[Output], int] = {}
    next_index = 0

    def submit(executor: Executor, value: Input) -> None:
        nonlocal next_index
        pending[executor.submit(function, value)] = next_index
        next_index += 1

    executor = (
        ProcessPoolExecutor(max_workers=limit, mp_context=get_context("spawn"))
        if process
        else ThreadPoolExecutor(max_workers=limit, thread_name_prefix=thread_name_prefix)
    )
    try:
        submit(executor, first)
        submit(executor, second)
        while len(pending) < limit:
            try:
                submit(executor, next(iterator))
            except StopIteration:
                break
        exhausted = False
        while pending:
            done, _waiting = wait(pending, return_when=FIRST_COMPLETED)
            for future in sorted(done, key=pending.__getitem__):
                index = pending.pop(future)
                results[index] = future.result()
            while not exhausted and len(pending) < limit:
                try:
                    submit(executor, next(iterator))
                except StopIteration:
                    exhausted = True
        return tuple(results[index] for index in range(next_index))
    except BaseException:
        for future in pending:
            future.cancel()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


__all__ = ["PROCESS_MIN_BYTES", "default_workers", "ordered_map"]
