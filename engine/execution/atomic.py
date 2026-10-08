"""Atomic-lane adapter for the existing translation execution entry point."""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Callable, Collection, Sequence
from pathlib import Path
from time import monotonic
from typing import Any

from engine.agents.runtime import RuntimePaused
from engine.agents.workflow import _review_epoch, _translation_epoch, run_workflow
from engine.execution.state import TranslationRunResult
from engine.item.budget import measure_budget
from engine.item.members import pack_members
from engine.schemas.contracts import ItemStatus
from engine.schemas.members import MemberBatch
from engine.services.atomic import StoreError
from engine.services.ready import limits_from_config
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
        recovery_partitions: tuple[tuple[str, ...], ...] = ()
        if reopen_attention and ready.plan.translation_config.get("output_budget_version", 2) in {3, 4, 5, 6}:
            records = journal.records()
            attention = {
                item_id
                for item_ids in ready.plan.unit_members.values()
                for item_id in item_ids
                if records[item_id].status == ItemStatus.NEEDS_ATTENTION
            }
            recovery_partitions = _retry_partitions(journal, attention)
            units = {
                unit_id
                for unit_id, item_ids in ready.plan.unit_members.items()
                if any(records[item_id].status == ItemStatus.NEEDS_ATTENTION for item_id in item_ids)
            }
            for request in journal._requests.values():
                if request.stage in {"translate", "review"} and journal._ambiguous(request):
                    units.update(unit_id for owners in request.item_unit_ids.values() for unit_id in owners)
            if units:
                reopened = set(journal.retry_units(tuple(sorted(units))))
                recovery_partitions = _selected_partitions(recovery_partitions, reopened)
            if progress:
                progress({"phase": "recovery", "notice": f"恢复：正文断点处理完成，重试 {len(units)} 个单元。"})
        order = {document: index for index, document in enumerate(session.index.document_order)}
        batches = sorted(
            session._prepared_batches.values(),
            key=lambda batch: (order[batch.items[0].document_id], batch.items[0].ordinal, batch.items[0].piece_index),
        )
        configured = ready.plan.translation_config.get("concurrency", 2)
        if type(configured) is not int or configured < 1:
            raise ValueError("frozen concurrency must be a positive integer")
        pending = iter(_pending_batches(journal, batches, partitions=recovery_partitions))
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
                        units.difference_update(set().union(*groups) if groups else set())
                        if units:
                            partitions = _retry_partitions(journal, retry_failures)
                            reopened = set(journal.retry_units(tuple(sorted(units))))
                            retry_round += 1
                            pending = iter(
                                _pending_batches(
                                    journal,
                                    batches,
                                    reopened,
                                    partitions=_selected_partitions(partitions, reopened),
                                )
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
                                        "source_channel",
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
        failures = (
            str(record.failure["message"])
            if str(record.failure["message"]).startswith(f"{record.stage}:tx-")
            else f"{record.stage}:{record.item_id}: {record.failure['message']}"
            for record in journal.records().values()
            if record.status == ItemStatus.NEEDS_ATTENTION
            and record.failure is not None
            and isinstance(record.failure.get("message"), str)
        )
        reason = stopped or ("; ".join(dict.fromkeys(failures)) if not complete else None) or None
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


def _pending_batches(
    journal,
    initial,
    selected=None,
    *,
    partitions: Sequence[Collection[str]] = (),
) -> tuple[MemberBatch, ...]:
    """Regroup a checkpoint by HTML, channel and next step, preserving completed members."""
    records = journal.records()
    if not partitions:
        retry_items = {
            item_id
            for item_id, record in records.items()
            if record.status in {ItemStatus.PENDING, ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE}
            and isinstance(record.checks.get("retry_feedback"), str)
        }
        if retry_items:
            partitions = _retry_partitions(journal, retry_items)
    session = journal.session
    ambiguous_items, ambiguous_batches = _ambiguous_ownership(journal, set(selected) if selected is not None else None)
    partition_by_item: dict[str, int] = {}
    for position, partition in enumerate(partitions):
        for item_id in partition:
            if item_id in partition_by_item:
                raise ValueError("retry partitions cannot overlap")
            partition_by_item[item_id] = position
    reuse_intact = selected is None
    fresh = selected is None and all(
        records[item].status == ItemStatus.PENDING and not _translation_epoch(records[item])
        for batch in initial
        for item in batch.manifest.item_ids
    )
    sparse_counts = Counter(
        (batch.items[0].document_id, batch.items[0].channel)
        for batch in initial
        if batch.items[0].channel in {"attribute", "metadata", "navigation"}
    )
    started_sparse = {
        (batch.items[0].document_id, batch.items[0].channel)
        for batch in initial
        if ambiguous_items.intersection(batch.manifest.item_ids)
    }
    merge_sparse = {lane for lane, count in sparse_counts.items() if count > 1 and lane not in started_sparse}
    untouched_body: set[str] = set()
    changed_body: set[str] = set()
    if reuse_intact:
        for member in session.index.members:
            if member.channel != "body":
                continue
            untouched_body.add(member.document_id)
            if (
                member.item_id in partition_by_item
                or member.item_id in ambiguous_items
                or records[member.item_id].status != ItemStatus.PENDING
                or _translation_epoch(records[member.item_id])
            ):
                changed_body.add(member.document_id)
        untouched_body.difference_update(changed_body)
    reusable_batches = [
        batch
        for batch in initial
        if batch.manifest.context_unlimited
        and not any(item_id in partition_by_item for item_id in batch.manifest.item_ids)
        and not ambiguous_items.intersection(batch.manifest.item_ids)
        and (
            (batch.items[0].channel == "body" and batch.items[0].document_id in untouched_body)
            or (fresh and (batch.items[0].document_id, batch.items[0].channel) not in merge_sparse)
        )
    ]
    reusable_batches = [*ambiguous_batches, *_merge_reusable_body(journal, reusable_batches, ambiguous_items)]
    reusable = {item_id for batch in reusable_batches for item_id in batch.manifest.item_ids}
    if (
        fresh
        and len(reusable_batches) == len(initial)
        and len(reusable) == sum(len(batch.manifest.item_ids) for batch in initial)
    ):
        return tuple(initial)
    batches = reusable_batches
    lanes = {}
    for member in session.index.members:
        if member.item_id in reusable:
            continue
        record = records[member.item_id]
        if (
            selected is not None
            and member.item_id not in selected
            or record.status not in {ItemStatus.PENDING, ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE}
            or member.unit_id in session.prepared.plan.derived_sources
        ):
            continue
        key = (
            partition_by_item.get(member.item_id, -1),
            member.document_id,
            member.channel,
            record.target_projection is not None,
        )
        epoch = _review_epoch(record) if record.target_projection is not None else _translation_epoch(record)
        for members, epochs in lanes.setdefault(key, []):
            if epochs.get(member.unit_id, epoch) == epoch:
                members.append(member)
                epochs[member.unit_id] = epoch
                break
        else:
            lanes[key].append(([member], {member.unit_id: epoch}))
    limits = limits_from_config(session.prepared.plan.translation_config, context_unlimited=True)
    model = str(session.prepared.plan.translation_config["model"])
    for members, _ in (group for groups in lanes.values() for group in groups):
        groups = [tuple(members)]
        feedback = {
            member.item_id: value
            for member in members
            if isinstance((value := records[member.item_id].checks.get("retry_feedback")), str)
        }
        if records[members[0].item_id].target_projection is not None:
            review = pack_members(
                "review",
                members,
                session.prepared.glossary,
                session.index,
                limits,
                targets={member.item_id: records[member.item_id] for member in members},
                revisions={member.unit_id: _review_epoch(records[member.item_id]) for member in members},
                feedback=feedback,
                tokenizer_model=model,
                sparse=True,
            )
            if not review.blocked:
                groups = [batch.items for batch in review.batches]
        for group in groups:
            versions = {}
            for member in group:
                versions[member.unit_id] = max(
                    versions.get(member.unit_id, 0), _translation_epoch(records[member.item_id])
                )
            packed = pack_members(
                "translate",
                group,
                session.prepared.glossary,
                session.index,
                limits,
                record_versions=versions,
                feedback={member.item_id: feedback[member.item_id] for member in group if member.item_id in feedback},
                tokenizer_model=model,
                sparse=True,
            )
            if packed.blocked:
                raise ValueError("checkpoint members exceed the saved request capacity")
            batches.extend(packed.batches)
    return tuple(batches)


def _merge_reusable_body(journal, batches: Sequence[MemberBatch], ambiguous_items: set[str]) -> list[MemberBatch]:
    session = journal.session
    limits = limits_from_config(session.prepared.plan.translation_config, context_unlimited=True)
    model = str(session.prepared.plan.translation_config["model"])
    lanes: dict[str, list[MemberBatch]] = {}
    for batch in batches:
        if batch.items[0].channel == "body":
            lanes.setdefault(batch.items[0].document_id, []).append(batch)
    replacements: dict[int, list[MemberBatch]] = {}
    skipped: set[int] = set()
    for values in lanes.values():
        merged: list[MemberBatch] = []
        current = values[0]
        for following in values[1:]:
            candidate = None
            if (
                not ambiguous_items.intersection(current.manifest.item_ids)
                and not ambiguous_items.intersection(following.manifest.item_ids)
                and current.budget.source_tokens + following.budget.source_tokens <= limits.source_ceiling
                and _epochs_match(current, following)
            ):
                packed = pack_members(
                    "translate",
                    (*current.items, *following.items),
                    session.prepared.glossary,
                    session.index,
                    limits,
                    record_versions=current.manifest.record_versions | following.manifest.record_versions,
                    plan_epochs=current.manifest.plan_epochs | following.manifest.plan_epochs,
                    feedback=current.manifest.feedback_by_item | following.manifest.feedback_by_item,
                    tokenizer_model=model,
                    sparse=True,
                    whole=True,
                )
                if (
                    len(packed.batches) == 1
                    and not packed.blocked
                    and _estimated_review_fits(packed.batches[0], limits, model)
                ):
                    candidate = packed.batches[0]
            if candidate is None:
                merged.append(current)
                current = following
            else:
                current = candidate
        merged.append(current)
        replacements[id(values[0])] = merged
        skipped.update(id(value) for value in values[1:])
    result: list[MemberBatch] = []
    for batch in batches:
        if replacement := replacements.get(id(batch)):
            result.extend(replacement)
        elif id(batch) not in skipped:
            result.append(batch)
    return result


def _ambiguous_ownership(journal, selected: set[str] | None) -> tuple[set[str], list[MemberBatch]]:
    ambiguous = getattr(journal, "_ambiguous", None)
    items: set[str] = set()
    batches: list[MemberBatch] = []
    for request in getattr(journal, "_requests", {}).values():
        if (
            selected is not None
            and not selected.intersection(request.item_ids)
            or not bool(ambiguous(request) if callable(ambiguous) else request.attempts)
        ):
            continue
        items.update(request.item_ids)
        restore = getattr(journal, "_translation_batch", None)
        if request.stage == "translate" and callable(restore):
            batch = restore(request)
            if not isinstance(batch, MemberBatch):
                raise TypeError("ambiguous translation request has no restorable batch")
            batches.append(batch)
    return items, batches


def _epochs_match(left: MemberBatch, right: MemberBatch) -> bool:
    for field in ("record_versions", "plan_epochs"):
        first, second = getattr(left.manifest, field), getattr(right.manifest, field)
        if any(first[unit] != second[unit] for unit in set(first) & set(second)):
            return False
    return True


def _estimated_review_fits(batch: MemberBatch, limits, model: str) -> bool:
    payload = dict(batch.payload)
    payload["protocol"] = "epubox-review-2"
    payload.pop("target_language", None)
    return measure_budget(
        stage="review",
        payload=payload,
        limits=limits,
        review_targets="estimated",
        tokenizer_model=model,
    ).fits


def _structural_failure(record) -> bool:
    message = _retry_message(record)
    if not isinstance(message, str):
        return False
    if message.endswith(("translation response was truncated", "review response was truncated")):
        return True
    detail = _failure_detail(message)
    return detail.startswith(
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
            "target decoding failed:",
            "target contains invalid XML",
            "translation item missing",
            "review item missing",
            "duplicate item_id",
            "response root must",
        )
    )


