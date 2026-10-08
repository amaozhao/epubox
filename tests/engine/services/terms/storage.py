from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from engine.agents.runtime import RESOLUTION_PROTOCOL_VERSION, TERM_PROMPT_VERSION
from engine.item.atoms import extract_resource
from engine.schemas.budget import BudgetLimits
from engine.schemas.contracts import PreparationPlan, canonical_hash
from engine.services.atomic import CorruptRecord, IdentityMismatch
from engine.services.preflight import prepare_preflight
from engine.services.store import RunStore
from engine.services.terms.freeze import freeze_terminology
from engine.services.terms.planning import ATOMIC_TERM_PLANNER_VERSION, plan_atomic_terms
from engine.services.terms.runner import TermRunner
from engine.services.terms.storage import write_plan
from tests.engine.epub.factory import make_epub


def atomic_plan(tmp_path: Path):
    source = make_epub(tmp_path / "input.epub", {"chapter.xhtml": "<p>One.</p><p>Two.</p>"})
    root = tmp_path / "work"
    store = RunStore(root)
    snapshot = root / "source.epub"
    snapshot.write_bytes(source.read_bytes())
    source_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    with zipfile.ZipFile(snapshot) as archive:
        raw = archive.read("OEBPS/chapter.xhtml")
    inventory = extract_resource(raw, "OEBPS/chapter.xhtml", source_hash)
    document_hash = store.write_document(inventory.document)
    store.write_user_terms(())
    extraction = {
        "auto_extract": True,
        "strategy": ATOMIC_TERM_PLANNER_VERSION,
        "prompt_version": TERM_PROMPT_VERSION,
        "resolution_protocol_version": RESOLUTION_PROTOCOL_VERSION,
        "model": "gpt-3.5-turbo",
        "target_language": "zh-Hans",
        "max_output_tokens": 4_096,
    }
    translation = {
        "model": "gpt-3.5-turbo",
        "max_source_tokens": 5_000,
        "max_input_tokens": 32_768,
        "max_output_tokens": 4_096,
        "context_tokens": 32_768,
    }
    preparation = PreparationPlan(
        source_hash=source_hash,
        source_path="source.epub",
        source_epub_version="3.0",
        run_id="run",
        document_hashes={inventory.document.document_id: document_hash},
        reading_order=(inventory.document.document_id,),
        unit_documents={unit.unit_id: inventory.document.document_id for unit in inventory.document.units},
        user_terms_hash=canonical_hash(()),
        extraction_config=extraction,
        translation_config=translation,
    )
    preparation_hash = store.write_preparation(preparation)
    prepare_preflight(
        store,
        BudgetLimits(source_tokens=5_000, input_tokens=32_768, output_tokens=4_096, context_tokens=32_768),
        "gpt-3.5-turbo",
    )
    plan = plan_atomic_terms(
        (inventory,),
        (),
        source_hash=source_hash,
        preparation_hash=preparation_hash,
        extraction_identity=extraction,
    ).plan
    return store, inventory, plan


def test_atomic_plan_executes_and_freezes_against_its_committed_documents(tmp_path: Path) -> None:
    store, inventory, plan = atomic_plan(tmp_path)
    assert store.write_term_plan(plan)

    async def transport(_kind, payload):
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": item["item_id"], "candidates": []} for item in payload["items"]],
                }
            ),
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    result = asyncio.run(TermRunner(store, transport=transport).run())
    preparation = store.read_preparation()
    records = {item.item_id: store.read_extraction(item.item_id) for item in plan.items}
    frozen = freeze_terminology(
        plan,
        records,
        (),
        preparation.unit_documents,
        (inventory.document,),
        extraction_config_hash=canonical_hash(preparation.extraction_config),
    )
    store.save_candidate_pool(frozen.candidate_pool)
    store.write_freeze(frozen.freeze_intent)
    store.write_glossary(frozen.glossary)

    assert result.status == "closed"
    assert all(record.status == "succeeded" for record in records.values())
    assert store._trusted_frozen_glossary(preparation) == (frozen.freeze_intent, frozen.glossary)


def test_verified_inventories_avoid_revalidating_a_fresh_term_plan(tmp_path: Path, monkeypatch) -> None:
    store, inventory, plan = atomic_plan(tmp_path)
    preparation = store.read_preparation()
    forged = plan_atomic_terms(
        (inventory,),
        (),
        source_hash=preparation.source_hash,
        preparation_hash=plan.preparation_hash,
        max_primary_chars=1,
        extraction_identity=preparation.extraction_config,
    ).plan

    def forbidden(*_args, **_kwargs):
        raise AssertionError("verified inventories must be reused")

    monkeypatch.setattr("engine.services.terms.storage.atomic_documents", forbidden)
    monkeypatch.setattr("engine.services.terms.storage.canonical_documents", forbidden)

    with pytest.raises(IdentityMismatch, match="deterministic atomic coverage"):
        write_plan(store, forged, verified_inventories=(inventory,))
    assert write_plan(store, plan, verified_inventories=(inventory,))


def test_atomic_plan_is_rejected_without_its_preflight_receipt(tmp_path: Path) -> None:
    store, _inventory, plan = atomic_plan(tmp_path)
    (store.root / "checks" / "preflight.json").unlink()

    with pytest.raises(CorruptRecord, match="preflight.json"):
        store.write_term_plan(plan)
    assert not (store.root / "glossary" / "plan.json").exists()


def test_saved_atomic_response_replays_without_receipt_or_new_http(tmp_path: Path) -> None:
    store, _inventory, plan = atomic_plan(tmp_path)
    store.write_term_plan(plan)

    async def first(_kind, payload):
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": item["item_id"], "candidates": []} for item in payload["items"]],
                }
            ),
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    assert asyncio.run(TermRunner(store, transport=first).run()).status == "closed"
    for item in plan.items:
        record = store.read_extraction(item.item_id)
        store._path("glossary/extraction", item.item_id).unlink()
        store.save_extraction(
            record.model_copy(
                update={
                    "record_version": 0,
                    "status": "pending",
                    "candidates": (),
                    "rejections": (),
                    "diagnostics": (),
                }
            )
        )
    (store.root / "checks" / "preflight.json").unlink()
    calls = 0

    async def forbidden(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("saved responses must replay before the paid dispatch guard")

    replayed = asyncio.run(TermRunner(store, transport=forbidden).run())

    assert replayed.status == "closed" and calls == 0
    assert all(store.read_extraction(item.item_id).status == "succeeded" for item in plan.items)


def test_atomic_plan_is_recomputed_instead_of_trusting_caller_ranges(tmp_path: Path) -> None:
    store, inventory, plan = atomic_plan(tmp_path)
    preparation = store.read_preparation()
    forged = plan_atomic_terms(
        (inventory,),
        (),
        source_hash=preparation.source_hash,
        preparation_hash=plan.preparation_hash,
        max_primary_chars=1,
        extraction_identity=preparation.extraction_config,
    ).plan

    with pytest.raises(IdentityMismatch, match="deterministic atomic coverage"):
        store.write_term_plan(forged)
    assert not (store.root / "glossary" / "plan.json").exists()
