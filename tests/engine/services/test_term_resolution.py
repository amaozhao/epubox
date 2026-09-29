from __future__ import annotations

import asyncio
import json
from pathlib import Path

from engine.schemas.contracts import TermExtractionRecord
from engine.services.term_candidates import CandidateProposal, EvidenceProposal, validate_candidate_proposals
from engine.services.term_freeze import prepare_candidate_pool
from engine.services.term_resolution import TermResolutionRunner
from tests.engine.services.test_term_runner import _prepare


def test_one_bounded_resolution_selects_only_existing_candidate(tmp_path: Path) -> None:
    store, _ = _prepare(tmp_path)
    plan = store.read_term_plan()
    prep = store.read_preparation()
    documents = tuple(store.read_document(document_id) for document_id in prep.document_hashes)
    records = {}
    seeded = False
    for item in plan.items:
        document = next(document for document in documents if document.document_id == item.document_id)
        proposals = ()
        for view_id in item.view_ids:
            view = document.source_views[view_id]
            if not seeded and "Memory" in view.text:
                proposals = (
                    CandidateProposal(
                        source="Memory",
                        target="内存",
                        category="term",
                        evidence=(EvidenceProposal(view_id, view.text),),
                    ),
                    CandidateProposal(
                        source="Memory",
                        target="记忆",
                        category="term",
                        evidence=(EvidenceProposal(view_id, view.text),),
                    ),
                )
                seeded = True
                break
        candidates = validate_candidate_proposals(document, item, proposals).candidates
        record = TermExtractionRecord(
            item_id=item.item_id,
            document_id=item.document_id,
            view_ids=item.view_ids,
            extraction_input_hash=item.extraction_input_hash,
            status="succeeded",
            candidates=candidates,
        )
        records[item.item_id] = store.save_extraction(record)
    pool = prepare_candidate_pool(plan, records, (), prep.unit_documents, documents)
    store.save_candidate_pool(pool)
    assert len(pool.conflict_groups) >= 1
    calls: list[str] = []

    async def transport(kind, payload):
        assert kind == "resolution"
        calls.append(payload["group_id"])
        chosen = next(
            candidate["candidate_id"] for candidate in payload["candidates"] if candidate["target"] == "内存"
        )
        raw = json.dumps(
            {
                "protocol": "epubox-term-resolution-1",
                "request_id": payload["request_id"],
                "group_id": payload["group_id"],
                "decision": "select",
                "selected_candidate_ids": [chosen],
                "reason": "The technical sense matches the source.",
            }
        )
        return {"raw": raw, "usage": {"input_tokens": 1, "output_tokens": 1}}

    result = asyncio.run(TermResolutionRunner(store, transport=transport).run())
    assert result.status == "closed"
    assert result.selected == len(calls) == 1
    assert store.read_candidate_pool().record_version == 1
    assert TermResolutionRunner(store, transport=transport).decisions()[0].decision == "select"