def _retry_partitions(journal, selected: Collection[str]) -> tuple[tuple[str, ...], ...]:
    """Keep failed request ownership while shrinking retries that cannot succeed unchanged."""
    wanted = set(selected)
    records = journal.records(tuple(wanted))
    truncated: dict[str, list[str]] = {}
    for item_id, record in records.items():
        message = _retry_message(record)
        if not isinstance(message, str) or not message.endswith("response was truncated"):
            continue
        match = re.match(r"^(?:translate|review):(tx-[^:]+):", message)
        if match and match.group(1) in journal._requests:
            truncated.setdefault(match.group(1), []).append(item_id)

    partitions: list[tuple[str, ...]] = []
    for request_id in truncated:
        request = journal._requests[request_id]
        members = tuple(item_id for item_id in request.item_ids if item_id in wanted)
        if not members:
            continue
        middle = max(1, len(members) // 2)
        partitions.append(members[:middle])
        if members[middle:]:
            partitions.append(members[middle:])
    return tuple(partitions)


def _retry_message(record) -> str | None:
    message = (record.failure or {}).get("message")
    if isinstance(message, str):
        return message
    feedback = record.checks.get("retry_feedback")
    return feedback if isinstance(feedback, str) else None


def _failure_detail(message: str) -> str:
    return re.sub(r"^(?:translate|review):tx-[^:]+:\s*", "", message)


def _selected_partitions(
    partitions: Sequence[Collection[str]], selected: Collection[str]
) -> tuple[tuple[str, ...], ...]:
    wanted = set(selected)
    return tuple(values for partition in partitions if (values := tuple(item for item in partition if item in wanted)))
