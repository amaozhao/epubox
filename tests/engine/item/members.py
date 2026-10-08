from __future__ import annotations

from dataclasses import replace
from typing import Literal

import pytest

import engine.item.members as members_module
from engine.item.atoms import extract_resource
from engine.item.budget import measure_budget
from engine.item.inline import projection_identities
from engine.item.members import (
    MemberIndex,
    build_member_payload,
    materialize_members,
    merge_member_targets,
    pack_members,
    validate_member_target,
)
from engine.schemas.budget import BUDGET_VERSION, BudgetLimits
from engine.schemas.contracts import GlossarySnapshot, ItemRecord, canonical_hash
from engine.services.preflight import PREFLIGHT_VERSION, preflight_atomic_resources

XHTML = "http://www.w3.org/1999/xhtml"


def source(body: str) -> bytes:
    return f'<html xmlns="{XHTML}"><head/><body>{body}</body></html>'.encode()


def limits(source_tokens: int = 5000) -> BudgetLimits:
    return BudgetLimits(
        source_tokens=source_tokens,
        input_tokens=50_000,
        output_tokens=10_000,
        context_tokens=60_000,
    )


def glossary(source_hash: str = "book") -> GlossarySnapshot:
    return GlossarySnapshot(
        source_hash=source_hash,
        freeze_id="freeze",
        extraction_config_hash="config",
        user_terms_hash="user",
        extraction_status="closed",
        warnings=("no terms",),
    )


def prepared(body: str, *, path: str = "OPS/chapter.xhtml", source_hash: str = "book", cap: int = 5000):
    raw = source(body)
    inventory = extract_resource(raw, path, source_hash)
    report = preflight_atomic_resources((inventory,), {path: raw}, limits(cap), "gpt-3.5-turbo")
    assert report.check is not None
    return inventory, report


def record(item_id: str, target: str) -> ItemRecord:
    return ItemRecord(
        item_id=item_id,
        segment_id=item_id,
        terms_hash="terms",
        context_hash="context",
        target_projection=target,
        target_hash=canonical_hash(target),
    )


def test_materialization_uses_only_accepted_complete_preflight_coverage() -> None:
    paragraph = "word " * 850
    inventory, report = prepared(
        '<a href="#linked">linked text</a> ' + paragraph + ". " + paragraph + ". " + paragraph + ".",
        cap=2000,
    )
    parent = inventory.items[0]

    first = materialize_members((inventory,), report)
    second = materialize_members((inventory,), report)

    assert first == second
    assert len(first) == 2
    assert tuple(member.piece_index for member in first) == (0, 1)
    assert all(member.piece_count == 2 and member.parent_item_id == parent.item_id for member in first)
    assert "".join(member.source_projection for member in first) == parent.source_projection
    assert first[0].source_span.byte_start == parent.source_span.byte_start
    assert first[-1].source_span.byte_end == parent.source_span.byte_end
    assert set(first[0].registry) | set(first[1].registry) == set(parent.registry)
    assert all(set(member.registry) == set(projection_identities(member.source_projection)) for member in first)

    missing = report.model_copy(update={"pieces": report.pieces[:-1]})
    with pytest.raises(ValueError, match="budget identity|coverage|reconstruct|end"):
        materialize_members((inventory,), missing)


def test_hard_atoms_stay_whole_and_cannot_be_relabelled_as_pieces() -> None:
    inventory, report = prepared("<p>Keep <em>this</em> paragraph whole.</p>")
    member = materialize_members((inventory,), report)[0]

    assert member.item_id == member.parent_item_id
    assert member.piece_count == 1 and member.atomic_tag == "p"
    assert member.registry == inventory.items[0].registry
    validate_member_target(member, member.source_projection)

    forged_piece = report.pieces[0].model_copy(update={"piece_id": "pc-" + "a" * 32})
    forged = report.model_copy(update={"pieces": (forged_piece,)})
    with pytest.raises(ValueError, match="budget identity|diagnostics|hard atomic"):
        materialize_members((inventory,), forged)


