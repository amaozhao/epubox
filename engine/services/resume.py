"""Read-only explanation of the next durable action for one run."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from engine.agents import wire as physical
from engine.agents.protocol import review_applicability, validate_translation_response
from engine.agents.terms import validate_review_response
from engine.item.members import MemberIndex, pack_members
from engine.schemas.bridge import AtomicDocument
from engine.schemas.contracts import (
    BOOK_FORMAT,
    PREPARATION_FORMAT,
    REQUEST_FORMAT,
    TERM_PLAN_FORMAT,
    UNIT_FORMAT,
    BookPlan,
    ItemRecord,
    ItemStatus,
    PreparationPlan,
    RequestManifest,
    TermExtractionPlan,
    UnitRecord,
    UnsupportedFormatError,
    canonical_hash,
    parse_contract,
    strict_json_loads,
)
from engine.schemas.members import MemberBatch, RequestMember
from engine.schemas.ready import AtomicPlan, AtomicPreparedInput
from engine.services import state
from engine.services.atomic import CorruptRecord, safe_id
from engine.services.coherence import _read as read_coherence_record
from engine.services.custody import review_draft, review_feedback
from engine.services.preflight import PreflightReport
from engine.services.ready import derived_record, limits_from_config
from engine.services.store import RunStore

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
    if not state.is_dir(root):
        raise FileNotFoundError(root)
    preparation_path = root / "preparation.json"
    old_book_path = root / "bookplan.json"
    if not state.exists(preparation_path):
        if state.exists(old_book_path):
            raw = strict_json_loads(state.read(old_book_path))
            if isinstance(raw, dict) and raw.get("format") != BOOK_FORMAT:
                return ResumePlan("preparation", "unsupported_format", ("start_new_run",), ("old BookPlan format",))
        return ResumePlan(
            "preparation", "incomplete", ("resume_source_parse",), ("parsed_ready has not been committed",)
        )
    try:
        preparation = parse_contract(state.read(preparation_path), PreparationPlan, PREPARATION_FORMAT)
    except UnsupportedFormatError:
        return ResumePlan("preparation", "unsupported_format", ("start_new_run",), ("old preparation format",))
    _ = preparation
    term_path = root / "glossary" / "plan.json"
    preflight_path = root / "checks" / "preflight.json"
    if state.exists(preflight_path) and not state.exists(term_path):
        try:
            value = strict_json_loads(state.read(preflight_path), max_bytes=None)
            if (
                not isinstance(value, dict)
                or set(value) != {"format", "preparation_hash", "translation_hash", "report"}
                or value.get("format") != "epubox-preflight-record-1"
                or value.get("preparation_hash") != hashlib.sha256(state.read(preparation_path)).hexdigest()
                or value.get("translation_hash") != canonical_hash(preparation.translation_config)
            ):
                raise ValueError("preflight wrapper identity changed")
            report = PreflightReport.model_validate(value["report"])
        except (OSError, TypeError, ValueError) as error:
            return ResumePlan("preparation", "needs_attention", ("repair_shared_identity",), (str(error),))
        if not report.passed:
            blocked = tuple(item.item_id for item in report.diagnostics if item.status == "blocked")
            return ResumePlan(
                "preparation",
                "needs_attention",
                ("adjust_source_or_limits",),
                (f"atomic preflight blocked {len(blocked)} item(s)",),
            )
    if not state.exists(term_path):
        return ResumePlan("terms", "incomplete", ("plan_term_windows",), ("parsed_ready source has no term plan",))
    plan = parse_contract(state.read(term_path), TermExtractionPlan, TERM_PLAN_FORMAT)
    if not state.exists(root / "glossary" / "freeze.json"):
        pending_terms = tuple(
            item.item_id
            for item in plan.items
            if not _term_terminal(root / "glossary" / "extraction" / f"{item.item_id}.json")
        )
        if pending_terms:
            return ResumePlan("terms", "ready", ("extract_pending_windows",), (), pending_term_item_ids=pending_terms)
        pool = root / "glossary" / "candidates.json"
        return ResumePlan(
            "terms" if not state.exists(pool) else "freeze",
            "ready",
            ("collect_candidates",) if not state.exists(pool) else ("resolve_or_freeze_candidates",),
            (),
        )
    if not state.exists(root / "glossary.json"):
        return ResumePlan("freeze", "ready", ("replay_frozen_glossary",), ())
    if state.exists(root / "prepared.json"):
        try:
            marker = strict_json_loads(state.read(root / "prepared.json"))
        except (OSError, TypeError, ValueError) as error:
            return ResumePlan("preparation", "needs_attention", ("repair_shared_identity",), (str(error),))
        if not isinstance(marker, dict) or marker.get("format") != "epubox-prepared-2":
            return ResumePlan(
                "preparation",
                "unsupported_format",
                ("start_new_run",),
                ("unsupported prepared marker format",),
            )
        return _atomic_resume(root)
    if not state.exists(old_book_path):
        return ResumePlan("freeze", "ready", ("initialize_units_and_bookplan",), ())
    try:
        book = parse_contract(state.read(old_book_path), BookPlan, BOOK_FORMAT)
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
        state.snapshot(root): book.source_hash,
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
        if not state.exists(path):
            attention.append(unit_id)
            continue
        record = parse_contract(state.read(path), UnitRecord, UNIT_FORMAT)
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


def _atomic_resume(root: Path) -> ResumePlan:
    """Inspect prepared-2 and result files without constructing a writable store."""
    try:
        ready = AtomicPreparedInput.model_validate_json(state.read(root / "prepared.json"))
        plan = ready.plan
        if AtomicPlan.model_validate_json(state.read(root / "plans" / "book.json")) != plan:
            raise ValueError("plans/book.json")
        shared = {
            state.snapshot(root): plan.source_hash,
            root / "preparation.json": plan.preparation_hash,
            root / "glossary.json": plan.glossary_file_sha256,
            root / "glossary" / "freeze.json": plan.freeze_file_sha256,
            root / "checks" / "preflight.json": plan.preflight_file_sha256,
            **{
                root / "documents" / f"{safe_id(identifier)}.json": digest
                for identifier, digest in plan.document_hashes.items()
            },
            **{
                root / "inventories" / f"{safe_id(identifier)}.json": digest
                for identifier, digest in plan.inventory_hashes.items()
            },
        }
        damaged = [str(path.relative_to(root)) for path, digest in shared.items() if not _matches_hash(path, digest)]
        if damaged:
            return ResumePlan("preparation", "needs_attention", ("repair_shared_identity",), tuple(damaged))
        members: dict[str, RequestMember] = {}
        for identifier, digest in plan.member_hashes.items():
            member = RequestMember.model_validate_json(state.read(root / "members" / f"{safe_id(identifier)}.json"))
            if member.item_id != identifier or canonical_hash(member) != digest:
                raise ValueError(f"members/{identifier}.json")
            members[identifier] = member
        ordered_documents = (
            *ready.preparation.reading_order,
            *(identifier for identifier in plan.inventory_hashes if identifier not in ready.preparation.reading_order),
        )
        inventories = tuple(
            AtomicDocument.model_validate_json(state.read(root / "inventories" / f"{safe_id(identifier)}.json"))
            for identifier in ordered_documents
        )
        report = _atomic_preflight(root, ready)
        ordered_members = tuple(
            members[item_id] for unit_id in plan.unit_ids for item_id in plan.unit_members[unit_id]
        )
        index = MemberIndex(inventories, report, ordered_members)
        for identifier, digest in plan.batch_hashes.items():
            batch = MemberBatch.model_validate_json(state.read(root / "batches" / f"{safe_id(identifier)}.json"))
            if batch.manifest.request_id != identifier or canonical_hash(batch) != digest:
                raise ValueError(f"batches/{identifier}.json")
        expected = set(plan.member_hashes)
        if {path.stem for path in state.glob(root / "results", "*.json")} != expected:
            raise ValueError("results inventory")
        records = {
            identifier: ItemRecord.model_validate_json(state.read(root / "results" / f"{safe_id(identifier)}.json"))
            for identifier in expected
        }
        if any(
            record.item_id != identifier or record.segment_id != identifier for identifier, record in records.items()
        ):
            raise ValueError("result ownership")
        requests: dict[str, RequestManifest] = {}
        for path in state.glob(root / "requests", "*.json"):
            request = parse_contract(state.read(path), RequestManifest, REQUEST_FORMAT)
            if request.request_id != path.stem or request.request_id in requests:
                raise ValueError("request filename or request ID inventory changed")
            requests[request.request_id] = request
        _compact_request_proofs(root, ready, members, records, index, requests)
        for record in records.values():
            source_id = plan.derived_sources.get(members[record.item_id].unit_id)
            if source_id is not None:
                if record != derived_record(record.item_id, source_id):
                    raise ValueError("saved derived result differs from its canonical dependency record")
                continue
            if record.status != ItemStatus.PENDING:
                _atomic_record_frame(root, ready, record, members, index, requests)
            if record.status == ItemStatus.REVIEWED:
                _atomic_review_proof(root, ready, record, members, records, index, requests)
    except (CorruptRecord, OSError, TypeError, ValueError) as error:
        return ResumePlan("preparation", "needs_attention", ("repair_shared_identity",), (str(error),))

    accepted: set[str] = set()
    attention: list[str] = []
    review: list[str] = []
    translate: list[str] = []
    for unit_id in plan.unit_ids:
        if unit_id in plan.derived_sources:
            continue
        values = tuple(records[item_id] for item_id in plan.unit_members[unit_id])
        if all(record.status == ItemStatus.REVIEWED for record in values):
            accepted.add(unit_id)
        elif any(record.status == ItemStatus.NEEDS_ATTENTION for record in values):
            attention.append(unit_id)
        elif any(record.target_projection is not None for record in values):
            review.append(unit_id)
        else:
            translate.append(unit_id)
    remaining = dict(plan.derived_sources)
    while remaining:
        advanced = {unit_id for unit_id, source_id in remaining.items() if source_id in accepted}
        if not advanced:
            break
        accepted.update(advanced)
        for unit_id in advanced:
            del remaining[unit_id]
    if translate or review:
        actions = tuple(action for action, values in (("translate", translate), ("review", review)) if values)
        return ResumePlan("translation", "ready", actions, (), pending_unit_ids=(*translate, *review))
    if attention or remaining:
        pending = (*attention, *remaining)
        return ResumePlan(
            "translation",
            "needs_attention",
            ("explicit_retry_or_repair",),
            ("local atomic results remain incomplete",),
            pending_unit_ids=tuple(pending),
        )
    return ResumePlan("publication", "ready", ("verify_and_publish",), ())


def _atomic_preflight(root: Path, ready: AtomicPreparedInput) -> PreflightReport:
    value = strict_json_loads(state.read(root / "checks" / "preflight.json"), max_bytes=None)
    if (
        not isinstance(value, dict)
        or set(value) != {"format", "preparation_hash", "translation_hash", "report"}
        or value.get("format") != "epubox-preflight-record-1"
        or value.get("preparation_hash") != ready.plan.preparation_hash
        or value.get("translation_hash") != canonical_hash(ready.plan.translation_config)
    ):
        raise ValueError("preflight wrapper identity changed")
    return PreflightReport.model_validate(value["report"])


def _compact_request_proofs(root, ready, members, records, index, requests) -> None:
    for stage in ("translate", "review"):
        for request in requests.values():
            if request.stage != stage or not any(
                "wire_version" in attempt.metadata or "wire_hash" in attempt.metadata for attempt in request.attempts
            ):
                continue
            targets = None
            if stage == "review":
                targets = {}
                for item_id in request.item_ids:
                    saved = records[item_id]
                    _atomic_record_frame(root, ready, saved, members, index, requests)
                    original = requests.get(saved.request_id or "")
                    if original is None:
                        raise ValueError("review member lacks translation request proof")
                    raw = _atomic_response(root, original)
                    result = validate_translation_response(
                        raw["raw"],
                        original.request_id,
                        {key: members[key].source_projection for key in original.item_ids},
                    ).accepted.get(item_id)
                    if result is None or canonical_hash(result["target"]) != request.target_hashes[item_id]:
                        raise ValueError("review member target differs from its translation proof")
                    targets[item_id] = review_draft(
                        saved, result["target"], request.revisions[members[item_id].unit_id]
                    )
                targets = review_feedback(targets, request.feedback_by_item)
            packed = pack_members(
                stage,
                tuple(members[key] for key in request.item_ids),
                ready.glossary,
                index,
                limits_from_config(ready.plan.translation_config, context_unlimited=request.context_unlimited),
                targets=targets,
                revisions=request.revisions if stage == "review" else None,
                record_versions=request.record_versions,
                plan_epochs=request.plan_epochs,
                feedback=request.feedback_by_item,
                tokenizer_model=str(ready.plan.translation_config["model"]),
                sparse=request.sparse,
            )
            if (
                len(packed.batches) != 1
                or packed.blocked
                or packed.batches[0].manifest != request.model_copy(update={"attempts": ()})
            ):
                raise ValueError("compact request differs from its canonical source and targets")
            batch = packed.batches[0]
            physical.verify(request, batch.payload, batch.budget.output_tokens)


def _atomic_record_frame(
    root: Path,
    ready: AtomicPreparedInput,
    record: ItemRecord,
    members: dict[str, RequestMember],
    index: MemberIndex,
    requests: dict[str, RequestManifest],
) -> None:
    frame = record.checks.get("translation_frame")
    if not isinstance(frame, dict) or set(frame) != {"request_id", "member_ids", "batch_hash"}:
        raise ValueError("saved result lacks its canonical translation frame")
    member_ids = frame.get("member_ids")
    if (
        not isinstance(member_ids, list)
        or any(not isinstance(value, str) for value in member_ids)
        or record.item_id not in member_ids
        or len(member_ids) != len(set(member_ids))
    ):
        raise ValueError("saved translation frame has invalid members")
    member_ids = cast(list[str], member_ids)
    try:
        selected = tuple(members[value] for value in member_ids)
    except KeyError as error:
        raise ValueError("saved translation frame references an unknown member") from error
    request_id = frame.get("request_id")
    request = requests.get(request_id) if isinstance(request_id, str) else None
    if request is None:
        raise ValueError("saved translation frame has no durable request")
    value = record.checks.get("translation_epoch", 0)
    if type(value) is not int or value < 0:
        raise ValueError("saved translation epoch must be a non-negative integer")
    if request.record_versions.get(members[record.item_id].unit_id) != value:
        raise ValueError("saved result translation epoch differs from its request frame")
    packed = pack_members(
        "translate",
        selected,
        ready.glossary,
        index,
        limits_from_config(ready.plan.translation_config, context_unlimited=request.context_unlimited),
        record_versions=request.record_versions,
        plan_epochs=request.plan_epochs,
        feedback=request.feedback_by_item,
        tokenizer_model=str(ready.plan.translation_config["model"]),
        sparse=request.sparse,
    )
    if len(packed.batches) != 1 or packed.blocked:
        raise ValueError("saved translation frame is not a canonical fitting batch")
    batch = packed.batches[0]
    if (
        tuple(member_ids) != batch.manifest.item_ids
        or frame.get("request_id") != batch.manifest.request_id
        or frame.get("batch_hash") != canonical_hash(batch)
        or record.request_id != batch.manifest.request_id
        or request is None
        or request.model_copy(update={"attempts": ()}) != batch.manifest
    ):
        raise ValueError("saved translation frame changed")
    physical.verify(request, batch.payload, batch.budget.output_tokens)
    wire_items = cast(list[dict[str, Any]], batch.payload["items"])
    wire = next(value for value in wire_items if value["item_id"] == record.item_id)
    roles = {str(term["term_id"]): term["role"] for term in cast(list[dict[str, Any]], wire["terms"])}
    if (
        record.selected_term_ids != batch.manifest.term_ids_by_item[record.item_id]
        or record.term_applicability != roles
        or record.terms_hash != batch.manifest.terms_hashes[record.item_id]
        or record.context_hash != batch.manifest.context_hashes[record.item_id]
    ):
        raise ValueError("saved result terminology or context identity changed")
    if record.status in {ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE}:
        response = _atomic_response(root, request)
        parsed = validate_translation_response(
            response["raw"],
            request.request_id,
            {item_id: members[item_id].source_projection for item_id in request.item_ids},
        )
        translated = parsed.accepted.get(record.item_id)
        if (
            translated is None
            or record.target_projection != translated["target"]
            or record.target_hash != canonical_hash(translated["target"])
        ):
            raise ValueError("saved draft differs from its persisted translation response")


def _atomic_review_proof(
    root: Path,
    ready: AtomicPreparedInput,
    record: ItemRecord,
    members: dict[str, RequestMember],
    records: dict[str, ItemRecord],
    index: MemberIndex,
    requests: dict[str, RequestManifest],
) -> None:
    if record.stage != "reviewed" or record.failure is not None or record.next_action is not None:
        raise ValueError("reviewed result has an invalid terminal state")
    translation = requests.get(record.request_id or "")
    review_id = record.checks.get("review_request_id")
    review = requests.get(review_id) if isinstance(review_id, str) else None
    if translation is None or translation.stage != "translate" or record.item_id not in translation.item_ids:
        raise ValueError("reviewed result lacks translation request proof")
    if review is None or review.stage != "review" or record.item_id not in review.item_ids:
        raise ValueError("reviewed result lacks review request proof")
    translated = _atomic_response(root, translation)
    reviewed = _atomic_response(root, review)
    translated_result = validate_translation_response(
        translated["raw"],
        translation.request_id,
        {item_id: members[item_id].source_projection for item_id in translation.item_ids},
    )
    initial = translated_result.accepted.get(record.item_id)
    if initial is None or review.target_hashes.get(record.item_id) != canonical_hash(initial["target"]):
        raise ValueError("reviewed result lacks translation target proof")
    expected = {
        item_id: {
            "base_revision": review.revisions[review.item_unit_ids[item_id][0]],
            "source_projection": members[item_id].source_projection,
            **review_applicability(
                members[item_id],
                index,
                {
                    "source": members[item_id].source_projection,
                    "target": (
                        members[item_id].source_projection
                        if review.target_hashes[item_id] == canonical_hash(members[item_id].source_projection)
                        else ""
                    ),
                    "terms": [{"role": role} for role in records[item_id].term_applicability.values()],
                    "applicability": {
                        "terminology": any(role == "target" for role in records[item_id].term_applicability.values()),
                        "bindings": bool(members[item_id].registry),
                    },
                },
            ),
        }
        for item_id in review.item_ids
    }
    reviewed_result = validate_review_response(reviewed["raw"], review.request_id, expected)
    if any(value.get("term_suggestions") for value in reviewed_result.accepted.values()):
        raise ValueError("review response changed its closed batch protocol")
    decision = reviewed_result.accepted.get(record.item_id)
    if (
        decision is None
        or record.checks.get("decision") != decision["decision"]
        or record.checks.get("checks") != decision["checks"]
        or record.checks.get("issues") != decision["issues"]
    ):
        raise ValueError("reviewed result differs from review response")
    target = decision.get("target") if decision["decision"] == "replace" else initial["target"]
    if record.target_projection != target or record.target_hash != canonical_hash(target):
        raise ValueError("reviewed result target lacks response proof")


def _atomic_response(root: Path, request: RequestManifest) -> dict:
    for attempt in reversed(request.attempts):
        path = root / "responses" / request.stage / safe_id(request.request_id) / f"{safe_id(attempt.attempt_id)}.json"
        if not state.exists(path):
            continue
        saved = RunStore._read_model_response_file(path)
        version, physical_hash = physical.identity(request, attempt.attempt_id)
        if (
            saved.stage != request.stage
            or saved.request_id != request.request_id
            or saved.attempt_id != attempt.attempt_id
            or saved.wire_hash != physical_hash
        ):
            raise ValueError("response identity differs from request")
        if saved.response.finish_reason in {"length", "max_tokens"}:
            continue
        response = saved.response.model_dump(mode="python")
        if version is not None:
            response["raw"] = physical.decode(
                request.stage,
                saved.response.raw,
                request.request_id,
                request.item_ids,
                version=version,
                sources=RunStore(root).read_response_sources(request) if version in physical.SLOT_VERSIONS else None,
            )
        return response
    raise ValueError("reviewed result lacks succeeded response file")


def _term_terminal(path: Path) -> bool:
    if not state.exists(path):
        return False
    raw = strict_json_loads(state.read(path))
    return isinstance(raw, dict) and raw.get("status") in {
        "succeeded",
        "succeeded_with_rejections",
        "failed_exhausted",
        "unplannable",
    }


def _coherence_valid(root: Path, book: BookPlan, records: dict[str, UnitRecord]) -> bool:
    for document_id in book.document_hashes:
        path = root / "checks" / f"{document_id}.json"
        if not state.exists(path):
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
    return state.is_file(path) and hashlib.sha256(state.read(path)).hexdigest() == expected


__all__ = ["ResumePlan", "plan_resume"]
