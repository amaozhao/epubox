from __future__ import annotations

from engine.item.extractor import extract_document
from engine.schemas.contracts import (
    TermExtractionRecord,
    TermPreparation,
    TermScope,
    UserTerm,
    canonical_hash,
    glossary_rules_hash,
)
from engine.services.term_candidates import CandidateProposal, EvidenceProposal, validate_candidate_proposals
from engine.services.term_freeze import ResolutionDecision, freeze_terminology, prepare_candidate_pool
from engine.services.term_planning import plan_term_extraction

EXTRACTION_IDENTITY = {
    "strategy": "chapter-windows",
    "prompt_version": "epubox-terms-1",
    "model": "test-model",
    "target_language": "zh-Hans",
}


def _document(name: str, paragraphs: tuple[str, ...]):
    body = "".join(f"<p>{text}</p>" for text in paragraphs)
    source = f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{name}</title></head><body>{body}</body></html>'
    return extract_document(source, f"OPS/{name}.xhtml", "source-sha")


def _plan(documents, user_terms=(), *, auto_extract: bool = True):
    return plan_term_extraction(
        documents,
        user_terms,
        source_hash="source-sha",
        preparation_hash="prep-hash",
        auto_extract=auto_extract,
        max_primary_chars=10_000,
        extraction_identity=EXTRACTION_IDENTITY,
    ).plan


def _candidate(document, item, source: str, target: str, *, aliases: tuple[str, ...] = ()):
    view = next(
        document.source_views[view_id] for view_id in item.view_ids if source in document.source_views[view_id].text
    )
    return validate_candidate_proposals(
        document,
        item,
        (
            CandidateProposal(
                source=source,
                target=target,
                category="term",
                aliases=aliases,
                evidence=(EvidenceProposal(view.view_id, view.text),),
            ),
        ),
    ).candidates[0]


def _records(plan, documents, targets=()):
    documents_by_id = {document.document_id: document for document in documents}
    target_by_document = {document.document_id: (source, target) for document, source, target in targets}
    records = {}
    for item in plan.items:
        candidates = ()
        if item.document_id in target_by_document:
            source, target = target_by_document[item.document_id]
            document = documents_by_id[item.document_id]
            if any(source in document.source_views[view_id].text for view_id in item.view_ids):
                candidates = (_candidate(document, item, source, target),)
        records[item.item_id] = TermExtractionRecord(
            item_id=item.item_id,
            document_id=item.document_id,
            view_ids=item.view_ids,
            extraction_input_hash=item.extraction_input_hash,
            status="succeeded",
            candidates=candidates,
            request_ids=(f"request-{item.item_id}",),
        )
    return records


def _unit_documents(documents):
    return {unit.unit_id: document.document_id for document in documents for unit in document.units}


def test_freeze_merges_same_sense_across_windows_only_in_frozen_term() -> None:
    first = _document("one", ("Memory allocation is fast.",))
    second = _document("two", ("Memory recovery is safe.",))
    documents = (first, second)
    plan = _plan(documents)
    records = _records(plan, documents, ((first, "Memory", "内存"), (second, "Memory", "内存")))
    units = _unit_documents(documents)

    result = freeze_terminology(
        plan,
        records,
        (),
        units,
        documents,
        extraction_config_hash="extract-config",
    )
    repeated = freeze_terminology(
        plan,
        records,
        (),
        units,
        documents,
        extraction_config_hash="extract-config",
    )

    assert result == repeated
    assert len(result.candidate_pool.candidates) == 2
    assert all(len(candidate.evidence) == 1 for candidate in result.candidate_pool.candidates)
    assert all(candidate.status == "adopted_preferred" for candidate in result.candidate_pool.candidates)
    assert len(result.glossary.terms) == 1
    term = result.glossary.terms[0]
    assert len(term.candidate_ids) == len(term.evidence) == 2
    assert term.scope == TermScope(kind="documents", document_ids=(first.document_id, second.document_id))
    assert result.freeze_intent.candidate_pool_hash == canonical_hash(result.candidate_pool)
    assert result.freeze_intent.rules_hash == glossary_rules_hash(result.glossary.terms)
    TermPreparation(
        plan=plan,
        records=records,
        candidates=result.candidate_pool,
        unit_documents=units,
        freeze=result.freeze_intent,
    )