def test_member_index_preserves_supplied_spine_order_and_bounds_context() -> None:
    second_raw = source("<h2>Second document</h2>")
    first_raw = source("<h2>First document</h2>")
    second = extract_resource(second_raw, "OPS/z.xhtml", "book")
    first = extract_resource(first_raw, "OPS/a.xhtml", "book")
    ordered = (second, first)
    combined = preflight_atomic_resources(
        ordered,
        {"OPS/z.xhtml": second_raw, "OPS/a.xhtml": first_raw},
        limits(),
        "gpt-3.5-turbo",
    )
    index = MemberIndex(ordered, combined)
    assert index.document_order == (second.document.document_id, first.document.document_id)
    assert [member.document_id for member in index.members] == list(index.document_order)

    inventory, report = prepared("<h2>First</h2><h2>Second</h2><h2>Third</h2>")
    members = materialize_members((inventory,), report)
    index = MemberIndex((inventory,), report, members)
    assert [text for _, text in index.preceding((members[-1],), 2)] == ["First", "Second"]


def test_merge_requires_every_hash_bound_sibling_before_validating_parent() -> None:
    paragraph = "word " * 850
    inventory, report = prepared(paragraph + ". " + paragraph + ". " + paragraph + ".", cap=2000)
    parent = inventory.items[0]
    members = materialize_members((inventory,), report)
    results = {member.item_id: record(member.item_id, member.source_projection) for member in members}

    assert merge_member_targets(parent, members, results) == parent.source_projection
    with pytest.raises(ValueError, match="exactly cover"):
        merge_member_targets(parent, members, {members[0].item_id: results[members[0].item_id]})

    broken = results[members[0].item_id].model_copy(update={"target_hash": "forged"})
    with pytest.raises(ValueError, match="hash-bound"):
        merge_member_targets(parent, members, results | {members[0].item_id: broken})
    wrong = results[members[0].item_id].model_copy(update={"segment_id": "other"})
    with pytest.raises(ValueError, match="identity differs"):
        merge_member_targets(parent, members, results | {members[0].item_id: wrong})


def test_member_payload_and_packing_use_piece_identity_and_shared_context() -> None:
    inventory, report = prepared("<h2>Previous</h2><h2>Current</h2><h2>Following</h2>")
    members = materialize_members((inventory,), report)
    index = MemberIndex((inventory,), report, members)
    frozen = glossary()

    payload = build_member_payload("translate", (members[1], members[2]), frozen, index, request_id="tx-test")
    assert payload["prompt_version"] == "epubox-members-1"
    assert payload["protocol"] == "epubox-text-1"
    assert payload["context"] == ["Previous"]
    assert all("context" not in item for item in payload["items"])

    packed = pack_members("translate", members, frozen, index, limits())
    assert packed.ready and not packed.blocked
    assert [item.item_id for batch in packed.batches for item in batch.items] == [item.item_id for item in members]
    assert all(batch.format == "epubox-batch-2" for batch in packed.batches)
    assert all(batch.payload["prompt_version"] == "epubox-members-1" for batch in packed.batches)


@pytest.mark.parametrize(
    ("version", "expected"),
    (
        (2, "fdf1efad0b273758501cd24a2bfa1b1f9be3f17cb20fe1e41cd653eb63c1a6ee"),
        (3, "b68df0fbefc901668014cf9e3d8153310fa55cd669b71e0b8a013dff4ebba032"),
        (4, "6da1636fde660286e82dc48c3a4bc4f424149f9a34bdf31194eb276ddaf2011c"),
    ),
)
def test_packing_preserves_budget_version_identities(version: Literal[2, 3, 4], expected: str) -> None:
    inventory, report = prepared("<h2>Previous</h2><h2>Current</h2><h2>Following</h2>")
    members = materialize_members((inventory,), report)
    index = MemberIndex((inventory,), report, members)
    configured = replace(limits(), output_version=version)

    assert canonical_hash(pack_members("translate", members, glossary(), index, configured)) == expected


def test_packing_hashes_stable_identity_components_once(monkeypatch: pytest.MonkeyPatch) -> None:
    inventory, report = prepared("<h2>Previous</h2><h2>Current</h2><h2>Following</h2>")
    members = materialize_members((inventory,), report)
    index = MemberIndex((inventory,), report, members)
    frozen = glossary()
    targets = {member.item_id: record(member.item_id, member.source_projection) for member in members}
    tracked = {id(frozen): "glossary"}
    tracked.update((id(member), f"member:{member.item_id}") for member in members)
    tracked.update((id(target), f"target:{item_id}") for item_id, target in targets.items())
    calls = dict.fromkeys(tracked.values(), 0)
    limit_calls = 0
    real_hash = members_module.canonical_hash
    real_limits = BudgetLimits.to_dict

    def hash_spy(value: object) -> str:
        if name := tracked.get(id(value)):
            calls[name] += 1
        return real_hash(value)

    def limits_spy(value: BudgetLimits) -> dict[str, int | float]:
        nonlocal limit_calls
        limit_calls += 1
        return real_limits(value)

    monkeypatch.setattr(members_module, "canonical_hash", hash_spy)
    monkeypatch.setattr(BudgetLimits, "to_dict", limits_spy)

    packed = pack_members(
        "review",
        members,
        frozen,
        index,
        limits(),
        targets=targets,
        revisions={member.unit_id: 4 for member in members},
    )

    assert packed.ready
    assert canonical_hash(packed) == "3219eb1ac551bd11147cbce7de8ed2cf2c26bbfd2664c81fff87d485d1c50f58"
    assert set(calls.values()) == {1}
    assert limit_calls == 1


