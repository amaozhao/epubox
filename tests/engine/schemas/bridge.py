from __future__ import annotations

import pytest
from pydantic import ValidationError

from engine.agents.runtime import wire_hash
from engine.item.inline import ProjectionError, validate_projection
from engine.schemas.bridge import (
    BATCH_FORMAT,
    MAP_FORMAT,
    PREPARED_FORMAT,
    AtomicItem,
    ByteSpan,
    PreparedInput,
    RequestBatch,
    SourceLocation,
    SourceMap,
    batch_item_hash,
)
from engine.schemas.budget import BudgetIdentity, BudgetResult
from engine.schemas.contracts import (
    GlossarySnapshot,
    PreparationPlan,
    SourceRef,
    canonical_hash,
    canonical_json_bytes,
    parse_contract,
)
from tests.engine.schemas.contracts import make_document


def atom() -> AtomicItem:
    unit = make_document().units[0]
    return AtomicItem(
        **unit.model_dump(),
        item_id="i1",
        ordinal=0,
        channel="body",
        atomic_tag="p",
        source_span=ByteSpan(byte_start=12, byte_end=40),
    )


def batch_data(stage: str = "translate", *, source_limit: int = 2000) -> dict:
    member = atom()
    wire_item: dict[str, object] = {"item_id": member.item_id, "source": member.source_projection}
    if stage == "review":
        wire_item["target"] = "译文"
        wire_item["base_revision"] = 0
    payload = {
        "protocol": "epubox-text-1" if stage == "translate" else "epubox-review-2",
        "request_id": "r1",
        "items": [wire_item],
        "context": ["Earlier source."],
    }
    # T00 freezes records independently; actual measurement is tested in T01.
    budget = BudgetResult(
        stage=stage,  # type: ignore[arg-type]
        source_tokens=25,
        input_tokens=1000,
        review_target_input_tokens=0,
        input_reserve=1756,
        output_tokens=128,
        context_tokens=2140,
        review_targets="actual" if stage == "review" else None,
        failures=() if source_limit >= 25 else (f"source budget 25 exceeds {source_limit}",),
        identity=BudgetIdentity(
            version=2,
            tokenizer="cl100k_base",
            tokenizer_version="fixture",
            tokenizer_model="fixture",
            tokenizer_fallback=True,
            strategy="cl100k+50pct+256",
            margin_percent=50,
            wrapper_tokens=256,
            safety_tokens=256,
            target_ratio=1.6,
            source_limit=source_limit,
            input_limit=50_000,
            output_limit=4096,
            context_limit=60_000,
        ),
        wire_hash=wire_hash(stage, payload, 128),  # type: ignore[arg-type]
    )
    result = {
        "items": (member,),
        "context": ("Earlier source.",),
        "payload": payload,
        "budget": budget.model_dump(),
        "manifest": {
            "request_id": "r1",
            "stage": stage,
            "owner_kind": "translation_item",
            "owner_id": "i1",
            "item_ids": ("i1",),
            "input_hashes": {"i1": batch_item_hash(member, "freeze", wire_item, canonical_hash(payload["context"]))},
            "wire_hash": wire_hash(budget.stage, payload, budget.output_tokens),
            "record_versions": {"u1": 0},
            "item_unit_ids": {"i1": ("u1",)},
            "unit_document_ids": {"u1": "d1"},
            "plan_epochs": {"u1": 0},
            "revisions": {"u1": 0},
            "glossary_file_sha256": "glossary-file",
            "freeze_id": "freeze",
            "term_ids_by_item": {"i1": ()},
            "terms_hashes": {"i1": canonical_hash([])},
            "context_hashes": {"i1": canonical_hash(payload["context"])},
        },
    }
    if stage == "review":
        result["manifest"]["target_hashes"] = {"i1": canonical_hash("译文")}
    return result


def prepared_data() -> dict:
    result = {
        "preparation": {
            "source_hash": "source",
            "source_path": "source.epub",
            "source_epub_version": "3.0",
            "run_id": "run",
            "document_hashes": {"d1": "document"},
            "reading_order": ("d1",),
            "unit_documents": {"u1": "d1"},
            "user_terms_hash": canonical_hash(()),
        },
        "glossary": {
            "source_hash": "source",
            "freeze_id": "freeze",
            "extraction_config_hash": canonical_hash({}),
            "user_terms_hash": canonical_hash(()),
            "extraction_status": "disabled",
            "warnings": ("Automatic extraction disabled.",),
        },
        "bookplan": {
            "source_hash": "source",
            "run_id": "run",
            "preparation_hash": "preparation-file",
            "glossary_file_sha256": "glossary-file",
            "freeze_file_sha256": "freeze-file",
            "freeze_id": "freeze",
            "document_hashes": {"d1": "document"},
            "unit_ids": ("u1",),
            "unit_documents": {"u1": "d1"},
            "required_unit_count": 1,
            "initial_unit_plans": {"u1": "plan"},
            "output_policy_hash": "policy",
        },
        "preflight": {
            "source_hash": "source",
            "map_hashes": {"d1": "map"},
            "atoms_hash": "atoms",
            "budget_hash": "budget",
            "passed": True,
        },
        "map_hashes": {"d1": "map"},
        "plan_hashes": {"u1": "plan"},
    }
    result["bookplan"]["preparation_hash"] = canonical_hash(PreparationPlan.model_validate(result["preparation"]))
    result["bookplan"]["glossary_file_sha256"] = canonical_hash(GlossarySnapshot.model_validate(result["glossary"]))
    return result


