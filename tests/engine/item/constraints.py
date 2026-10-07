from dataclasses import replace
from typing import Any, cast

import pytest

from engine.item.atoms import extract_resource
from engine.item.budget import measure_budget
from engine.item.members import (
    MemberIndex,
    build_member_payload,
    materialize_members,
    pack_members,
    validate_member_target,
)
from engine.schemas.budget import BudgetLimits
from engine.services.preflight import preflight_atomic_resources
from tests.engine.item.members import glossary, source


def directory_case(prefix=""):
    raw = source(
        "<p>" + prefix + "<br/>".join(f"├── Service{number}Application.java" for number in range(38)) + "</p>"
    )
    inventory = extract_resource(raw, "OPS/tree.xhtml", "book")
    limits = BudgetLimits(2000, 50000, 4096, 32768, output_version=4)
    report = preflight_atomic_resources((inventory,), {"OPS/tree.xhtml": raw}, limits, "gpt-3.5-turbo")
    assert report.passed
    index = MemberIndex((inventory,), report)
    return index, limits


def test_locked_directory_tree_fits_without_splitting_or_weakening_order():
    index, limits = directory_case()
    member = index.members[0]
    old = build_member_payload("translate", (member,), glossary(), index, request_id="old")
    old_budget = measure_budget(stage="translate", payload=old, limits=limits)
    assert not old_budget.fits and any("context budget" in failure for failure in old_budget.failures)
    result = pack_members("translate", (member,), glossary(), index, limits)
    assert result.ready and len(result.batches) == 1
    batch = result.batches[0]
    payload = cast(dict[str, Any], batch.payload)
    assert batch.items == (member,) and member.piece_count == 1
    assert payload["items"][0]["source"] == member.source_projection
    assert batch.budget.fits and batch.budget.input_tokens < old_budget.input_tokens / 2
    assert all(not value["fixed_order"] for value in payload["items"][0]["constraints"].values())
    assert any(entry.fixed_order for entry in member.registry.values())
    invalid = member.source_projection.replace("⟦=x1⟧", "TEMP").replace("⟦=x2⟧", "⟦=x1⟧").replace("TEMP", "⟦=x2⟧")
    with pytest.raises(ValueError):
        validate_member_target(member, invalid)


def test_fitting_legacy_payload_and_budget_remain_unchanged():
    index, limits = directory_case()
    result = pack_members("translate", index.members, glossary(), index, replace(limits, context_tokens=60000))
    assert result.ready
    batch = result.batches[0]
    assert batch.payload == build_member_payload(
        "translate", batch.items, glossary(), index, request_id=batch.manifest.request_id
    )
    assert any(
        value["fixed_order"] for value in cast(dict[str, Any], batch.payload)["items"][0]["constraints"].values()
    )


def test_movable_references_are_not_silently_locked_to_make_a_payload_fit():
    index, limits = directory_case("<em>Root</em>")
    assert any(entry.reorder_allowed for entry in index.members[0].registry.values())
    result = pack_members("translate", index.members, glossary(), index, limits)
    assert not result.ready


def test_small_html_opening_merges_forward_into_a_constraint_heavy_next_chunk():
    raw = source(
        "<h2>Project layout</h2><p>"
        + "<br/>".join(f"├── Service{number}Application.java" for number in range(38))
        + "</p>"
    )
    inventory = extract_resource(raw, "OPS/tree.xhtml", "book")
    base = BudgetLimits(2000, 50000, 4096, 32768, output_version=4)
    report = preflight_atomic_resources((inventory,), {"OPS/tree.xhtml": raw}, base, "gpt-3.5-turbo")
    members = tuple(inventory.items)
    request_members = materialize_members((inventory,), report)
    index = MemberIndex((inventory,), report, request_members)
    legacy = pack_members("translate", request_members, glossary(), index, base)
    planned = pack_members("translate", request_members, glossary(), index, replace(base, minimum_source_tokens=500))

    assert len(members) == 2 and [batch.budget.source_tokens for batch in legacy.batches] == [2, 266]
    assert len(planned.batches) == 1 and len(planned.batches[0].items) == 2
    assert planned.boundaries[0].reason == "minimum_unavoidable"