def test_whole_pack_measures_only_the_complete_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    inventory, report = prepared("<p>" + "word " * 800 + "</p><p>" + "word " * 1000 + "</p>", cap=3000)
    index = MemberIndex((inventory,), report)
    capacity = replace(
        limits(),
        source_tokens=2000,
        source_tolerance_tokens=1000,
        minimum_source_tokens=500,
        output_version=6,
        output_tokens=10000,
        context_unlimited=True,
    )
    calls: list[tuple[str, ...]] = []
    real_fit = members_module.fit_member_payload

    def tracked(stage, items, *args, **kwargs):
        calls.append(tuple(item.item_id for item in items))
        return real_fit(stage, items, *args, **kwargs)

    monkeypatch.setattr(members_module, "fit_member_payload", tracked)
    planned = pack_members("translate", index.members, glossary(), index, capacity, sparse=True, whole=True)

    assert planned.ready and len(planned.batches) == 1
    assert calls == [tuple(member.item_id for member in index.members)]


def test_member_index_hashes_each_glossary_object_once(monkeypatch: pytest.MonkeyPatch) -> None:
    inventory, report = prepared("<p>First.</p><p>Second.</p>")
    index = MemberIndex((inventory,), report)
    first, second = glossary(), glossary()
    calls = {id(first): 0, id(second): 0}
    real_hash = members_module.canonical_hash

    def tracked(value):
        if id(value) in calls:
            calls[id(value)] += 1
        return real_hash(value)

    monkeypatch.setattr(members_module, "canonical_hash", tracked)
    for frozen in (first, first, second, second):
        assert pack_members("translate", index.members, frozen, index, limits(), sparse=True, whole=True).ready

    assert calls == {id(first): 1, id(second): 1}


def test_review_payload_binds_each_piece_target_and_parent_revision() -> None:
    paragraph = "word " * 850
    inventory, report = prepared(paragraph + ". " + paragraph + ". " + paragraph + ".", cap=2000)
    members = materialize_members((inventory,), report)
    index = MemberIndex((inventory,), report, members)
    targets = {member.item_id: record(member.item_id, member.source_projection) for member in members}
    revisions = {members[0].unit_id: 4}

    packed = pack_members(
        "review",
        members,
        glossary(),
        index,
        limits(),
        targets=targets,
        revisions=revisions,
    )

    assert packed.ready
    assert all(batch.budget.review_targets == "actual" for batch in packed.batches)
    for batch in packed.batches:
        wire_items = batch.payload["items"]
        assert isinstance(wire_items, list)
        assert all(isinstance(item, dict) and item["base_revision"] == 4 for item in wire_items)


def test_heading_priority_and_saved_versions_affect_member_request_identity() -> None:
    body = f"<p>{'word ' * 90}</p><h2>New section</h2><p>{'word ' * 90}</p>"
    inventory, report = prepared(body)
    members = materialize_members((inventory,), report)
    previous, heading, following = members
    index = MemberIndex((inventory,), report, members)
    frozen = glossary()
    pair_payload = build_member_payload("translate", (heading, following), frozen, index, request_id="tx-" + "a" * 32)
    pair_budget = measure_budget(stage="translate", payload=pair_payload, limits=limits())
    constrained = limits(pair_budget.source_tokens)

    packed = pack_members("translate", members, frozen, index, constrained)
    assert [[item.item_id for item in batch.items] for batch in packed.batches] == [
        [previous.item_id],
        [heading.item_id, following.item_id],
    ]
    assert packed.boundaries[0].reason == "heading"
    assert canonical_hash(packed) == "b7f3bc6080b7c2d286a221c3d9b94b009612aa9e9ebd5c7750c6eb19753b2a7a"

    baseline = pack_members("translate", members, frozen, index, limits()).batches[0].manifest.request_id
    versioned = (
        pack_members(
            "translate",
            members,
            frozen,
            index,
            limits(),
            record_versions={member.unit_id: 3 for member in members},
        )
        .batches[0]
        .manifest.request_id
    )
    epoched = (
        pack_members(
            "translate",
            members,
            frozen,
            index,
            limits(),
            plan_epochs={member.unit_id: 4 for member in members},
        )
        .batches[0]
        .manifest.request_id
    )
    assert len({baseline, versioned, epoched}) == 3


