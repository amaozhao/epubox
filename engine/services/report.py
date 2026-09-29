"""Derived, replaceable run report built from durable JSON records."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from engine.schemas.contracts import JsonValue, UnitRecord, canonical_hash, canonical_json_bytes
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
    requests = tuple(store.read_request(path.stem) for path in sorted((store.root / "requests").glob("*.json")))
    attempts = tuple(attempt for request in requests for attempt in request.attempts)
    actual = tuple(attempt for attempt in attempts if attempt.state != "reserved")
    known_usages = tuple(attempt.usage for attempt in actual if attempt.usage is not None)
    known_costs = tuple(usage.known_cost for usage in known_usages if usage.known_cost is not None)
    extraction_records = (
        tuple(store.read_extraction(item.item_id) for item in term_plan.items) if term_plan is not None else ()
    )
    unresolved: list[JsonValue] = [
        {"unit_id": unit_id, "issues": list(record.unresolved_issues)}
        for unit_id, record in records.items()
        if record.unresolved_issues
    ]
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
        "required_units": book.required_unit_count if book is not None else None,
        "accepted_units": sum(
            record.accepted_revision == record.revision or _current_derived(record, records)
            for record in records.values()
        ),
        "pending_items": sum(
            item.status in {"pending", "in_flight", "retry_wait"}
            for record in records.values()
            for item in record.items.values()
        ),
        "unresolved_issues": unresolved,
        "http": {
            "actual_attempts": len(actual),
            "reserved_attempts": len(attempts) - len(actual),
            "known_input_tokens": sum(usage.input_tokens for usage in known_usages),
            "known_output_tokens": sum(usage.output_tokens for usage in known_usages),
            "known_cost": sum(known_costs) if known_costs else None,
            "known_cost_attempts": len(known_costs),
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
        "reader_check": "not_run",
        "coherence_by_document": checks,
    }
    path = store.root / "report.json"
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