def test_frequency_counts_primary_source_once_across_overlapping_user_aliases() -> None:
    document = _document("frequency", ("Memory memory Memory.",))
    user = UserTerm(
        term_id="ut-memory",
        source="Memory",
        target="内存",
        aliases=("memory",),
        match_policy="casefold",
        scope=TermScope(kind="book"),
    )
    plan = _plan((document,), (user,))
    records = _records(plan, (document,))
    result = freeze_terminology(
        plan,
        records,
        (user,),
        _unit_documents((document,)),
        (document,),
        extraction_config_hash="extract-config",
    )

    term = result.glossary.terms[0]
    assert term.frequency == 3
    assert glossary_rules_hash((term,)) == glossary_rules_hash((term.model_copy(update={"frequency": 99}),))
    assert canonical_hash(term) != canonical_hash(term.model_copy(update={"frequency": 99}))


def test_frequency_merges_chain_of_overlapping_source_ranges() -> None:
    document = _document("overlap", ("Symbols @#$%^&amp;*()!+ are used.",))
    user = UserTerm(
        term_id="ut-symbols",
        source="@#$%^",
        target="符号",
        aliases=("%^&*()!", "()!+"),
        scope=TermScope(kind="book"),
    )
    plan = _plan((document,), (user,))
    result = freeze_terminology(
        plan,
        _records(plan, (document,)),
        (user,),
        _unit_documents((document,)),
        (document,),
        extraction_config_hash="extract-config",
    )
    assert result.glossary.terms[0].frequency == 1


def test_unresolved_conflict_defers_all_candidates_and_resolution_selects_existing_target() -> None:
    document = _document("one", ("Memory allocation is fast.",))
    documents = (document,)
    plan = _plan(documents)
    item = next(
        item
        for item in plan.items
        if any("Memory" in document.source_views[view_id].text for view_id in item.view_ids)
    )
    first = _candidate(document, item, "Memory", "内存")
    second = _candidate(document, item, "Memory", "记忆")
    records = _records(plan, documents)
    records[item.item_id] = records[item.item_id].model_copy(update={"candidates": (first, second)})
    units = _unit_documents(documents)

    open_pool = prepare_candidate_pool(plan, records, (), units, documents)
    assert open_pool.extraction_status == "open"
    assert {candidate.status for candidate in open_pool.candidates} == {"deferred_conflict"}
    group_id = str(open_pool.conflict_groups[0]["group_id"])
    unresolved = freeze_terminology(
        plan,
        records,
        (),
        units,
        documents,
        extraction_config_hash="extract-config",
        candidate_pool_version=1,
        resolution_response_ids=("failed-resolution-response",),
    )
    assert {candidate.status for candidate in unresolved.candidate_pool.candidates} == {"deferred_conflict"}
    assert unresolved.candidate_pool.conflict_groups[0]["decision"] == "defer"
    selected = freeze_terminology(
        plan,
        records,
        (),
        units,
        documents,
        extraction_config_hash="extract-config",
        resolution_decisions=(
            ResolutionDecision(
                group_id=group_id,
                decision="select",
                selected_candidate_ids=(first.candidate_id,),
                reason="The source uses the technical sense.",
            ),
        ),
        candidate_pool_version=1,
        resolution_response_ids=("resolution-response-1",),
    )

    assert {candidate.status for candidate in selected.candidate_pool.candidates} == {
        "adopted_preferred",
        "deferred_conflict",
    }
    assert selected.glossary.terms[0].target == "内存"
    assert selected.candidate_pool.conflict_groups[0]["status"] == "resolved"
    assert selected.candidate_pool.conflict_groups[0]["group_id"] == group_id
    assert selected.candidate_pool.record_version == 1
    assert "resolution-response-1" in selected.candidate_pool.consumed_response_ids
    TermPreparation(
        plan=plan,
        records=records,
        candidates=selected.candidate_pool,
        unit_documents=units,
        freeze=selected.freeze_intent,
    )