def test_materialization_rejects_self_consistent_forged_preflight_metadata() -> None:
    inventory, report = prepared("<h2>Integrity</h2>")
    assert report.check is not None

    with pytest.raises(ValueError, match="resource identities"):
        materialize_members((inventory,), report.model_copy(update={"resource_hashes": {"ghost": "forged"}}))

    duplicated = report.model_copy(update={"diagnostics": (*report.diagnostics, report.diagnostics[0])})
    with pytest.raises(ValueError, match="cannot repeat"):
        materialize_members((inventory,), duplicated)

    fake_hash = "f" * 64
    fake_check = report.check.model_copy(update={"budget_hash": fake_hash})
    forged_hash = report.model_copy(update={"budget_hash": fake_hash, "check": fake_check})
    with pytest.raises(ValueError, match="budget identity"):
        materialize_members((inventory,), forged_hash)

    piece = report.pieces[0]
    identity = piece.translate.identity.model_copy(update={"source_limit": piece.translate.identity.source_limit + 1})
    changed_piece = piece.model_copy(update={"translate": piece.translate.model_copy(update={"identity": identity})})
    changed_pieces = (changed_piece,)
    changed_hash = canonical_hash(
        {
            "version": BUDGET_VERSION,
            "preflight_version": PREFLIGHT_VERSION,
            "model": report.model,
            "limits": report.limits,
            "pieces": tuple(value.model_dump(mode="json") for value in changed_pieces),
        }
    )
    changed_check = report.check.model_copy(update={"budget_hash": changed_hash})
    changed = report.model_copy(update={"pieces": changed_pieces, "budget_hash": changed_hash, "check": changed_check})
    with pytest.raises(ValueError, match="model identity"):
        materialize_members((inventory,), changed)


def test_materialization_accepts_the_effective_50k_input_cap_and_rejects_a_raw_limit_identity() -> None:
    raw = source("<h2>Input cap</h2>")
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    configured = BudgetLimits(
        source_tokens=5000,
        input_tokens=60_000,
        output_tokens=10_000,
        context_tokens=70_000,
    )
    report = preflight_atomic_resources((inventory,), {"OPS/chapter.xhtml": raw}, configured, "gpt-3.5-turbo")
    assert report.check is not None
    assert report.limits["input_tokens"] == 60_000
    assert report.pieces[0].translate.identity.input_limit == 50_000
    assert materialize_members((inventory,), report)

    piece = report.pieces[0]
    raw_identity = piece.translate.identity.model_copy(update={"input_limit": 60_000})
    changed_piece = piece.model_copy(
        update={"translate": piece.translate.model_copy(update={"identity": raw_identity})}
    )
    changed_pieces = (changed_piece,)
    changed_hash = canonical_hash(
        {
            "version": BUDGET_VERSION,
            "preflight_version": PREFLIGHT_VERSION,
            "model": report.model,
            "limits": report.limits,
            "pieces": tuple(value.model_dump(mode="json") for value in changed_pieces),
        }
    )
    changed = report.model_copy(
        update={
            "pieces": changed_pieces,
            "budget_hash": changed_hash,
            "check": report.check.model_copy(update={"budget_hash": changed_hash}),
        }
    )
    with pytest.raises(ValueError, match="model identity"):
        materialize_members((inventory,), changed)


def test_packing_closes_batches_across_normal_channel_transitions() -> None:
    inventory, report = prepared('<p>Body<img alt="Label"/></p><h2>After</h2>')
    members = materialize_members((inventory,), report)
    index = MemberIndex((inventory,), report, members)

    packed = pack_members("translate", members, glossary(), index, limits())

    assert packed.ready
    assert [item.item_id for batch in packed.batches for item in batch.items] == [item.item_id for item in members]
    assert any(boundary.reason in {"channel", "adjacency"} for boundary in packed.boundaries)
