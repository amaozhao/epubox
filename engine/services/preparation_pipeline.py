"""The single P1-to-P4 production preparation path for v2.5."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from engine.epub.preparation import (
    PreparationConfig,
    _frozen_extraction_config,
    _frozen_translation_config,
    prepare_book,
)
from engine.item.planner import PlanningError
from engine.item.unit_planner import build_context_index, initial_derived_navigation, plan_unit
from engine.schemas.contracts import (
    BookPlan,
    DocumentPlan,
    GlossarySnapshot,
    PreparationPlan,
    TermExtractionPlan,
    TermExtractionRecord,
    UnitRecord,
    canonical_hash,
)
from engine.services.atomic_store import IdentityMismatch
from engine.services.store import RunStore
from engine.services.term_freeze import ResolutionDecision, freeze_terminology, prepare_candidate_pool
from engine.services.term_inputs import load_user_terms
from engine.services.term_planning import plan_term_extraction
from engine.services.term_resolution import TermResolutionRunner
from engine.services.term_runner import TermRunner


@dataclass(frozen=True)
class PreparationPipelineResult:
    status: Literal["ready", "needs_attention", "paused"]
    phase: Literal["terms", "resolution", "ready"]
    work_dir: Path
    run_id: str
    term_status: str
    bookplan: BookPlan | None = None


@dataclass(frozen=True)
class PreparationProgress:
    phase: Literal["terms", "resolution", "p4", "ready"]
    planned: int
    succeeded: int
    failed: int
    pending: int
    http_attempts: int


type ProgressCallback = Callable[[PreparationProgress], None]


async def prepare_translation(
    source: Path,
    work_root: Path,
    config: PreparationConfig,
    checker: object,
    *,
    term_transport: Any = None,
    resolution_transport: Any = None,
    model: Any = None,
    output_policy_hash: str = "preserve-source-resources-1",
    progress: ProgressCallback | None = None,
) -> PreparationPipelineResult:
    """Create P1 if needed, then advance the durable run through P4."""
    store, preparation, preparation_hash = _p1(source, work_root, config, checker)
    return await _advance(
        store,
        preparation,
        preparation_hash,
        term_transport=term_transport,
        resolution_transport=resolution_transport,
        model=model,
        output_policy_hash=output_policy_hash,
        progress=progress,
    )


async def resume_preparation(
    work_dir: Path,
    checker: object,
    *,
    term_transport: Any = None,
    resolution_transport: Any = None,
    model: Any = None,
    output_policy_hash: str = "preserve-source-resources-1",
    progress: ProgressCallback | None = None,
) -> PreparationPipelineResult:
    """Resume P2-P4 using only the committed P1 snapshot and JSON inputs."""
    _ = checker
    store = RunStore(work_dir)
    preparation = store.read_preparation()
    return await _advance(
        store,
        preparation,
        _sha256(store.root / "preparation.json"),
        term_transport=term_transport,
        resolution_transport=resolution_transport,
        model=model,
        output_policy_hash=output_policy_hash,
        progress=progress,
    )


async def _advance(
    store: RunStore,
    preparation: PreparationPlan,
    preparation_hash: str,
    *,
    term_transport: Any,
    resolution_transport: Any,
    model: Any,
    output_policy_hash: str,
    progress: ProgressCallback | None,
) -> PreparationPipelineResult:
    bookplan_path = store.root / "bookplan.json"
    if bookplan_path.exists():
        ready = store.read_bookplan()
        glossary = store.read_glossary()
        has_gaps = glossary.extraction_status == "closed_with_gaps" or any(
            store.read_unit(unit_id).cut_plan is None for unit_id in ready.unit_ids
        )
        _emit(
            progress,
            PreparationProgress("ready", len(ready.unit_ids), len(ready.unit_ids), 0, 0, _http_attempts(store)),
        )
        return PreparationPipelineResult(
            "needs_attention" if has_gaps else "ready",
            "ready",
            store.root,
            preparation.run_id,
            glossary.extraction_status,
            ready,
        )

    documents = _documents(store, preparation)
    plan_path = store.root / "glossary" / "plan.json"
    if plan_path.exists():
        term_plan = store.read_term_plan()
        store.write_term_plan(term_plan)
    else:
        extraction = preparation.extraction_config
        term_plan = plan_term_extraction(
            documents,
            preparation.user_terms,
            source_hash=preparation.source_hash,
            preparation_hash=preparation_hash,
            auto_extract=_bool(extraction, "auto_extract", True),
            max_primary_chars=_integer(extraction, "max_primary_chars", 12_000),
            adjacent_context_views=_integer(extraction, "adjacent_context_views", 1),
            context_chars=_integer(extraction, "context_chars", 400),
            reading_edges=tuple(zip(preparation.reading_order, preparation.reading_order[1:], strict=False)),
            extraction_identity=extraction,
            item_http_limit=_integer(extraction, "item_http_limit", 6),
            resolution_group_limit=_integer(extraction, "resolution_group_limit", 20),
        ).plan
        store.write_term_plan(term_plan)

    _initialize_extraction_records(store, term_plan)
    _emit(progress, _term_progress(store, term_plan))
    freeze_path = store.root / "glossary" / "freeze.json"
    term_status = "frozen"
    if not freeze_path.exists():
        term_result = await _with_progress(
            TermRunner(store, model=model, transport=term_transport).run(),
            progress,
            lambda: _term_progress(store, term_plan),
        )
        term_status = term_result.status
        if term_result.status == "paused" or _has_unsettled_term_attempts(store):
            return PreparationPipelineResult("paused", "terms", store.root, preparation.run_id, term_result.status)
        records = {item.item_id: store.read_extraction(item.item_id) for item in term_plan.items}
        pool_path = store.root / "glossary" / "candidates.json"
        if pool_path.exists():
            pool = store.read_candidate_pool()
        else:
            pool = store.save_candidate_pool(
                prepare_candidate_pool(
                    term_plan,
                    records,
                    preparation.user_terms,
                    preparation.unit_documents,
                    documents,
                )
            )

        if pool.extraction_status == "open":
            resolver = TermResolutionRunner(store, model=model, transport=resolution_transport or term_transport)
            resolution = await _with_progress(resolver.run(), progress, lambda: _resolution_progress(store))
            if resolution.status == "paused":
                return PreparationPipelineResult("paused", "resolution", store.root, preparation.run_id, term_status)
            decisions = resolver.decisions()
            pool = store.read_candidate_pool()
            frozen = freeze_terminology(
                term_plan,
                records,
                preparation.user_terms,
                preparation.unit_documents,
                documents,
                extraction_config_hash=canonical_hash(preparation.extraction_config),
                resolution_decisions=decisions,
                candidate_pool_version=pool.record_version + 1,
                resolution_response_ids=pool.consumed_response_ids,
            )
            store.save_candidate_pool(frozen.candidate_pool, expected_record_version=pool.record_version)
        else:
            decisions = _stored_decisions(pool.conflict_groups)
            frozen = freeze_terminology(
                term_plan,
                records,
                preparation.user_terms,
                preparation.unit_documents,
                documents,
                extraction_config_hash=canonical_hash(preparation.extraction_config),
                resolution_decisions=decisions,
                candidate_pool_version=pool.record_version,
                resolution_response_ids=pool.consumed_response_ids,
            )
            if frozen.candidate_pool != pool:
                raise IdentityMismatch("closed candidate pool cannot replay its freeze input")
        store.write_freeze(frozen.freeze_intent)
        store.write_glossary(frozen.glossary)
    else:
        freeze = store.read_freeze()
        glossary = GlossarySnapshot.model_validate(freeze.snapshot_payload.model_dump(mode="python"))
        store.write_glossary(glossary)
        term_status = glossary.extraction_status

    glossary = store.read_glossary()
    local_gaps = glossary.extraction_status == "closed_with_gaps"
    initial_plans: dict[str, str | None] = {}
    reading_edges = tuple(zip(preparation.reading_order, preparation.reading_order[1:], strict=False))
    context_chars = _integer(preparation.translation_config, "context_chars", 400)
    context_index = build_context_index(documents, reading_edges, context_chars)
    total_units = sum(len(document.units) for document in documents)
    completed_units = 0
    failed_units = 0
    for document in documents:
        for unit in document.units:
            path = store._path("units", unit.unit_id)
            if path.exists():
                record = store.read_unit(unit.unit_id)
            else:
                try:
                    derived = initial_derived_navigation(unit, document, documents=documents)
                    if derived is not None:
                        record = UnitRecord(
                            unit_id=unit.unit_id,
                            document_id=document.document_id,
                            source_hash=preparation.source_hash,
                            derived=derived,
                        )
                    else:
                        initialized = plan_unit(
                            unit,
                            document,
                            glossary,
                            preparation.translation_config,
                            documents=documents,
                            reading_edges=reading_edges,
                            context_chars=context_chars,
                            context_index=context_index,
                        )
                        record = UnitRecord(
                            unit_id=unit.unit_id,
                            document_id=document.document_id,
                            source_hash=preparation.source_hash,
                            logical_hash=initialized.logical_hash,
                            input_hash=initialized.input_hash,
                            cut_plan=initialized.cut_plan,
                            items=initialized.items,
                        )
                except PlanningError as error:
                    local_gaps = True
                    record = UnitRecord(
                        unit_id=unit.unit_id,
                        document_id=document.document_id,
                        source_hash=preparation.source_hash,
                        unresolved_issues=({"stage": "planning", "code": "unit_unplannable", "message": str(error)},),
                    )
                record = store.save_unit(record)
            initial_plans[unit.unit_id] = record.cut_plan.plan_hash if record.cut_plan is not None else None
            completed_units += record.cut_plan is not None
            failed_units += record.cut_plan is None
            processed = completed_units + failed_units
            if processed == total_units or processed % max(1, total_units // 100) == 0:
                _emit(
                    progress,
                    PreparationProgress(
                        "p4",
                        total_units,
                        completed_units,
                        failed_units,
                        total_units - processed,
                        _http_attempts(store),
                    ),
                )

    unit_ids = tuple(unit.unit_id for document in documents for unit in document.units)
    freeze = store.read_freeze()
    bookplan = BookPlan(
        source_hash=preparation.source_hash,
        run_id=preparation.run_id,
        preparation_hash=preparation_hash,
        glossary_file_sha256=_sha256(store.root / "glossary.json"),
        freeze_file_sha256=_sha256(freeze_path),
        freeze_id=freeze.freeze_id,
        document_hashes=preparation.document_hashes,
        unit_ids=unit_ids,
        unit_documents=preparation.unit_documents,
        required_unit_count=len(unit_ids),
        initial_unit_plans=initial_plans,
        translation_config=preparation.translation_config,
        output_policy_hash=output_policy_hash,
    )
    store.write_bookplan(bookplan)
    ready = store.read_bookplan()
    _emit(
        progress,
        PreparationProgress(
            "ready", len(unit_ids), len(unit_ids) - failed_units, failed_units, 0, _http_attempts(store)
        ),
    )
    return PreparationPipelineResult(
        "needs_attention" if local_gaps else "ready",
        "ready",
        store.root,
        preparation.run_id,
        term_status,
        ready,
    )


def _p1(
    source: Path, work_root: Path, config: PreparationConfig, checker: object
) -> tuple[RunStore, PreparationPlan, str]:
    source = source.resolve(strict=True)
    source_hash = _sha256(source)
    if config.run_id:
        root = work_root / source_hash / config.run_id
        preparation_path = root / "preparation.json"
        if preparation_path.exists():
            store = RunStore(root)
            preparation = store.read_preparation()
            terms, terms_hash = load_user_terms(
                config.user_terms_path,
                document_ids=preparation.document_hashes,
                unit_ids=preparation.unit_documents,
            )
            if (
                preparation.source_hash != source_hash
                or preparation.run_id != config.run_id
                or preparation.extraction_config != _frozen_extraction_config(config)
                or preparation.translation_config != _frozen_translation_config(config)
                or preparation.user_terms != terms
                or preparation.user_terms_hash != terms_hash
            ):
                raise IdentityMismatch("resume configuration differs from the committed P1 inputs")
            return store, preparation, _sha256(preparation_path)
    prepared = prepare_book(source, work_root, config, checker)
    store = RunStore(prepared.work_dir)
    preparation = store.read_preparation()
    return store, preparation, _sha256(store.root / "preparation.json")


def _documents(store: RunStore, preparation: PreparationPlan) -> tuple[DocumentPlan, ...]:
    ordered = (
        *preparation.reading_order,
        *(document_id for document_id in preparation.document_hashes if document_id not in preparation.reading_order),
    )
    return tuple(
        store.read_document(document_id, expected_hash=preparation.document_hashes[document_id])
        for document_id in ordered
    )


def _initialize_extraction_records(store: RunStore, plan: TermExtractionPlan) -> None:
    for item in plan.items:
        path = store._path("glossary/extraction", item.item_id)
        if path.exists():
            store.read_extraction(item.item_id)
            continue
        store.save_extraction(
            TermExtractionRecord(
                item_id=item.item_id,
                document_id=item.document_id,
                view_ids=item.view_ids,
                extraction_input_hash=item.extraction_input_hash,
            )
        )


def _stored_decisions(groups) -> tuple[ResolutionDecision, ...]:
    decisions: list[ResolutionDecision] = []
    for group in groups:
        decision = group.get("decision")
        if decision not in {"select", "defer"}:
            raise IdentityMismatch("closed candidate pool contains an unresolved conflict")
        decisions.append(
            ResolutionDecision(
                group_id=str(group["group_id"]),
                decision=decision,
                selected_candidate_ids=_string_tuple(group.get("selected_candidate_ids", [])),
                restricted_unit_ids=_string_tuple(group.get("restricted_unit_ids", [])),
                reason=str(group.get("reason", "")),
            )
        )
    return tuple(decisions)


def _has_unsettled_term_attempts(store: RunStore) -> bool:
    return any(
        request.stage in {"terms", "resolution"}
        and any(attempt.state in {"sent", "unknown"} for attempt in request.attempts)
        for path in (store.root / "requests").glob("*.json")
        for request in (store.read_request(path.stem),)
    )


async def _with_progress(
    operation: Awaitable[Any],
    callback: ProgressCallback | None,
    snapshot: Callable[[], PreparationProgress],
) -> Any:
    if callback is None:
        return await operation
    task = asyncio.ensure_future(operation)
    previous: PreparationProgress | None = None
    while not task.done():
        current = snapshot()
        if current != previous:
            callback(current)
            previous = current
        await asyncio.wait((task,), timeout=0.25)
    current = snapshot()
    if current != previous:
        callback(current)
    return await task


def _term_progress(store: RunStore, plan: TermExtractionPlan) -> PreparationProgress:
    records = [
        store.read_extraction(item.item_id)
        for item in plan.items
        if store._path("glossary/extraction", item.item_id).exists()
    ]
    succeeded = sum(record.status in {"succeeded", "succeeded_with_rejections"} for record in records)
    failed = sum(record.status in {"failed_exhausted", "unplannable"} for record in records)
    return PreparationProgress(
        "terms",
        len(plan.items),
        succeeded,
        failed,
        len(plan.items) - succeeded - failed,
        _http_attempts(store),
    )


def _resolution_progress(store: RunStore) -> PreparationProgress:
    pool = store.read_candidate_pool()
    succeeded = sum(group.get("decision") == "select" for group in pool.conflict_groups)
    failed = sum(group.get("decision") == "defer" for group in pool.conflict_groups)
    return PreparationProgress(
        "resolution",
        len(pool.conflict_groups),
        succeeded,
        failed,
        len(pool.conflict_groups) - succeeded - failed,
        _http_attempts(store),
    )


def _http_attempts(store: RunStore) -> int:
    return sum(
        attempt.state != "reserved"
        for path in (store.root / "requests").glob("*.json")
        for attempt in store.read_request(path.stem).attempts
    )


def _emit(callback: ProgressCallback | None, event: PreparationProgress) -> None:
    if callback is not None:
        callback(event)


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise IdentityMismatch("stored resolution IDs must be a string array")
    return tuple(value)


def _integer(config: dict[str, Any], name: str, default: int) -> int:
    value = config.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    return value


def _bool(config: dict[str, Any], name: str, default: bool) -> bool:
    value = config.get(name, default)
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "PreparationPipelineResult",
    "PreparationProgress",
    "ProgressCallback",
    "prepare_translation",
    "resume_preparation",
]