def test_user_partial_scope_is_preserved_without_expanding_or_losing_remaining_units() -> None:
    document = _document("one", ("Memory allocation is fast.", "Memory recovery is safe."))
    documents = (document,)
    paragraph_units = [unit for unit in document.units if unit.kind == "paragraph"]
    user = UserTerm(
        term_id="ut-memory",
        source="Memory",
        target="记忆",
        scope=TermScope(kind="units", unit_ids=(paragraph_units[0].unit_id,)),
    )
    plan = _plan(documents, (user,))
    records = _records(plan, documents, ((document, "Memory", "内存"),))
    units = _unit_documents(documents)

    result = freeze_terminology(
        plan,
        records,
        (user,),
        units,
        documents,
        extraction_config_hash="extract-config",
    )

    model_term = next(term for term in result.glossary.terms if term.origin == "model_extraction")
    assert model_term.scope.kind == "units"
    assert paragraph_units[0].unit_id not in model_term.scope.unit_ids
    assert paragraph_units[1].unit_id in model_term.scope.unit_ids
    assert next(term for term in result.glossary.terms if term.origin == "user").term_id == user.term_id
    TermPreparation(
        plan=plan,
        records=records,
        candidates=result.candidate_pool,
        unit_documents=units,
        freeze=result.freeze_intent,
    )


def test_closed_with_gaps_and_empty_closed_runs_have_explicit_warnings() -> None:
    first = _document("one", ("Memory allocation is fast.",))
    second = _document("two", ("No terminology here.",))
    documents = (first, second)
    plan = _plan(documents)
    records = _records(plan, documents)
    failed_id = next(item.item_id for item in plan.items if item.document_id == second.document_id)
    records[failed_id] = records[failed_id].model_copy(update={"status": "failed_exhausted"})

    result = freeze_terminology(
        plan,
        records,
        (),
        _unit_documents(documents),
        documents,
        extraction_config_hash="extract-config",
    )

    assert result.glossary.extraction_status == "closed_with_gaps"
    assert any("local gaps" in warning for warning in result.glossary.warnings)
    assert any("No valid terminology" in warning for warning in result.glossary.warnings)


def test_unconfirmed_automatic_alias_is_reported_but_not_activated() -> None:
    document = _document("one", ("Memory (RAM) allocation is fast.",))
    documents = (document,)
    plan = _plan(documents)
    item = next(
        item
        for item in plan.items
        if any("Memory" in document.source_views[view_id].text for view_id in item.view_ids)
    )
    candidate = _candidate(document, item, "Memory", "内存", aliases=("RAM",))
    records = _records(plan, documents)
    records[item.item_id] = records[item.item_id].model_copy(update={"candidates": (candidate,)})

    result = freeze_terminology(
        plan,
        records,
        (),
        _unit_documents(documents),
        documents,
        extraction_config_hash="extract-config",
    )

    assert result.glossary.terms[0].aliases == ()
    assert result.freeze_intent.coverage["aliases_deferred"] == 1
    assert any("1 automatic alias" in warning for warning in result.glossary.warnings)


def test_disabled_and_not_required_freeze_to_distinct_empty_snapshots() -> None:
    document = _document("one", ("Text without a request.",))
    disabled_plan = _plan((document,), auto_extract=False)
    disabled = freeze_terminology(
        disabled_plan,
        {},
        (),
        _unit_documents((document,)),
        (document,),
        extraction_config_hash="extract-config",
    )
    not_required_plan = _plan(())
    not_required = freeze_terminology(
        not_required_plan,
        {},
        (),
        {},
        (),
        extraction_config_hash="extract-config",
    )

    assert disabled.glossary.extraction_status == "disabled"
    assert not_required.glossary.extraction_status == "not_required"
    assert disabled.glossary.warnings != not_required.glossary.warnings
