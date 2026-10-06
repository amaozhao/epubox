"""Atomic-lane adapter for the existing translation execution entry point."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Any

from engine.agents.runtime import RuntimePaused
from engine.agents.workflow import run_workflow
from engine.execution.state import TranslationRunResult
from engine.schemas.members import MemberBatch
from engine.services.atomic import StoreError
from engine.services.store import RunStore


async def run_atomic(
    work_dir: Path | str,
    *,
    model: Any = None,
    transport: Any = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> TranslationRunResult:
    from engine.services.journal import BodyJournal

    store = RunStore(work_dir)
    with store.lock(blocking=False):
        journal = BodyJournal(store)
        session = journal.session
        ready = session.prepared
        journal.recover_results()
        order = {document: index for index, document in enumerate(session.index.document_order)}
        batches = sorted(
            (
                MemberBatch.model_validate_json((store.root / "batches" / f"{identifier}.json").read_bytes())
                for identifier in ready.plan.batch_hashes
            ),
            key=lambda batch: (order[batch.items[0].document_id], batch.items[0].ordinal, batch.items[0].piece_index),
        )
        configured = ready.plan.translation_config.get("concurrency", 2)
        if type(configured) is not int or configured < 1:
            raise ValueError("frozen concurrency must be a positive integer")
        pending = iter(batches)
        active: dict[asyncio.Task, tuple[MemberBatch, float]] = {}
        stopped: str | None = None
        errors: list[str] = []

        def emit(phase: str, batch: MemberBatch | None = None, elapsed: float = 0.0, **extra) -> None:
            if progress is None:
                return
            report: dict[str, Any] = dict(journal.progress_snapshot())
            report.update(phase=phase, execution_state="running", elapsed_seconds=elapsed, **extra)
            if batch is not None:
                report.update(
                    request_id=batch.manifest.request_id,
                    item_ids=batch.manifest.item_ids,
                    chunk_items=len(batch.items),
                    source_tokens=batch.budget.source_tokens,
                    estimated_input_tokens=batch.budget.input_tokens,
                    reserved_input_tokens=batch.budget.input_reserve,
                    reserved_output_tokens=batch.budget.output_tokens,
                )
            progress(report)

        requests: dict[str, dict[str, Any]] = {}
        current_requests: dict[Any, str] = {}

        def request_event(event: dict[str, Any]) -> None:
            if progress is None:
                return
            identifier = event.get("request_id")
            if isinstance(identifier, str) and event.get("event") == "request":
                requests[identifier] = dict(event)
                current_requests[asyncio.current_task()] = identifier
            details = requests.get(identifier or "", {}) if isinstance(identifier, str) else {}
            report = dict(journal.progress_snapshot()) | details | event
            report["phase"] = event.get("stage", "translation")
            report["execution_state"] = "running"
            progress(report)

        runtime = journal.runtime(model=model, transport=transport, progress=request_event)

        async def execute(batch: MemberBatch):
            started = monotonic()

            def save(record):
                journal.save(record)
                request_id = record.checks.get("review_request_id", record.request_id)
                details = requests.get(request_id, {}) if isinstance(request_id, str) else {}
                if progress is not None:
                    progress(
                        dict(journal.progress_snapshot())
                        | details
                        | {
                            "phase": record.stage,
                            "execution_state": "running",
                            "request_id": request_id,
                            "result_item_id": record.item_id,
                            "result_status": str(record.status),
                            "decision": record.checks.get("decision"),
                            "revised": record.checks.get("decision") == "replace",
                            "reason": record.failure,
                            "elapsed_seconds": monotonic() - started,
                        }
                    )

            return await run_workflow(
                ready,
                batch,
                session.index,
                runtime,
                session=session,
                save=save,
                records=journal.records(batch.manifest.item_ids),
            )

        try:
            emit("translation")
            while True:
                while stopped is None and len(active) < configured:
                    batch = next(pending, None)
                    if batch is None:
                        break
                    records = journal.records(batch.manifest.item_ids)
                    if all(str(record.status) == "reviewed" for record in records.values()):
                        continue
                    active[asyncio.create_task(execute(batch))] = (batch, monotonic())
                if not active:
                    break
                done, _ = await asyncio.wait(active, timeout=15, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    for task, (batch, started) in active.items():
                        identifier = current_requests.get(task)
                        if progress is not None:
                            progress(
                                dict(journal.progress_snapshot())
                                | requests.get(identifier or "", {})
                                | {
                                    "phase": "waiting",
                                    "execution_state": "running",
                                    "request_id": identifier,
                                    "elapsed_seconds": monotonic() - started,
                                }
                            )
                    continue
                for task in done:
                    batch, started = active.pop(task)
                    try:
                        result = task.result()
                        errors.extend(result.issues)
                        emit(
                            "translation",
                            None,
                            monotonic() - started,
                            batch_status=result.status,
                            batch_issues=result.issues,
                            reason="; ".join(result.issues) or None,
                        )
                    except RuntimePaused as error:
                        stopped = str(error)
                    except (StoreError, ValueError, OSError) as error:
                        stopped = str(error)
                        errors.append(str(error))
        except BaseException:
            for task in active:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            raise
        snapshot = dict(journal.progress_snapshot())
        accepted = int(snapshot.get("accepted_units", 0))
        attention = int(snapshot.get("needs_attention_units", 0))
        pending_items = int(snapshot.get("pending_items", 0))
        complete = accepted == ready.plan.required_unit_count
        status = "paused" if stopped is not None else "translated" if complete else "needs_attention"
        if progress is not None:
            progress(
                snapshot
                | {
                    "phase": "translation",
                    "execution_state": "stopped",
                    "reason": stopped or "; ".join(errors) or None,
                }
            )
        return TranslationRunResult(
            status=status,
            accepted_units=accepted,
            needs_attention_units=attention,
            pending_items=pending_items,
            http_attempts=int(snapshot.get("http_attempts", 0)),
            predicted_http_requests=2 * len(batches),
            reason=stopped or "; ".join(errors) or None,
        )
