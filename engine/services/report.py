"""Derived, replaceable run report built from durable JSON records."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from engine.agents.runtime import MAX_MODEL_INPUT_TOKENS
from engine.schemas.contracts import (
    JsonValue,
    UnitRecord,
    Usage,
    canonical_hash,
    canonical_json_bytes,
    strict_json_loads,
)
from engine.services.coherence import _read as read_coherence_record
from engine.services.store import RunStore

type Outcome = Literal["completed", "paused", "needs_attention", "failed"]


def write_report(
    store: RunStore,
    *,
    status: Outcome,
    phase: str,
    output_path: Path | None = None,
    output_sha256: str | None = None,
    reason: str | None = None,
) -> Path:
    """Snapshot facts without treating report.json as a resume authority."""
    preparation = store.read_preparation()
    glossary_path = store.root / "glossary.json"
    glossary = store.read_glossary() if glossary_path.exists() else None
    plan_path = store.root / "glossary" / "plan.json"
    term_plan = store.read_term_plan() if plan_path.exists() else None
    pool_path = store.root / "glossary" / "candidates.json"
    pool = store.read_candidate_pool() if pool_path.exists() else None
    book_path = store.root / "bookplan.json"
    book = store.read_bookplan() if book_path.exists() else None
    records = {unit_id: store.read_unit(unit_id) for unit_id in book.unit_ids} if book is not None else {}
    atomic = None
    atomic_required = None
    atomic_records = {}
    if (store.root / "prepared.json").is_file():
        from engine.services.journal import BodyJournal

        journal = BodyJournal(store)
        atomic = journal.progress_snapshot()
        atomic_required = journal.session.prepared.plan.required_unit_count
        atomic_records = journal.records()
    requests = tuple(store.read_request(path.stem) for path in sorted((store.root / "requests").glob("*.json")))
    attempts = tuple(attempt for request in requests for attempt in request.attempts)
    actual = tuple(attempt for attempt in attempts if attempt.state != "reserved")
    usage_by_attempt: dict[tuple[str, str], Usage] = {}
    journal_recovered_usage_attempts = 0
    for request in requests:
        for attempt in request.attempts:
            if attempt.state == "reserved":
                continue
            usage = attempt.usage
            if usage is None:
                response = store.read_model_response(request.stage, request.request_id, attempt.attempt_id)
                if response is not None and response.usage is not None:
                    usage = Usage(
                        input_tokens=response.usage.input_tokens,
                        output_tokens=response.usage.output_tokens,
                        known_cost=response.usage.known_cost,
                    )
                    journal_recovered_usage_attempts += 1
            if usage is not None:
                usage_by_attempt[(request.request_id, attempt.attempt_id)] = usage
    known_usages = tuple(usage_by_attempt.values())
    known_costs = tuple(usage.known_cost for usage in known_usages if usage.known_cost is not None)
    by_stage: dict[str, JsonValue] = {}
    for stage in sorted({request.stage for request in requests}):
        stage_requests = tuple(request for request in requests if request.stage == stage)
        stage_attempts = tuple(
            attempt for request in stage_requests for attempt in request.attempts if attempt.state != "reserved"
        )
        stage_usages = tuple(
            usage_by_attempt[(request.request_id, attempt.attempt_id)]
            for request in stage_requests
            for attempt in request.attempts
            if (request.request_id, attempt.attempt_id) in usage_by_attempt
        )
        by_stage[stage] = {
            "actual_attempts": len(stage_attempts),
            "max_items_per_request": max((len(request.item_ids) for request in stage_requests), default=0),
            "max_known_input_tokens": max((usage.input_tokens for usage in stage_usages), default=0),
        }
    extraction_records = (
        tuple(store.read_extraction(item.item_id) for item in term_plan.items) if term_plan is not None else ()
    )
    unresolved: list[JsonValue] = (
        [
            {"item_id": item_id, "issues": [record.failure]}
            for item_id, record in atomic_records.items()
            if record.failure is not None
        ]
        if atomic is not None
        else [
            {"unit_id": unit_id, "issues": list(record.unresolved_issues)}
            for unit_id, record in records.items()
            if record.unresolved_issues
        ]
    )
    checks: dict[str, JsonValue] = {}
    if book is not None:
        for document_id in book.document_hashes:
            path = store._path("checks", document_id)
            checks[document_id] = read_coherence_record(path).get("status", "unknown") if path.exists() else "missing"
    report: dict[str, JsonValue] = {
        "format": "epubox-report-2",
        "status": status,
        "execution_state": "stopped",
        "phase": phase,
        "run_id": preparation.run_id,
        "source_hash": preparation.source_hash,
        "work_dir": str(store.root),
        "output_path": str(output_path) if status == "completed" and output_path is not None else None,
        "output_sha256": output_sha256 if status == "completed" else None,
        "reason": reason,
        "source_units": len(preparation.unit_documents),
        "required_units": (
            atomic_required if atomic is not None else book.required_unit_count if book is not None else None
        ),
        "accepted_units": (
            atomic["accepted_units"]
            if atomic is not None
            else sum(
                record.accepted_revision == record.revision or _current_derived(record, records)
                for record in records.values()
            )
        ),
        "pending_items": (
            atomic["pending_items"]
            if atomic is not None
            else sum(
                item.status in {"pending", "in_flight", "retry_wait"}
                for record in records.values()
                for item in record.items.values()
            )
        ),
        "unresolved_issues": unresolved,
        "http": {
            "actual_attempts": len(actual),
            "reserved_attempts": len(attempts) - len(actual),
            "known_input_tokens": sum(usage.input_tokens for usage in known_usages),
            "known_output_tokens": sum(usage.output_tokens for usage in known_usages),
            "journal_recovered_usage_attempts": journal_recovered_usage_attempts,
            "max_known_input_tokens": max((usage.input_tokens for usage in known_usages), default=0),
            "max_preflight_input_bound": max(
                (attempt.reservation.get("estimated_input_tokens", 0) for attempt in attempts), default=0
            ),
            "max_rendered_utf8_bytes": max(
                (attempt.reservation.get("rendered_input_bytes", 0) for attempt in attempts), default=0
            ),
            "input_budget_algorithm_version": max(
                (attempt.reservation.get("input_budget_algorithm_version", 0) for attempt in attempts), default=0
            ),
            "preflight_limit_kind": "conservative_local_bound_not_provider_exact",
            "input_limit_violations": sum(usage.input_tokens > MAX_MODEL_INPUT_TOKENS for usage in known_usages),
            "by_stage": by_stage,
            "known_cost": sum(known_costs) if known_costs else None,
            "known_cost_attempts": len(known_costs),
        },
        "json_paths": {
            "documents": str(store.root / "documents"),
            "units": str(store.root / "units"),
            "results": str(store.root / "results"),
            "prepared": str(store.root / "prepared.json"),
            "requests": str(store.root / "requests"),
            "glossary": str(store.root / "glossary.json"),
            "report": str(store.root / "report.json"),
        },
        "terminology": {
            "planned_windows": len(term_plan.items) if term_plan is not None else 0,
            "succeeded_windows": sum(
                record.status in {"succeeded", "succeeded_with_rejections"} for record in extraction_records
            ),
            "succeeded_with_rejections_windows": sum(
                record.status == "succeeded_with_rejections" for record in extraction_records
            ),
            "failed_windows": sum(
                record.status in {"failed_exhausted", "unplannable"} for record in extraction_records
            ),
            "pending_windows": sum(
                record.status in {"pending", "in_flight", "retry_wait"} for record in extraction_records
            ),
            "candidate_count": len(pool.candidates) if pool is not None else 0,
            "rejected_candidate_count": (
                sum(candidate.status in {"rejected_evidence", "rejected_schema"} for candidate in pool.candidates)
                + len(pool.rejections)
                if pool is not None
                else 0
            ),
            "frozen_term_count": len(glossary.terms) if glossary is not None else 0,
            "extraction_status": glossary.extraction_status
            if glossary is not None
            else pool.extraction_status
            if pool is not None
            else "open",
            "warnings": list(glossary.warnings) if glossary is not None else [],
            "freeze_id": glossary.freeze_id if glossary is not None else None,
        },
        "body": atomic,
        "reader_check": "not_run",
        "coherence_by_document": checks,
    }
    path = store.root / "report.json"
    if path.is_file():
        try:
            previous = strict_json_loads(path.read_bytes())
        except ValueError:
            previous = None
        diagnostic = previous.get("source_validation") if isinstance(previous, dict) else None
        if isinstance(diagnostic, dict) and diagnostic.get("source_hash") == preparation.source_hash:
            report["source_validation"] = diagnostic
    publish_path = store.root / "publish.json"
    if publish_path.is_file():
        publication = strict_json_loads(publish_path.read_bytes())
        if isinstance(publication, dict) and publication.get("target_hash") == output_sha256:
            report["publication_verification"] = publication.get("verification")
    store._base.atomic_write_bytes(path, canonical_json_bytes(report))
    return path


__all__ = ["write_report"]


def _current_derived(record: UnitRecord, records: dict[str, UnitRecord]) -> bool:
    derived = record.derived
    if derived is None or derived.get("state") != "valid":
        return False
    source_id = derived.get("source_unit_id")
    if not isinstance(source_id, str):
        return False
    source = records.get(source_id)
    target = derived.get("target")
    return (
        source is not None
        and source.accepted_revision == source.revision
        and source.accepted_target_hash == derived.get("source_target_hash")
        and source.revision == derived.get("source_revision")
        and isinstance(target, str)
        and derived.get("target_hash") == canonical_hash(target)
    )
