from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError

from engine.agents.runtime import wire_hash
from engine.item.budget import measure_budget
from engine.item.members import MemberIndex, materialize_members, pack_members
from engine.schemas.budget import BudgetLimits, BudgetResult
from engine.schemas.contracts import RequestManifest, canonical_hash, canonical_json_bytes
from engine.services.ready import ReadySession, limits_from_config
from tests.engine.item.members import glossary, limits
from tests.engine.item.members import prepared as prepared_members
from tests.engine.services.ready import prepared


def unlimited(*, source_tokens: int = 10_000) -> BudgetLimits:
    return BudgetLimits(
        source_tokens=source_tokens,
        input_tokens=50_000,
        output_tokens=None,
        context_tokens=32_768,
        output_version=7,
        context_unlimited=True,
    )


def payload(source: str) -> dict[str, object]:
    return {
        "protocol": "epubox-text-1",
        "prompt_version": "epubox-members-1",
        "wire_version": "epubox-wire-5",
        "request_id": "tx-" + "0" * 32,
        "target_language": "zh-Hans",
        "context": [],
        "items": [{"item_id": "u-1", "source": source, "terms": [], "hints": {}, "constraints": {}}],
    }


def test_v7_measures_large_output_without_an_output_failure_or_wire_limit() -> None:
    request = payload("word " * 4000)
    measured = measure_budget(stage="translate", payload=request, limits=unlimited())

    assert measured.output_tokens > 8192
    assert measured.identity.output_limit is None
    assert measured.wire_hash == wire_hash("translate", request, None)
    assert measured.fits
    assert not any(reason.startswith("output ") for reason in measured.failures)


def test_v7_keeps_the_real_estimate_instead_of_the_old_output_floor() -> None:
    measured = measure_budget(stage="translate", payload=payload("Short text."), limits=unlimited())

    assert measured.output_tokens < 4096


def test_v7_context_gate_counts_input_but_not_the_output_estimate() -> None:
    request = payload("word " * 1000)
    measured = measure_budget(stage="translate", payload=request, limits=unlimited())
    input_context = measured.input_reserve + measured.identity.safety_tokens
    assert measured.context_tokens > input_context

    capacity = replace(unlimited(), context_tokens=input_context, context_unlimited=False)
    assert measure_budget(stage="translate", payload=request, limits=capacity).fits

    constrained = replace(capacity, context_tokens=input_context - 1)
    result = measure_budget(stage="translate", payload=request, limits=constrained)
    assert result.failures == (f"context budget {input_context} exceeds {input_context - 1}",)


def test_v7_requires_no_output_limit_and_rejects_tampered_saved_budgets() -> None:
    with pytest.raises(ValueError, match="must not define"):
        replace(unlimited(), output_tokens=8192)
    with pytest.raises(ValueError, match="positive integers"):
        replace(limits(), output_tokens=None)

    measured = measure_budget(stage="translate", payload=payload("Short text."), limits=unlimited())
    changed = measured.model_dump(mode="python")
    changed["identity"]["output_limit"] = 8192
    with pytest.raises(ValidationError, match="must not define"):
        BudgetResult.model_validate(changed)

    changed = measured.model_dump(mode="python")
    changed["failures"] = ("output budget 1 exceeds 0",)
    with pytest.raises(ValidationError, match="failures"):
        BudgetResult.model_validate(changed)


def test_v7_config_needs_no_max_output_and_explicit_upgrade_keeps_frozen_v6_available() -> None:
    current = limits_from_config({"output_budget_version": 7, "context_unlimited": True})
    upgraded = limits_from_config(
        {"output_budget_version": 6, "max_output_tokens": 8192},
        context_unlimited=True,
        output_unlimited=True,
    )
    frozen = limits_from_config(
        {"output_budget_version": 6, "max_output_tokens": 8192},
        context_unlimited=True,
        output_unlimited=False,
    )

    assert current.output_version == upgraded.output_version == 7
    assert current.output_tokens is upgraded.output_tokens is None
    assert current.to_dict()["output_tokens"] is None
    assert frozen.output_version == 6 and frozen.output_tokens == 8192


def test_false_manifest_flag_keeps_legacy_json_and_new_batches_mark_unlimited() -> None:
    fields = {
        "request_id": "tx-1",
        "stage": "terms",
        "owner_kind": "extraction_item",
        "owner_id": "u-1",
        "item_ids": ("u-1",),
        "input_hashes": {"u-1": "hash"},
        "wire_hash": "wire",
    }
    legacy = RequestManifest(**fields)
    dumped = legacy.model_dump(mode="json")
    assert "output_unlimited" not in dumped
    assert canonical_json_bytes(legacy) == canonical_json_bytes(dumped)
    assert RequestManifest(**fields, output_unlimited=True).model_dump(mode="json")["output_unlimited"] is True

    inventory, report = prepared_members("<h2>Current</h2>")
    members = materialize_members((inventory,), report)
    index = MemberIndex((inventory,), report, members)
    batch = pack_members("translate", members, glossary(), index, unlimited()).batches[0]
    assert batch.manifest.output_unlimited


def test_v2_through_v6_packing_hashes_remain_frozen() -> None:
    expected = {
        2: "fdf1efad0b273758501cd24a2bfa1b1f9be3f17cb20fe1e41cd653eb63c1a6ee",
        3: "b68df0fbefc901668014cf9e3d8153310fa55cd669b71e0b8a013dff4ebba032",
        4: "6da1636fde660286e82dc48c3a4bc4f424149f9a34bdf31194eb276ddaf2011c",
        5: "917c4168da2088455e63d0d56f08276221101b37379a58b8250412f82e96c194",
        6: "22b581909cba78a9fde505254147fa3fe0c26f583b7581c99eeed46a22823f8b",
    }
    inventory, report = prepared_members("<h2>Previous</h2><h2>Current</h2><h2>Following</h2>")
    members = materialize_members((inventory,), report)
    index = MemberIndex((inventory,), report, members)

    for version, fingerprint in expected.items():
        configured = replace(limits(), output_version=version)
        assert canonical_hash(pack_members("translate", members, glossary(), index, configured)) == fingerprint


def test_ready_verifies_frozen_and_unlimited_batches_by_their_manifest_policy(tmp_path) -> None:
    store, value = prepared(tmp_path, output_budget_version=6, max_output_tokens=8192)
    session = ReadySession(store)
    frozen = next(iter(session._prepared_batches.values()))
    session.verify_batch(frozen, initial=True)

    configured = limits_from_config(
        value.preparation.translation_config,
        context_unlimited=True,
        output_unlimited=True,
    )
    current = pack_members(
        "translate",
        frozen.items,
        value.glossary,
        session.index,
        configured,
        tokenizer_model="fake",
    ).batches[0]
    assert current.manifest.output_unlimited
    session.verify_batch(current)
