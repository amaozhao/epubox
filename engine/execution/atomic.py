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
from engine.schemas.contracts import ItemStatus
from engine.schemas.members import MemberBatch
from engine.services import state
from engine.services.atomic import StoreError
from engine.services.store import RunStore


async def run_atomic(
    work_dir: Path | str,
    *,
    model: Any = None,
    transport: Any = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    reopen_attention: bool = False,
    ready_session: Any = None,
) -> TranslationRunResult:
    from engine.services.journal import BodyJournal

    store = RunStore(work_dir)
    with store.lock(blocking=False):
        if progress:
            progress({"phase": "recovery", "notice": "翻译：加载已保存的正文记录与请求计划。"})
        journal = BodyJournal(store, session=ready_session, progress=progress)
        session = journal.session
        ready = session.prepared
        journal.recover_results()
        if reopen_attention and ready.plan.translation_config.get("output_budget_version", 2) in {3, 4}:
            records = journal.records()
            units = {
                unit_id
                for unit_id, item_ids in ready.plan.unit_members.items()
                if any(records[item_id].status == ItemStatus.NEEDS_ATTENTION for item_id in item_ids)
            }
            for request in journal._requests.values():
                if request.stage in {"translate", "review"} and journal._ambiguous(request):
                    units.update(unit_id for owners in request.item_unit_ids.values() for unit_id in owners)
            if units:
                journal.retry_units(tuple(sorted(units)))
            if progress:
                progress({"phase": "recovery", "notice": f"恢复：正文断点处理完成，重试 {len(units)} 个单元。"})
        order = {document: index for index, document in enumerate(session.index.document_order)}
        batches = sorted(
            (
                MemberBatch.model_validate_json(state.read(store.root / "batches" / f"{identifier}.json"))
                for identifier in ready.plan.batch_hashes
            ),
            key=lambda batch: (order[batch.items[0].document_id], batch.items[0].ordinal, batch.items[0].piece_index),
        )
        configured = ready.plan.translation_config.get("concurrency", 2)
        if type(configured) is not int or configured < 1:
            raise ValueError("frozen concurrency must be a positive integer")
        pending = iter(batches)
        retry_failures: set[str] = set()
        retry_round = 0
        active: dict[asyncio.Task, tuple[MemberBatch, float]] = {}
        stopped: str | None = (
            "output budget v2 requires a new v3 task before further provider requests"
            if transport is None
            and ready.plan.translation_config.get("output_budget_version", 2) == 2
            and journal.progress_snapshot()["accepted_units"] < ready.plan.required_unit_count
            else None
        )
        errors: list[str] = []

        def emit(phase: str, batch: MemberBatch | None = None, elapsed: float = 0.0, **extra) -> None:
            if progress is None:
                return
            report: dict[str, Any] = dict(journal.progress_snapshot())
            report.update(phase=phase, execution_state="running", elapsed_seconds=elapsed)
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
            report.update(extra)
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

            def observe(record):
                if record.status == ItemStatus.NEEDS_ATTENTION and (
                    record.stage == "review"
                    and record.target_projection is not None
                    or record.stage == "translate"
                    and _structural_failure(record)
                ):
                    retry_failures.add(record.item_id)
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

            def save(record):
                journal.save(record)
                observe(record)

            def save_many(records):
                journal.save_many(records)
                for record in records:
                    observe(record)

            save.save_many = save_many  # type: ignore[attr-defined]

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
                    if stopped is None and retry_round < 2:
                        records = journal.records()
                        units = set()
                        for unit_id in {session.index.members_by_id[item].unit_id for item in retry_failures}:
                            failed = [
                                records[item]
                                for item in ready.plan.unit_members[unit_id]
                                if records[item].status == ItemStatus.NEEDS_ATTENTION
                            ]
                            if (
                                failed
                                and len({record.stage for record in failed}) == 1
                                and all(record.item_id in retry_failures for record in failed)
                            ):
                                units.add(unit_id)
                        groups = [
                            {unit for owners in request.item_unit_ids.values() for unit in owners}
                            for request in journal._requests.values()
                            if request.stage in {"translate", "review"} and journal._ambiguous(request)
                        ]
                        while units:
                            blocked = set().union(*(group for group in groups if group & units and not group <= units))
                            if not blocked:
                                break
                            units.difference_update(blocked)
                        if units:
                            reopened = set(journal.retry_units(tuple(sorted(units))))
                            retry_round += 1
                            pending = iter(
                                batch for batch in batches if reopened.intersection(batch.manifest.item_ids)
                            )
                            if progress:
                                progress(
                                    {
                                        "phase": "review",
                                        "notice": f"正文自动重试：第 {retry_round}/2 轮，{len(units)} 个单元；已有有效初译继续复用。",
                                    }
                                )
                            continue
                    break
                done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    batch, started = active.pop(task)
                    try:
                        result = task.result()
                        errors.extend(result.issues)
                        revised = any(record.checks.get("decision") == "replace" for record in result.results.values())
                        identifier = current_requests.get(task)
                        details = requests.get(identifier or "", {})
                        emit(
                            "workflow",
                            batch,
                            monotonic() - started,
                            **{
                                **{
                                    key: details[key]
                                    for key in (
                                        "source_tokens",
                                        "estimated_input_tokens",
                                        "reserved_input_tokens",
                                        "reserved_output_tokens",
                                    )
                                    if key in details
                                },
                                "request_id": identifier,
                                "batch_status": result.status,
                                "batch_issues": result.issues,
                                "decision": "needs_attention"
                                if result.status == "needs_attention"
                                else "replace"
                                if revised
                                else "no_change",
                                "revised": revised,
                                "reason": "; ".join(result.issues) or None,
                            },
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
        reason = stopped or ("; ".join(errors) if not complete else None) or None
        if progress is not None:
            progress(
                snapshot
                | {
                    "phase": "translation",
                    "execution_state": "stopped",
                    "reason": reason,
                }
            )
        return TranslationRunResult(
            status=status,
            accepted_units=accepted,
            needs_attention_units=attention,
            pending_items=pending_items,
            http_attempts=int(snapshot.get("http_attempts", 0)),
            predicted_http_requests=2 * len(batches),
            reason=reason,
        )


def _structural_failure(record) -> bool:
    message = (record.failure or {}).get("message")
    return isinstance(message, str) and message.startswith(
        (
            "text moved across",
            "reference moved across",
            "locked reference order changed",
            "plain text and metadata units",
            "marker inventory mismatch",
            "crossed or unmatched",
            "unclosed target",
            "duplicate target",
            "unknown projection marker",
            "unclosed projection marker",
            "literal closing delimiter",
            "unsupported projection escape",
            "dangling projection escape",
            "target must be",
            "target contains invalid XML",
            "translation item missing",
            "duplicate item_id",
        )
    )
