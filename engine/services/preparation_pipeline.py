"""The single P1-to-P4 production preparation path for v2.5."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from engine.epub.preparation_v25 import PreparationConfig, _frozen_extraction_config, prepare_book
from engine.item.planner import PlanningError
from engine.item.planner_v25 import plan_unit_v25
from engine.schemas.v25 import (
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
from engine.services.store_v25 import StoreV25
from engine.services.term_freeze import ResolutionDecision, freeze_terminology, prepare_candidate_pool
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
    )


async def resume_preparation(
    work_dir: Path,
    checker: object,
    *,
    term_transport: Any = None,
    resolution_transport: Any = None,
    model: Any = None,
    output_policy_hash: str = "preserve-source-resources-1",
) -> PreparationPipelineResult:
    """Resume P2-P4 using only the committed P1 snapshot and JSON inputs."""
    _ = checker
    store = StoreV25(work_dir)
    preparation = store.read_preparation()
    return await _advance(
        store,
        preparation,
        _sha256(store.root / "preparation.json"),
        term_transport=term_transport,
        resolution_transport=resolution_transport,
        model=model,
        output_policy_hash=output_policy_hash,
    )


async def _advance(
    store: StoreV25,
    preparation: PreparationPlan,
    preparation_hash: str,
    *,
    term_transport: Any,
    resolution_transport: Any,
    model: Any,
    output_policy_hash: str,
) -> PreparationPipelineResult:
    bookplan_path = store.root / "bookplan.json"
    if bookplan_path.exists():
        ready = store.read_bookplan()
        glossary = store.read_glossary()
        has_gaps = glossary.extraction_status == "closed_with_gaps" or any(
            store.read_unit(unit_id).cut_plan is None for unit_id in ready.unit_ids
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
    freeze_path = store.root / "glossary" / "freeze.json"
    term_status = "frozen"
    if not freeze_path.exists():
        term_result = await TermRunner(store, model=model, transport=term_transport).run()
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
            resolution = await resolver.run()
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
    for document in documents:
        for unit in document.units:
            path = store._path("units", unit.unit_id)
            if path.exists():
                record = store.read_unit(unit.unit_id)
            else:
                try:
                    initialized = plan_unit_v25(
                        unit,
                        document,
                        glossary,
                        preparation.translation_config,
                        documents=documents,
                        reading_edges=reading_edges,
                        context_chars=context_chars,
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
) -> tuple[StoreV25, PreparationPlan, str]:
    source = source.resolve(strict=True)
    source_hash = _sha256(source)
    if config.run_id:
        root = work_root / source_hash / config.run_id
        preparation_path = root / "preparation.json"
        if preparation_path.exists():
            store = StoreV25(root)
            preparation = store.read_preparation()
            if (
                preparation.source_hash != source_hash
                or preparation.run_id != config.run_id
                or preparation.extraction_config != _frozen_extraction_config(config)
                or preparation.translation_config != config.translation_config
            ):
                raise IdentityMismatch("resume configuration differs from the committed P1 inputs")
            return store, preparation, _sha256(preparation_path)
    prepared = prepare_book(source, work_root, config, checker)
    store = StoreV25(prepared.work_dir)
    preparation = store.read_preparation()
    return store, preparation, _sha256(store.root / "preparation.json")


def _documents(store: StoreV25, preparation: PreparationPlan) -> tuple[DocumentPlan, ...]:
    ordered = (
        *preparation.reading_order,
        *(document_id for document_id in preparation.document_hashes if document_id not in preparation.reading_order),
    )
    return tuple(
        store.read_document(document_id, expected_hash=preparation.document_hashes[document_id])
        for document_id in ordered
    )


def _initialize_extraction_records(store: StoreV25, plan: TermExtractionPlan) -> None:
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


def _has_unsettled_term_attempts(store: StoreV25) -> bool:
    return any(
        request.stage in {"terms", "resolution"}
        and any(attempt.state in {"sent", "unknown"} for attempt in request.attempts)
        for path in (store.root / "requests").glob("*.json")
        for request in (store.read_request(path.stem),)
    )


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


__all__ = ["PreparationPipelineResult", "prepare_translation", "resume_preparation"]
