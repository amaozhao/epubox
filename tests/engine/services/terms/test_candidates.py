from __future__ import annotations

from engine.item.extractor import extract_document
from engine.schemas.contracts import ExtractionItem, TermExtractionRecord, TermScope, UserTerm
from engine.services.terms.candidates import (
    CandidateProposal,
    EvidenceProposal,
    dispose_candidates,
    validate_candidate_proposals,
)


def _document():
    source = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Systems</title></head><body>'
        "<div><p>Use pro<em>cess</em>or scheduling safely.</p></div>"
        "<div><p>A category contains items.</p></div>"
        "</body></html>"
    )
    return extract_document(source, "OPS/chapter.xhtml", "source-sha")


def _item(document, *view_ids: str) -> ExtractionItem:
    return ExtractionItem(
        item_id="te-1",
        document_id=document.document_id,
        view_ids=tuple(view_ids),
        primary_ranges=tuple(
            {"view_id": view_id, "start": 0, "end": len(document.source_views[view_id].text)} for view_id in view_ids
        ),
        extraction_input_hash="input-hash",
    )


def _paragraph_views(document):
    paragraphs = [unit for unit in document.units if unit.kind == "paragraph"]
    return [document.source_views[unit.source_view_ids[0]] for unit in paragraphs]


def test_valid_evidence_binds_cross_format_quote_to_exact_source_ranges() -> None:
    document = _document()
    view = _paragraph_views(document)[0]
    item = _item(document, view.view_id)
    result = validate_candidate_proposals(
        document,
        item,
        (
            CandidateProposal(
                source="processor",
                target="处理器",
                category="term",
                scope_hint="book",
                evidence=(EvidenceProposal(view.view_id, "Use processor scheduling safely."),),
            ),
        ),
    )

    candidate = result.candidates[0]
    evidence = candidate.evidence[0]
    assert candidate.status == "proposed"
    assert evidence.evidence_check == "source_matched"
    assert evidence.unit_id == view.unit_id and evidence.document_id == document.document_id
    assert len(evidence.source_refs) == 3
    assert (
        "".join(document.source_slots[ref.slot_id].source_value[ref.start : ref.end] for ref in evidence.source_refs)
        == "Use processor scheduling safely."
    )
    assert result.diagnostics == ()


def test_rejected_unknown_view_is_auditable_but_cannot_become_proposed() -> None:
    document = _document()
    view = _paragraph_views(document)[0]
    item = _item(document, view.view_id)
    result = validate_candidate_proposals(
        document,
        item,
        (
            CandidateProposal(
                source="processor",
                target="处理器",
                category="term",
                evidence=(EvidenceProposal("sv-unknown", "processor"),),
            ),
        ),
    )
    record_data = {
        "item_id": item.item_id,
        "document_id": item.document_id,
        "view_ids": item.view_ids,
        "extraction_input_hash": item.extraction_input_hash,
        "status": "succeeded_with_rejections",
        "candidates": result.candidates,
    }
    record = TermExtractionRecord.model_validate(record_data)
    assert record.candidates[0].status == "rejected_evidence"
    try:
        TermExtractionRecord.model_validate(
            record_data | {"candidates": (result.candidates[0].model_copy(update={"status": "proposed"}),)}
        )
    except ValueError as error:
        assert "unknown extraction view" in str(error)
    else:
        raise AssertionError("unknown evidence was promoted to a usable candidate")


def test_bad_evidence_and_word_boundary_reject_only_the_bad_candidates() -> None:
    document = _document()
    first, second = _paragraph_views(document)
    item = _item(document, first.view_id, second.view_id)
    title = next(view for view in document.source_views.values() if view.text == "Systems")
    result = validate_candidate_proposals(
        document,
        item,
        (
            CandidateProposal(
                source="processor",
                target="处理器",
                category="term",
                evidence=(EvidenceProposal(first.view_id, first.text),),
            ),
            CandidateProposal(
                source="invented",
                target="虚构",
                category="term",
                evidence=(EvidenceProposal(first.view_id, "This sentence does not exist."),),
            ),
            CandidateProposal(
                source="cat",
                target="猫",
                category="term",
                evidence=(EvidenceProposal(second.view_id, second.text),),
            ),
            CandidateProposal(
                source="Systems",
                target="系统",
                category="term",
                evidence=(EvidenceProposal(title.view_id, title.text),),
            ),
            CandidateProposal(
                source="processor",
                target="处理器",
                category="term",
                aliases=("CPU",),
                evidence=(EvidenceProposal(first.view_id, first.text),),
            ),
        ),
    )

    assert [candidate.status for candidate in result.candidates].count("proposed") == 1
    assert [candidate.status for candidate in result.candidates].count("rejected_evidence") == 4
    assert {entry["reason"] for entry in result.diagnostics} == {
        "alias_missing_from_evidence:CPU",
        "quote_not_found_in_primary_range",
        "source_not_found_at_boundary",
        "view_not_primary",
    }