def test_source_coordinates_round_trip_without_confusing_characters_and_bytes() -> None:
    source = "<p>你好</p>".encode()
    location = SourceLocation(
        node_key="p",
        source_ref=SourceRef(slot_id="text", start=0, end=2),
        byte_span=ByteSpan(byte_start=3, byte_end=9),
        field="text",
    )
    mapping = SourceMap(
        document_id="d1",
        source_hash="source",
        document_hash="document",
        encoding="utf-8",
        source_size=len(source),
        locations=(location,),
        protected_spans=(ByteSpan(byte_start=0, byte_end=3), ByteSpan(byte_start=9, byte_end=13)),
    )
    loaded = parse_contract(canonical_json_bytes(mapping), SourceMap, MAP_FORMAT)
    assert loaded == mapping
    assert source[loaded.locations[0].byte_span.byte_start : loaded.locations[0].byte_span.byte_end].decode() == "你好"
    with pytest.raises(ValidationError):
        ByteSpan.model_validate(location.source_ref.model_dump())
    with pytest.raises(ValidationError, match="overlap"):
        SourceMap.model_validate(mapping.model_dump() | {"protected_spans": ({"byte_start": 0, "byte_end": 4},)})


def test_batch_round_trip_and_shared_context_bounds() -> None:
    batch = RequestBatch.model_validate(batch_data())
    assert parse_contract(canonical_json_bytes(batch), RequestBatch, BATCH_FORMAT) == batch
    for context in [("one", "two", "three"), ("x" * 401,)]:
        with pytest.raises(ValidationError):
            RequestBatch.model_validate(batch_data() | {"context": context})
    data = batch_data()
    data["manifest"]["item_unit_ids"] = {"i1": ("other",)}
    with pytest.raises(ValidationError):
        RequestBatch.model_validate(data)


def test_batch_rejects_fake_failed_wrong_stage_and_unrelated_wire_budgets() -> None:
    data = batch_data()
    with pytest.raises(ValidationError):
        RequestBatch.model_validate(data | {"budget": {"anything": "accepted"}})
    with pytest.raises(ValidationError, match="current stage"):
        RequestBatch.model_validate(data | {"budget": batch_data("review")["budget"]})
    with pytest.raises(ValidationError, match="fitting budget"):
        RequestBatch.model_validate(batch_data(source_limit=1))
    data["budget"]["wire_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="complete payload"):
        RequestBatch.model_validate(data)
    data = batch_data()
    data["payload"]["items"][0]["source"] = "A different whole atom."
    with pytest.raises(ValidationError, match="complete payload"):
        RequestBatch.model_validate(data)


def test_review_batch_binds_saved_target_and_cannot_dispatch_an_estimate() -> None:
    data = batch_data("review")
    reviewed = RequestBatch.model_validate(data)
    assert parse_contract(canonical_json_bytes(reviewed), RequestBatch, BATCH_FORMAT) == reviewed
    data["manifest"]["target_hashes"]["i1"] = "different-saved-target"
    with pytest.raises(ValidationError, match="saved target identity"):
        RequestBatch.model_validate(data)
    data = batch_data("review")
    estimate = BudgetResult.model_validate(
        data["budget"]
        | {
            "review_targets": "estimated",
            "review_target_input_tokens": 40,
            "input_reserve": 1796,
            "context_tokens": 2180,
        }
    )
    data["budget"] = estimate.model_dump()
    data["manifest"]["wire_hash"] = estimate.wire_hash
    with pytest.raises(ValidationError, match="actual saved targets"):
        RequestBatch.model_validate(data)


def test_ready_round_trip_and_changed_identity_are_rejected() -> None:
    prepared = PreparedInput.model_validate(prepared_data())
    assert parse_contract(canonical_json_bytes(prepared), PreparedInput, PREPARED_FORMAT) == prepared
    for field, update in [
        ("glossary", {"source_hash": "other"}),
        ("bookplan", {"run_id": "other"}),
        ("preflight", {"passed": False}),
        ("preflight", {"map_hashes": {"d1": "other"}}),
    ]:
        data = prepared_data()
        data[field] |= update
        with pytest.raises(ValidationError):
            PreparedInput.model_validate(data)
    with pytest.raises(ValidationError):
        PreparedInput.model_validate(prepared_data() | {"plan_hashes": {}})


@pytest.mark.parametrize(
    ("field", "update"),
    [
        ("bookplan", {"preparation_hash": "unrelated"}),
        ("bookplan", {"glossary_file_sha256": "unrelated"}),
        ("bookplan", {"translation_config": {"model": "wrong"}}),
        ("glossary", {"extraction_config_hash": "unrelated"}),
    ],
)
def test_ready_rejects_changed_canonical_payloads_and_configuration(field: str, update: dict) -> None:
    data = prepared_data()
    data[field] |= update
    with pytest.raises(ValidationError):
        PreparedInput.model_validate(data)


@pytest.mark.parametrize(
    ("case", "initial", "replacement", "valid"),
    [
        ("no correction", "⟦+g1⟧译文⟦-g1⟧", None, True),
        ("valid correction", "⟦+g1⟧译文⟦-g1⟧", "⟦+g1⟧修订⟦-g1⟧", True),
        ("missing inline tag", "⟦+g1⟧译文⟦-g1⟧", "修订", False),
        ("misplaced inline tag", "⟦+g1⟧译文⟦-g1⟧", "⟦-g1⟧修订⟦+g1⟧", False),
    ],
)
def test_workflow_correction_contract_fixtures(case: str, initial: str, replacement: str | None, valid: bool) -> None:
    """T16 consumes these cases; this tests the existing local projection gate."""
    source = "⟦+g1⟧Source⟦-g1⟧"
    candidate = initial if replacement is None else replacement
    if valid:
        assert validate_projection(source, candidate)
    else:
        with pytest.raises(ProjectionError):
            validate_projection(source, candidate)
