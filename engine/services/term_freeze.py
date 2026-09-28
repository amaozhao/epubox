"""Pure candidate-pool closure and immutable glossary freezing."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from engine.schemas.v25 import (
    CandidatePool,
    DocumentPlan,
    FreezeIntent,
    FrozenTerm,
    GlossaryPayload,
    GlossarySnapshot,
    JsonValue,
    TermCandidate,
    TermEvidence,
    TermExtractionPlan,
    TermExtractionRecord,
    TermScope,
    UserTerm,
    candidate_pool_record_hash,
    canonical_hash,
    glossary_rules_hash,
)
from engine.services.term_candidates import dispose_candidates

_TERMINAL = {"succeeded", "succeeded_with_rejections", "failed_exhausted", "unplannable"}


@dataclass(frozen=True)
class ResolutionDecision:
    group_id: str
    decision: Literal["select", "defer"]
    selected_candidate_ids: tuple[str, ...] = ()
    restricted_unit_ids: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class TermFreezeResult:
    candidate_pool: CandidatePool
    freeze_intent: FreezeIntent
    glossary: GlossarySnapshot


def prepare_candidate_pool(
    plan: TermExtractionPlan,
    records: dict[str, TermExtractionRecord],
    user_terms: tuple[UserTerm, ...],
    unit_documents: dict[str, str],
    documents: Sequence[DocumentPlan],
) -> CandidatePool:
    """Create the stable open pool required before optional paid conflict resolution."""

    _validate_inputs(plan, records, unit_documents, documents)
    raw_candidates = _raw_candidates(records)
    disposition = dispose_candidates(raw_candidates, user_terms, unit_documents=unit_documents)
    return _candidate_pool(
        plan,
        records,
        "open",
        disposition.candidates,
        tuple(_json_group(group) for group in disposition.conflict_groups),
    )


def freeze_terminology(
    plan: TermExtractionPlan,
    records: dict[str, TermExtractionRecord],
    user_terms: tuple[UserTerm, ...],
    unit_documents: dict[str, str],
    documents: Sequence[DocumentPlan],
    *,
    extraction_config_hash: str,
    resolution_decisions: tuple[ResolutionDecision, ...] = (),
    candidate_pool_version: int = 0,
    resolution_response_ids: tuple[str, ...] = (),
) -> TermFreezeResult:
    """Close terminal extraction state and build one deterministic frozen glossary."""

    _validate_inputs(plan, records, unit_documents, documents)
    if candidate_pool_version < 0:
        raise ValueError("candidate_pool_version cannot be negative")
    if resolution_decisions and candidate_pool_version == 0:
        raise ValueError("resolved candidate pools must advance beyond the open pool version")
    extraction_status = _extraction_status(plan, records)
    raw_candidates = _raw_candidates(records)
    disposition = dispose_candidates(raw_candidates, user_terms, unit_documents=unit_documents)
    open_pool = prepare_candidate_pool(plan, records, user_terms, unit_documents, documents)
    candidates = {candidate.candidate_id: candidate for candidate in open_pool.candidates}
    scopes = dict(disposition.effective_scopes)
    conflict_groups = _apply_resolutions(
        open_pool.conflict_groups,
        resolution_decisions,
        candidates,
        scopes,
        raw_candidates,
        user_terms,
        unit_documents,
    )
    ordered_candidates = tuple(candidates[candidate_id] for candidate_id in sorted(candidates))
    pool = _candidate_pool(
        plan,
        records,
        extraction_status,
        ordered_candidates,
        conflict_groups,
        record_version=candidate_pool_version,
        additional_response_ids=resolution_response_ids,
    )
    terms = _frozen_user_terms(user_terms) + _frozen_model_terms(ordered_candidates, scopes, unit_documents)
    terms = tuple(sorted(terms, key=lambda term: term.term_id))
    warnings = _warnings(extraction_status, ordered_candidates, conflict_groups, terms)
    user_terms_hash = canonical_hash(user_terms)
    pool_hash = canonical_hash(pool)
    freeze_id = (
        "tf-"
        + canonical_hash(
            {
                "source_hash": plan.source_hash,
                "preparation_hash": plan.preparation_hash,
                "term_plan_hash": plan.plan_hash,
                "candidate_pool_hash": pool_hash,
                "user_terms_hash": user_terms_hash,
                "extraction_config_hash": extraction_config_hash,
                "terms": terms,
                "warnings": warnings,
            }
        )[:24]
    )
    coverage = _coverage(records, ordered_candidates, conflict_groups)
    payload = GlossaryPayload(
        source_hash=plan.source_hash,
        freeze_id=freeze_id,
        extraction_config_hash=extraction_config_hash,
        user_terms_hash=user_terms_hash,
        extraction_status=extraction_status,
        warnings=warnings,
        terms=terms,
    )
    freeze = FreezeIntent(
        freeze_id=freeze_id,
        source_hash=plan.source_hash,
        preparation_hash=plan.preparation_hash,
        term_plan_hash=plan.plan_hash,
        candidate_pool_hash=pool_hash,
        user_terms_hash=user_terms_hash,
        rules_hash=glossary_rules_hash(terms),
        coverage=coverage,
        snapshot_payload=payload,
    )
    return TermFreezeResult(pool, freeze, GlossarySnapshot.model_validate(payload.model_dump(mode="python")))


def _raw_candidates(records: dict[str, TermExtractionRecord]) -> tuple[TermCandidate, ...]:
    candidates = tuple(
        candidate
        for item_id in sorted(records)
        for candidate in sorted(records[item_id].candidates, key=lambda value: value.candidate_id)
    )
    if len({candidate.candidate_id for candidate in candidates}) != len(candidates):
        raise ValueError("candidate IDs must remain unique across extraction records")
    return candidates


def _validate_inputs(
    plan: TermExtractionPlan,
    records: dict[str, TermExtractionRecord],
    unit_documents: dict[str, str],
    documents: Sequence[DocumentPlan],
) -> None:
    document_map = {document.document_id: document for document in documents}
    if len(document_map) != len(documents):
        raise ValueError("document IDs must be unique")
    if any(document.source_hash != plan.source_hash for document in documents):
        raise ValueError("documents belong to another source")
    actual_units = {unit.unit_id: document.document_id for document in documents for unit in document.units}
    if unit_documents != actual_units:
        raise ValueError("unit_documents must exactly match the frozen document inventory")
    items = {item.item_id: item for item in plan.items}
    if set(records) != set(items):
        raise ValueError("terminal freeze requires one record for every extraction item")
    for item_id, record in records.items():
        item = items[item_id]
        if record.status not in _TERMINAL:
            raise ValueError(f"extraction item {item_id} is not terminal")
        if (
            record.item_id != item_id
            or record.document_id != item.document_id
            or record.view_ids != item.view_ids
            or record.extraction_input_hash != item.extraction_input_hash
        ):
            raise ValueError(f"extraction record identity mismatch: {item_id}")
        if item.document_id not in document_map:
            raise ValueError(f"extraction item references an unknown document: {item_id}")
        if record.status in {"failed_exhausted", "unplannable"} and record.candidates:
            raise ValueError(f"failed extraction item cannot retain candidates: {item_id}")


def _extraction_status(
    plan: TermExtractionPlan, records: dict[str, TermExtractionRecord]
) -> Literal["closed", "closed_with_gaps", "disabled", "not_required"]:
    if not plan.auto_extract:
        if plan.items or records:
            raise ValueError("disabled extraction cannot contain items or records")
        return "disabled"
    if not plan.items:
        if records:
            raise ValueError("not-required extraction cannot contain records")
        return "not_required"
    if any(record.status in {"failed_exhausted", "unplannable"} for record in records.values()):
        return "closed_with_gaps"
    return "closed"


def _apply_resolutions(
    groups: Sequence[Mapping[str, object]],
    decisions: tuple[ResolutionDecision, ...],
    candidates: dict[str, TermCandidate],
    scopes: dict[str, TermScope],
    raw_candidates: tuple[TermCandidate, ...],
    user_terms: tuple[UserTerm, ...],
    unit_documents: dict[str, str],
) -> tuple[dict[str, JsonValue], ...]:
    by_group = {decision.group_id: decision for decision in decisions}
    if len(by_group) != len(decisions) or not set(by_group).issubset({str(group["group_id"]) for group in groups}):
        raise ValueError("resolution decisions must uniquely reference current conflict groups")
    raw_by_id = {candidate.candidate_id: candidate for candidate in raw_candidates}
    resolved: list[dict[str, JsonValue]] = []
    for group in groups:
        group_id = str(group["group_id"])
        candidate_ids = _string_list(group, "candidate_ids")
        allowed = set(_string_list(group, "allowed_unit_ids"))
        decision = by_group.get(group_id)
        if decision is None or decision.decision == "defer":
            if decision is not None and (decision.selected_candidate_ids or decision.restricted_unit_ids):
                raise ValueError("deferred resolution cannot select candidates or Units")
            resolved.append(
                _json_group(group)
                | {
                    "status": "deferred_conflict",
                    "decision": "defer",
                    "reason": decision.reason if decision is not None else "no_resolution_result",
                }
            )
            continue
        selected = tuple(sorted(set(decision.selected_candidate_ids)))
        restricted = set(decision.restricted_unit_ids) if decision.restricted_unit_ids else allowed
        if not selected or not set(selected).issubset(candidate_ids) or not restricted.issubset(allowed):
            raise ValueError(f"invalid resolution selection for group {group_id}")
        signatures = {
            (
                raw_by_id[candidate_id].source,
                raw_by_id[candidate_id].target,
                raw_by_id[candidate_id].category,
                raw_by_id[candidate_id].note,
            )
            for candidate_id in selected
        }
        if len(signatures) != 1:
            raise ValueError(f"resolution selected incompatible candidates for group {group_id}")
        adopted: list[str] = []
        for candidate_id in selected:
            individual = dispose_candidates((raw_by_id[candidate_id],), user_terms, unit_documents=unit_documents)
            scope = individual.effective_scopes.get(candidate_id)
            units = _scope_units(scope, unit_documents) & restricted if scope is not None else set()
            if not units:
                continue
            candidates[candidate_id] = raw_by_id[candidate_id].model_copy(update={"status": "adopted_preferred"})
            scopes[candidate_id] = _units_scope(units, unit_documents)
            adopted.append(candidate_id)
        if not adopted:
            raise ValueError(f"resolution selected no applicable scope for group {group_id}")
        resolved.append(
            _json_group(group)
            | {
                "status": "resolved",
                "decision": "select",
                "selected_candidate_ids": adopted,
                "restricted_unit_ids": sorted(restricted),
                "reason": decision.reason,
            }
        )
    return tuple(resolved)


def _candidate_pool(
    plan: TermExtractionPlan,
    records: dict[str, TermExtractionRecord],
    extraction_status: Literal["open", "closed", "closed_with_gaps", "disabled", "not_required"],
    candidates: tuple[TermCandidate, ...],
    conflict_groups: tuple[dict[str, JsonValue], ...],
    *,
    record_version: int = 0,
    additional_response_ids: tuple[str, ...] = (),
) -> CandidatePool:
    pool = CandidatePool(
        source_hash=plan.source_hash,
        preparation_hash=plan.preparation_hash,
        term_plan_hash=plan.plan_hash,
        record_version=record_version,
        extraction_status=extraction_status,
        candidates=candidates,
        conflict_groups=conflict_groups,
        consumed_response_ids=tuple(
            sorted(
                {
                    *additional_response_ids,
                    *(request_id for record in records.values() for request_id in record.request_ids),
                }
            )
        ),
    )
    return CandidatePool.model_validate(
        pool.model_dump(mode="python") | {"record_hash": candidate_pool_record_hash(pool)}
    )


def _frozen_user_terms(user_terms: tuple[UserTerm, ...]) -> tuple[FrozenTerm, ...]:
    return tuple(
        FrozenTerm(
            term_id=term.term_id,
            source=term.source,
            target=term.target,
            aliases=term.aliases,
            scope=term.scope,
            mode=term.mode,
            match_policy=term.match_policy,
            note=term.note,
            origin="user",
        )
        for term in user_terms
    )


def _frozen_model_terms(
    candidates: tuple[TermCandidate, ...],
    scopes: dict[str, TermScope],
    unit_documents: dict[str, str],
) -> tuple[FrozenTerm, ...]:
    grouped: dict[tuple[str, str, str, str], list[TermCandidate]] = {}
    for candidate in candidates:
        if candidate.status != "adopted_preferred":
            continue
        grouped.setdefault((candidate.source, candidate.target, candidate.category, candidate.note), []).append(
            candidate
        )
    terms: list[FrozenTerm] = []
    for (source, target, category, note), group in sorted(grouped.items()):
        candidate_ids = tuple(sorted(candidate.candidate_id for candidate in group))
        units = set().union(*(_scope_units(scopes[candidate_id], unit_documents) for candidate_id in candidate_ids))
        scope = _units_scope(units, unit_documents)
        evidence = _unique_evidence(group)
        identity = {
            "source": source,
            "target": target,
            "category": category,
            "scope": scope.model_dump(mode="json"),
            "note": note,
            "candidate_ids": candidate_ids,
        }
        terms.append(
            FrozenTerm(
                term_id=f"mt-{canonical_hash(identity)[:24]}",
                source=source,
                target=target,
                aliases=(),
                scope=scope,
                mode="preferred",
                match_policy="exact",
                note=note,
                origin="model_extraction",
                candidate_ids=candidate_ids,
                evidence=evidence,
            )
        )
    return tuple(terms)


def _string_list(group: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = group.get(key)
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"conflict group {key} must be a string array")
    return tuple(value)


def _json_group(group: Mapping[str, object]) -> dict[str, JsonValue]:
    return {
        "group_id": str(group["group_id"]),
        "group_input_hash": str(group["group_input_hash"]),
        "candidate_ids": list(_string_list(group, "candidate_ids")),
        "allowed_unit_ids": list(_string_list(group, "allowed_unit_ids")),
        "status": str(group["status"]),
    }


def _unique_evidence(candidates: list[TermCandidate]) -> tuple[TermEvidence, ...]:
    evidence = {
        canonical_hash(item): item
        for candidate in candidates
        for item in candidate.evidence
        if item.evidence_check == "source_matched"
    }
    return tuple(evidence[key] for key in sorted(evidence))


def _scope_units(scope: TermScope | None, unit_documents: dict[str, str]) -> set[str]:
    if scope is None:
        return set()
    if scope.kind == "book":
        return set(unit_documents)
    if scope.kind == "documents":
        return {unit_id for unit_id, document_id in unit_documents.items() if document_id in scope.document_ids}
    return set(scope.unit_ids)


def _units_scope(unit_ids: set[str], unit_documents: dict[str, str]) -> TermScope:
    documents = {unit_documents[unit_id] for unit_id in unit_ids}
    all_document_units = {unit_id for unit_id, document_id in unit_documents.items() if document_id in documents}
    if unit_ids == all_document_units:
        return TermScope(kind="documents", document_ids=tuple(documents))
    return TermScope(kind="units", unit_ids=tuple(unit_ids))


def _warnings(
    extraction_status: Literal["closed", "closed_with_gaps", "disabled", "not_required"],
    candidates: tuple[TermCandidate, ...],
    conflict_groups: tuple[dict[str, JsonValue], ...],
    terms: tuple[FrozenTerm, ...],
) -> tuple[str, ...]:
    warnings: list[str] = []
    if extraction_status == "disabled":
        warnings.append("Automatic terminology extraction was disabled.")
    elif extraction_status == "not_required":
        warnings.append("No extractable source views required terminology preparation.")
    elif extraction_status == "closed_with_gaps":
        warnings.append("Terminology extraction closed with local gaps.")
    rejected = sum(candidate.status.startswith("rejected_") for candidate in candidates)
    deferred = sum(candidate.status == "deferred_conflict" for candidate in candidates)
    if rejected:
        warnings.append(f"{rejected} terminology candidate(s) were rejected.")
    if deferred or any(group.get("status") == "deferred_conflict" for group in conflict_groups):
        warnings.append(f"{deferred} terminology candidate(s) remain deferred by conflict.")
    if not terms:
        warnings.append("No valid terminology rules were available to freeze.")
    return tuple(warnings)


def _coverage(
    records: dict[str, TermExtractionRecord],
    candidates: tuple[TermCandidate, ...],
    conflict_groups: tuple[dict[str, JsonValue], ...],
) -> dict[str, JsonValue]:
    return {
        "items_total": len(records),
        "items_succeeded": sum(
            record.status in {"succeeded", "succeeded_with_rejections"} for record in records.values()
        ),
        "items_failed": sum(record.status in {"failed_exhausted", "unplannable"} for record in records.values()),
        "candidates_total": len(candidates),
        "candidates_adopted": sum(candidate.status == "adopted_preferred" for candidate in candidates),
        "candidates_rejected": sum(candidate.status.startswith("rejected_") for candidate in candidates),
        "candidates_shadowed": sum(candidate.status == "shadowed_by_user" for candidate in candidates),
        "candidates_deferred": sum(candidate.status == "deferred_conflict" for candidate in candidates),
        "conflict_groups": len(conflict_groups),
    }


__all__ = [
    "ResolutionDecision",
    "TermFreezeResult",
    "freeze_terminology",
    "prepare_candidate_pool",
]
