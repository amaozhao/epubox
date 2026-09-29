"""Read-only explanation of the next durable action for one run."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from engine.schemas.v25 import (
    BOOK_FORMAT,
    PREPARATION_FORMAT,
    TERM_PLAN_FORMAT,
    UNIT_FORMAT,
    BookPlan,
    PreparationPlan,
    TermExtractionPlan,
    UnitRecord,
    UnsupportedFormatError,
    parse_contract,
    strict_json_loads,
)
from engine.services.atomic_store import CorruptRecord, safe_id
from engine.services.coherence import _read as read_coherence_record

type ResumePhase = Literal["preparation", "terms", "freeze", "translation", "coherence", "publication"]


@dataclass(frozen=True)
class ResumePlan:
    phase: ResumePhase
    status: Literal["ready", "needs_attention", "unsupported_format", "incomplete"]
    actions: tuple[str, ...]
    reasons: tuple[str, ...]
    pending_unit_ids: tuple[str, ...] = ()
    pending_term_item_ids: tuple[str, ...] = ()


def plan_resume(work_dir: Path | str) -> ResumePlan:
    """Inspect saved JSON without creating directories, lock files, or model requests."""
    root = Path(work_dir)
    if not root.is_dir():
        raise FileNotFoundError(root)
    preparation_path = root / "preparation.json"
    old_book_path = root / "bookplan.json"
    if not preparation_path.exists():
        if old_book_path.exists():
            raw = strict_json_loads(old_book_path.read_bytes())
            if isinstance(raw, dict) and raw.get("format") != BOOK_FORMAT:
                return ResumePlan("preparation", "unsupported_format", ("start_new_run",), ("old BookPlan format",))
        return ResumePlan(
            "preparation", "incomplete", ("resume_source_parse",), ("parsed_ready has not been committed",)
        )
    try:
        preparation = parse_contract(preparation_path.read_bytes(), PreparationPlan, PREPARATION_FORMAT)
    except UnsupportedFormatError:
        return ResumePlan("preparation", "unsupported_format", ("start_new_run",), ("old preparation format",))
    _ = preparation
    term_path = root / "glossary" / "plan.json"
    if not term_path.exists():
        return ResumePlan("terms", "incomplete", ("plan_term_windows",), ("parsed_ready source has no term plan",))
    plan = parse_contract(term_path.read_bytes(), TermExtractionPlan, TERM_PLAN_FORMAT)
    if not (root / "glossary" / "freeze.json").exists():
        pending_terms = tuple(
            item.item_id
            for item in plan.items
            if not _term_terminal(root / "glossary" / "extraction" / f"{item.item_id}.json")
        )
        if pending_terms:
            return ResumePlan("terms", "ready", ("extract_pending_windows",), (), pending_term_item_ids=pending_terms)
        pool = root / "glossary" / "candidates.json"
        return ResumePlan(
            "terms" if not pool.exists() else "freeze",
            "ready",
            ("collect_candidates",) if not pool.exists() else ("resolve_or_freeze_candidates",),
            (),
        )
    if not (root / "glossary.json").exists():
        return ResumePlan("freeze", "ready", ("replay_frozen_glossary",), ())
    if not old_book_path.exists():
        return ResumePlan("freeze", "ready", ("initialize_units_and_bookplan",), ())
    try:
        book = parse_contract(old_book_path.read_bytes(), BookPlan, BOOK_FORMAT)
    except UnsupportedFormatError:
        return ResumePlan("translation", "unsupported_format", ("start_new_run",), ("old BookPlan format",))
    try:
        for record_id in (*book.document_hashes, *book.unit_ids):
            safe_id(record_id)
    except ValueError:
        return ResumePlan("preparation", "needs_attention", ("repair_shared_identity",), ("unsafe inventory ID",))
    if preparation.source_path != "source.epub" or preparation.source_hash != book.source_hash:
        return ResumePlan("preparation", "needs_attention", ("repair_shared_identity",), ("source identity mismatch",))
    shared_paths = {
        root / preparation.source_path: book.source_hash,
        preparation_path: book.preparation_hash,
        root / "glossary.json": book.glossary_file_sha256,
        root / "glossary" / "freeze.json": book.freeze_file_sha256,
        **{root / "documents" / f"{document_id}.json": digest for document_id, digest in book.document_hashes.items()},
    }
    damaged = tuple(
        str(path.relative_to(root)) for path, digest in shared_paths.items() if not _matches_hash(path, digest)
    )
    if damaged:
        return ResumePlan("preparation", "needs_attention", ("repair_shared_identity",), damaged)
    records: dict[str, UnitRecord] = {}
    pending: list[str] = []
    attention: list[str] = []
    review: list[str] = []
    for unit_id in book.unit_ids:
        path = root / "units" / f"{unit_id}.json"
        if not path.exists():
            attention.append(unit_id)
            continue
        record = parse_contract(path.read_bytes(), UnitRecord, UNIT_FORMAT)
        records[unit_id] = record
        if record.accepted_revision == record.revision:
            continue
        if (
            record.cut_plan is None
            or record.unresolved_issues
            or any(item.status == "needs_attention" for item in record.items.values())
        ):
            attention.append(unit_id)
        elif any(item.target_projection is not None for item in record.items.values()):
            review.append(unit_id)
        else:
            pending.append(unit_id)
    if pending or review:
        actions = tuple(action for action, values in (("translate", pending), ("review", review)) if values)
        return ResumePlan("translation", "ready", actions, (), pending_unit_ids=(*pending, *review))
    if attention:
        return ResumePlan(
            "translation",
            "needs_attention",
            ("explicit_retry_or_repair",),
            ("local Unit failures remain",),
            pending_unit_ids=tuple(attention),
        )
    if not _coherence_valid(root, book, records):
        return ResumePlan("coherence", "ready", ("check_current_chapters",), ())
    return ResumePlan("publication", "ready", ("verify_and_publish",), ())


def _term_terminal(path: Path) -> bool:
    if not path.exists():
        return False
    raw = strict_json_loads(path.read_bytes())
    return isinstance(raw, dict) and raw.get("status") in {
        "succeeded",
        "succeeded_with_rejections",
        "failed_exhausted",
        "unplannable",
    }


def _coherence_valid(root: Path, book: BookPlan, records: dict[str, UnitRecord]) -> bool:
    for document_id in book.document_hashes:
        path = root / "checks" / f"{document_id}.json"
        if not path.exists():
            return False
        try:
            check = read_coherence_record(path)
        except (CorruptRecord, ValueError):
            return False
        if check.get("status") != "valid" or check.get("document_id") != document_id:
            return False
        windows = check.get("windows")
        completed = check.get("checks")
        vector = check.get("candidate_versions")
        if not isinstance(windows, list) or not isinstance(completed, dict) or not isinstance(vector, dict):
            return False
        window_ids = {window.get("item_id") for window in windows if isinstance(window, dict)}
        participants = {
            unit_id
            for window in windows
            if isinstance(window, dict) and isinstance(window.get("unit_ids"), list)
            for unit_id in window["unit_ids"]
            if isinstance(unit_id, str)
        }
        if not participants.issubset(records):
            return False
        if set(completed) != window_ids or vector != {unit_id: records[unit_id].revision for unit_id in participants}:
            return False
    return True


def _matches_hash(path: Path, expected: str) -> bool:
    return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected


__all__ = ["ResumePlan", "plan_resume"]