def test_evidence_is_limited_to_the_items_exact_ranges_of_a_shared_view() -> None:
    document = extract_document(
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Book</title></head>'
        "<body><p>memory first; memory second.</p></body></html>",
        "OPS/chapter.xhtml",
        "source-sha",
    )
    view = _paragraph_views(document)[0]
    split = len("memory first;")
    first_range = ExtractionItem(
        item_id="te-first",
        document_id=document.document_id,
        view_ids=(view.view_id,),
        primary_ranges=({"view_id": view.view_id, "start": 0, "end": split},),
        extraction_input_hash="first-hash",
    )
    result = validate_candidate_proposals(
        document,
        first_range,
        (
            CandidateProposal(
                source="memory",
                target="内存",
                category="term",
                evidence=(EvidenceProposal(view.view_id, "memory"),),
            ),
            CandidateProposal(
                source="memory",
                target="记忆",
                category="term",
                evidence=(EvidenceProposal(view.view_id, "memory second."),),
            ),
        ),
    )

    assert {candidate.status for candidate in result.candidates} == {"proposed", "rejected_evidence"}
    matched = next(candidate for candidate in result.candidates if candidate.status == "proposed").evidence[0]
    assert matched.source_refs[0].start == 0
    assert {diagnostic["reason"] for diagnostic in result.diagnostics} == {"quote_not_found_in_primary_range"}


def test_multiple_ranges_for_one_view_use_only_their_real_positions() -> None:
    document = extract_document(
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Book</title></head>'
        "<body><p>memory first; gap; memory second.</p></body></html>",
        "OPS/chapter.xhtml",
        "source-sha",
    )
    view = _paragraph_views(document)[0]
    second = view.text.index("memory", 1)
    item = ExtractionItem(
        item_id="te-parts",
        document_id=document.document_id,
        view_ids=(view.view_id,),
        primary_ranges=(
            {"view_id": view.view_id, "start": 0, "end": len("memory first;")},
            {"view_id": view.view_id, "start": second, "end": len(view.text)},
        ),
        extraction_input_hash="parts-hash",
    )
    result = validate_candidate_proposals(
        document,
        item,
        (
            CandidateProposal(
                source="memory",
                target="内存",
                category="term",
                evidence=(EvidenceProposal(view.view_id, "memory"),),
            ),
        ),
    )

    assert result.candidates[0].status == "rejected_evidence"
    assert result.diagnostics[0]["reason"] == "quote_not_unique_in_primary_ranges"


def test_candidate_identity_and_evidence_merge_are_idempotent() -> None:
    document = _document()
    view = _paragraph_views(document)[0]
    item = _item(document, view.view_id)
    proposal = CandidateProposal(
        source="processor",
        target="处理器",
        category="term",
        evidence=(EvidenceProposal(view.view_id, view.text), EvidenceProposal(view.view_id, view.text)),
    )

    first = validate_candidate_proposals(document, item, (proposal, proposal))
    second = validate_candidate_proposals(document, item, (proposal,))

    assert first == second
    assert len(first.candidates) == len(first.candidates[0].evidence) == 1


def test_user_scope_wins_only_where_it_applies_and_never_expands_auto_scope() -> None:
    document = _document()
    first, second = _paragraph_views(document)
    item = _item(document, first.view_id)
    candidate = validate_candidate_proposals(
        document,
        item,
        (
            CandidateProposal(
                source="processor",
                target="处理器",
                category="term",
                scope_hint="book",
                evidence=(EvidenceProposal(first.view_id, first.text),),
            ),
        ),
    ).candidates[0]
    user = UserTerm(
        term_id="ut-1",
        source="processor",
        target="处理单元",
        scope=TermScope(kind="units", unit_ids=(first.unit_id,)),
    )
    unit_documents = {first.unit_id: document.document_id, second.unit_id: document.document_id}

    unrestricted = dispose_candidates((candidate,), (), unit_documents=unit_documents)
    assert unrestricted.effective_scopes[candidate.candidate_id] == TermScope(
        kind="documents", document_ids=(document.document_id,)
    )
    result = dispose_candidates((candidate,), (user,), unit_documents=unit_documents)

    assert result.candidates[0].status == "adopted_preferred"
    assert result.effective_scopes[candidate.candidate_id] == TermScope(kind="units", unit_ids=(second.unit_id,))
    book_user = user.model_copy(update={"scope": TermScope(kind="book")})
    shadowed = dispose_candidates((candidate,), (book_user,), unit_documents=unit_documents)
    assert shadowed.candidates[0].status == "shadowed_by_user"
    assert candidate.candidate_id not in shadowed.effective_scopes


def test_conflicting_automatic_targets_are_deferred_without_choosing_a_winner() -> None:
    document = _document()
    view = _paragraph_views(document)[0]
    item = _item(document, view.view_id)
    candidates = validate_candidate_proposals(
        document,
        item,
        (
            CandidateProposal(
                source="processor",
                target="处理器",
                category="term",
                evidence=(EvidenceProposal(view.view_id, view.text),),
            ),
            CandidateProposal(
                source="processor",
                target="处理单元",
                category="term",
                evidence=(EvidenceProposal(view.view_id, view.text),),
            ),
        ),
    ).candidates
    units = {unit.unit_id: document.document_id for unit in document.units}

    result = dispose_candidates(candidates, (), unit_documents=units)

    assert {candidate.status for candidate in result.candidates} == {"deferred_conflict"}
    assert len(result.conflict_groups) == 1
    assert result.effective_scopes == {}
