"""Model-free validation and disposition of extracted terminology candidates."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

from engine.schemas.contracts import (
    DocumentPlan,
    ExtractionItem,
    SourceRef,
    TermCandidate,
    TermEvidence,
    TermScope,
    UserTerm,
    canonical_hash,
)

type CandidateCategory = Literal["term", "person", "organization", "product", "abbreviation", "other"]


@dataclass(frozen=True)
class EvidenceProposal:
    view_id: str
    source_quote: str


@dataclass(frozen=True)
class CandidateProposal:
    source: str
    target: str
    category: CandidateCategory
    evidence: tuple[EvidenceProposal, ...]
    aliases: tuple[str, ...] = ()
    scope_hint: Literal["document", "book"] = "document"
    note: str = ""


@dataclass(frozen=True)
class CandidateValidation:
    candidates: tuple[TermCandidate, ...]
    diagnostics: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class CandidateDisposition:
    candidates: tuple[TermCandidate, ...]
    effective_scopes: dict[str, TermScope]
    conflict_groups: tuple[dict[str, object], ...]


def validate_candidate_proposals(
    document: DocumentPlan,
    item: ExtractionItem,
    proposals: tuple[CandidateProposal, ...],
) -> CandidateValidation:
    """Bind model evidence to immutable primary views and reject only bad candidates."""

    if item.document_id != document.document_id:
        raise ValueError("extraction item belongs to another document")
    primary_ranges = _primary_ranges(document, item)
    merged: dict[str, tuple[CandidateProposal, set[EvidenceProposal]]] = {}
    for proposal in proposals:
        if not proposal.source or not proposal.target or not proposal.evidence:
            raise ValueError("candidate proposals require source, target, and evidence")
        aliases = tuple(sorted(set(proposal.aliases)))
        normalized = CandidateProposal(
            source=proposal.source,
            target=proposal.target,
            category=proposal.category,
            evidence=proposal.evidence,
            aliases=aliases,
            scope_hint=proposal.scope_hint,
            note=proposal.note,
        )
        candidate_id = _candidate_id(item, normalized)
        existing = merged.setdefault(candidate_id, (normalized, set()))
        existing[1].update(normalized.evidence)

    candidates: list[TermCandidate] = []
    diagnostics: list[dict[str, object]] = []
    for candidate_id in sorted(merged):
        proposal, raw_evidence = merged[candidate_id]
        evidence: list[TermEvidence] = []
        reasons: list[str] = []
        for raw in sorted(raw_evidence, key=lambda value: (value.view_id, value.source_quote)):
            checked, reason = _validate_evidence(document, item, primary_ranges, proposal.source, raw)
            evidence.append(checked)
            if reason is not None:
                reasons.append(reason)
        valid_quotes = [item.source_quote for item in evidence if item.evidence_check == "source_matched"]
        for alias in proposal.aliases:
            if not any(_has_bounded_occurrence(quote, alias) for quote in valid_quotes):
                reasons.append(f"alias_missing_from_evidence:{alias}")
        status = "rejected_evidence" if reasons else "proposed"
        candidates.append(
            TermCandidate(
                candidate_id=candidate_id,
                extraction_item_id=item.item_id,
                source=proposal.source,
                target=proposal.target,
                category=proposal.category,
                aliases=proposal.aliases,
                scope_hint=proposal.scope_hint,
                note=proposal.note,
                evidence=tuple(evidence),
                status=status,
            )
        )
        diagnostics.extend(
            {"candidate_id": candidate_id, "code": "rejected_evidence", "reason": reason}
            for reason in sorted(set(reasons))
        )
    return CandidateValidation(tuple(candidates), tuple(diagnostics))


def dispose_candidates(
    candidates: tuple[TermCandidate, ...],
    user_terms: tuple[UserTerm, ...],
    *,
    unit_documents: dict[str, str],
) -> CandidateDisposition:
    """Apply user priority, keep automatic scope local, and defer unresolved conflicts."""

    scopes: dict[str, set[str]] = {}
    statuses: dict[str, str] = {}
    for candidate in candidates:
        if candidate.status in {"rejected_evidence", "rejected_schema"}:
            statuses[candidate.candidate_id] = candidate.status
            continue
        source_documents = {
            evidence.document_id
            for evidence in candidate.evidence
            if evidence.evidence_check == "source_matched" and evidence.document_id
        }
        candidate_units = {
            unit_id for unit_id, document_id in unit_documents.items() if document_id in source_documents
        }
        if not candidate_units:
            statuses[candidate.candidate_id] = "rejected_evidence"
            continue
        shadowed_units = set().union(
            *(
                _scope_units(term.scope, unit_documents) & candidate_units
                for term in user_terms
                if _user_matches_candidate(term, candidate)
            ),
            set(),
        )
        remaining = candidate_units - shadowed_units
        if not remaining:
            statuses[candidate.candidate_id] = "shadowed_by_user"
            continue
        scopes[candidate.candidate_id] = remaining
        statuses[candidate.candidate_id] = "adopted_preferred"

    conflicts: list[dict[str, object]] = []
    eligible = [candidate for candidate in candidates if statuses[candidate.candidate_id] == "adopted_preferred"]
    for source in sorted({candidate.source for candidate in eligible}):
        group = [candidate for candidate in eligible if candidate.source == source]
        signatures = {
            candidate.candidate_id: (candidate.target, candidate.category, candidate.aliases, candidate.note)
            for candidate in group
        }
        edges: dict[str, set[str]] = {candidate.candidate_id: set() for candidate in group}
        for index, candidate in enumerate(group):
            for other in group[index + 1 :]:
                if signatures[candidate.candidate_id] != signatures[other.candidate_id] and (
                    scopes[candidate.candidate_id] & scopes[other.candidate_id]
                ):
                    edges[candidate.candidate_id].add(other.candidate_id)
                    edges[other.candidate_id].add(candidate.candidate_id)
        for candidate_ids in _connected_conflicts(edges):
            overlap = set().union(*(scopes[candidate_id] for candidate_id in candidate_ids))
            payload = {
                "source": source,
                "candidate_ids": candidate_ids,
                "allowed_unit_ids": sorted(overlap),
            }
            group_id = f"tcg-{canonical_hash(payload)[:24]}"
            conflicts.append(
                {
                    "group_id": group_id,
                    "group_input_hash": canonical_hash(payload),
                    "candidate_ids": list(candidate_ids),
                    "allowed_unit_ids": sorted(overlap),
                    "status": "deferred_conflict",
                }
            )
            for candidate_id in candidate_ids:
                statuses[candidate_id] = "deferred_conflict"
                scopes.pop(candidate_id, None)

    disposed = tuple(
        candidate.model_copy(update={"status": statuses.get(candidate.candidate_id, candidate.status)})
        for candidate in sorted(candidates, key=lambda value: value.candidate_id)
    )
    effective_scopes = {
        candidate_id: _units_scope(unit_ids, unit_documents) for candidate_id, unit_ids in sorted(scopes.items())
    }
    return CandidateDisposition(disposed, effective_scopes, tuple(conflicts))


def _candidate_id(item: ExtractionItem, proposal: CandidateProposal) -> str:
    payload = {
        "extraction_item_id": item.item_id,
        "source": proposal.source,
        "target": proposal.target,
        "category": proposal.category,
        "aliases": list(proposal.aliases),
        "scope": {"kind": "documents", "document_ids": [item.document_id]},
        "match_policy": "exact",
        "note": proposal.note,
    }
    return f"tc-{canonical_hash(payload)[:24]}"


def _validate_evidence(
    document: DocumentPlan,
    item: ExtractionItem,
    primary_ranges: dict[str, tuple[tuple[int, int], ...]],
    source: str,
    proposal: EvidenceProposal,
) -> tuple[TermEvidence, str | None]:
    rejected = TermEvidence(view_id=proposal.view_id, source_quote=proposal.source_quote, evidence_check="rejected")
    if proposal.view_id not in item.view_ids:
        return rejected, "view_not_primary"
    view = document.source_views.get(proposal.view_id)
    if view is None or view.document_id != item.document_id:
        return rejected, "view_not_found"
    positions = {
        start + position
        for start, end in primary_ranges[proposal.view_id]
        for position in _occurrences(view.text[start:end], proposal.source_quote)
    }
    if len(positions) != 1:
        return rejected, "quote_not_unique_in_primary_ranges" if positions else "quote_not_found_in_primary_range"
    if not _has_bounded_occurrence(proposal.source_quote, source):
        return rejected, "source_not_found_at_boundary"
    start = next(iter(positions))
    refs = _slice_refs(document, view.source_refs, start, start + len(proposal.source_quote), proposal.source_quote)
    if refs is None:
        return rejected, "quote_source_refs_invalid"
    return (
        TermEvidence(
            view_id=view.view_id,
            source_quote=proposal.source_quote,
            unit_id=view.unit_id,
            document_id=view.document_id,
            source_refs=refs,
            evidence_check="source_matched",
        ),
        None,
    )


def _primary_ranges(document: DocumentPlan, item: ExtractionItem) -> dict[str, tuple[tuple[int, int], ...]]:
    grouped: dict[str, list[tuple[int, int]]] = {}
    for interval in item.primary_ranges:
        view_id = interval.get("view_id")
        start, end = interval.get("start"), interval.get("end")
        if not isinstance(view_id, str) or not isinstance(start, int) or isinstance(start, bool):
            raise TypeError("primary range requires string view_id and integer start/end")
        if not isinstance(end, int) or isinstance(end, bool):
            raise TypeError("primary range requires string view_id and integer start/end")
        view = document.source_views.get(view_id)
        if view is None or view.document_id != item.document_id or not 0 <= start < end <= len(view.text):
            raise ValueError(f"invalid primary range for view {view_id!r}")
        grouped.setdefault(view_id, []).append((start, end))
    if set(grouped) != set(item.view_ids):
        raise ValueError("primary range view IDs must exactly match extraction item view_ids")
    result: dict[str, tuple[tuple[int, int], ...]] = {}
    for view_id, ranges in grouped.items():
        ordered = sorted(ranges)
        if any(current[0] < previous[1] for previous, current in pairwise(ordered)):
            raise ValueError(f"primary ranges overlap for view {view_id!r}")
        result[view_id] = tuple(ordered)
    return result


def _slice_refs(
    document: DocumentPlan,
    refs: tuple[SourceRef, ...],
    start: int,
    end: int,
    expected_text: str,
) -> tuple[SourceRef, ...] | None:
    result: list[SourceRef] = []
    cursor = 0
    rebuilt: list[str] = []
    for ref in refs:
        slot = document.source_slots[ref.slot_id]
        length = ref.end - ref.start
        overlap_start = max(start, cursor)
        overlap_end = min(end, cursor + length)
        if overlap_start < overlap_end:
            source_start = ref.start + overlap_start - cursor
            source_end = ref.start + overlap_end - cursor
            result.append(SourceRef(slot_id=ref.slot_id, start=source_start, end=source_end))
            rebuilt.append(slot.source_value[source_start:source_end])
        cursor += length
    if cursor < end or "".join(rebuilt) != expected_text:
        return None
    return tuple(result)


def _connected_conflicts(edges: dict[str, set[str]]) -> tuple[tuple[str, ...], ...]:
    unseen = {candidate_id for candidate_id, neighbors in edges.items() if neighbors}
    groups: list[tuple[str, ...]] = []
    while unseen:
        pending = [min(unseen)]
        component: set[str] = set()
        while pending:
            candidate_id = pending.pop()
            if candidate_id in component:
                continue
            component.add(candidate_id)
            pending.extend(edges[candidate_id] - component)
        unseen -= component
        groups.append(tuple(sorted(component)))
    return tuple(groups)


def _occurrences(text: str, phrase: str) -> tuple[int, ...]:
    if not phrase:
        return ()
    found: list[int] = []
    start = 0
    while (position := text.find(phrase, start)) >= 0:
        found.append(position)
        start = position + 1
    return tuple(found)


def _has_bounded_occurrence(text: str, phrase: str) -> bool:
    for start in _occurrences(text, phrase):
        end = start + len(phrase)
        left_ok = not _word_char(phrase[0]) or start == 0 or not _word_char(text[start - 1])
        right_ok = not _word_char(phrase[-1]) or end == len(text) or not _word_char(text[end])
        if left_ok and right_ok:
            return True
    return False


def _word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _scope_units(scope: TermScope, unit_documents: dict[str, str]) -> set[str]:
    if scope.kind == "book":
        return set(unit_documents)
    if scope.kind == "documents":
        return {unit_id for unit_id, document_id in unit_documents.items() if document_id in scope.document_ids}
    return set(scope.unit_ids)


def _user_matches_candidate(term: UserTerm, candidate: TermCandidate) -> bool:
    user_writings = (term.source, *term.aliases)
    candidate_writings = (candidate.source, *candidate.aliases)
    if term.match_policy == "casefold":
        return bool({value.casefold() for value in user_writings} & {value.casefold() for value in candidate_writings})
    return bool(set(user_writings) & set(candidate_writings))


def _units_scope(unit_ids: set[str], unit_documents: dict[str, str]) -> TermScope:
    documents = {unit_documents[unit_id] for unit_id in unit_ids}
    document_units = {unit_id for unit_id, document_id in unit_documents.items() if document_id in documents}
    if unit_ids == document_units:
        return TermScope(kind="documents", document_ids=tuple(documents))
    return TermScope(kind="units", unit_ids=tuple(unit_ids))


__all__ = [
    "CandidateDisposition",
    "CandidateProposal",
    "CandidateValidation",
    "EvidenceProposal",
    "dispose_candidates",
    "validate_candidate_proposals",
]
