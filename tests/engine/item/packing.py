from __future__ import annotations

from typing import Any, cast

import pytest

from engine.item.budget import measure_budget
from engine.item.packing import PACKING_VERSION, PackingResult, pack_requests
from engine.item.request import SourceIndex, build_payload
from engine.schemas.bridge import BATCH_FORMAT, RequestBatch
from engine.schemas.budget import BudgetLimits
from engine.schemas.contracts import TermScope, canonical_hash, canonical_json_bytes, parse_contract
from tests.engine.item.request import glossary, inventory, record, term


def limits(
    source: int = 20_000,
    input_: int = 100_000,
    output: int = 20_000,
    context: int = 150_000,
) -> BudgetLimits:
    return BudgetLimits(
        source_tokens=source,
        input_tokens=input_,
        output_tokens=output,
        context_tokens=context,
    )


def members(result: PackingResult) -> list[list[str]]:
    return [list(batch.manifest.item_ids) for batch in result.batches]


def measured(stage, payload):
    return measure_budget(stage=stage, payload=payload, limits=limits())


def test_greedy_packing_measures_the_complete_candidate_request() -> None:
    doc = inventory("".join(f"<p>{letter + ' ' * 2}{'word ' * 180}</p>" for letter in "ABC"))
    index = SourceIndex((doc,))
    frozen = glossary()
    first, second, third = doc.items
    request_id = "tx-" + "a" * 32
    pair = measured("translate", build_payload("translate", (first, second), frozen, index, request_id=request_id))
    triple = measured(
        "translate", build_payload("translate", (first, second, third), frozen, index, request_id=request_id)
    )
    assert pair.input_reserve < triple.input_reserve

    result = pack_requests(
        "translate",
        doc.items,
        frozen,
        index,
        limits(input_=(pair.input_reserve + triple.input_reserve) // 2),
    )

    assert members(result) == [[first.item_id, second.item_id], [third.item_id]]
    assert result.boundaries[0].reason == "input"
    assert all(batch.budget.fits for batch in result.batches)
    assert all(batch.budget.wire_hash == batch.manifest.wire_hash for batch in result.batches)


def test_configured_source_limit_accepts_a_whole_2600_token_paragraph_at_5000() -> None:
    doc = inventory(f"<p>{'token ' * 2600}</p>")
    item = doc.items[0]
    index = SourceIndex((doc,))
    frozen = glossary()

    blocked = pack_requests("translate", (item,), frozen, index, limits(source=2000))
    accepted = pack_requests("translate", (item,), frozen, index, limits(source=5000))

    assert 2000 < blocked.blocked[0].budget.source_tokens < 5000
    assert blocked.batches == ()
    assert blocked.blocked[0].item_id == item.item_id
    assert members(accepted) == [[item.item_id]]


def test_there_is_no_hidden_1200_token_or_eight_item_batch_cap() -> None:
    doc = inventory("".join(f"<p>paragraph {number} {'token ' * 130}</p>" for number in range(9)))
    result = pack_requests("translate", doc.items, glossary(), SourceIndex((doc,)), limits(source=5000))

    assert len(result.batches) == 1
    assert len(result.batches[0].items) == 9
    assert result.batches[0].budget.source_tokens > 1200


def test_oversized_table_and_list_are_blocked_as_whole_atoms() -> None:
    doc = inventory(f"<table><tr><td>{'table ' * 80}</td></tr></table><ul><li>{'list ' * 80}</li></ul>")
    table, listing = doc.items
    result = pack_requests("translate", doc.items, glossary(), SourceIndex((doc,)), limits(source=20))

    assert result.batches == ()
    assert [(entry.item_id, entry.atomic_tag) for entry in result.blocked] == [
        (table.item_id, "table"),
        (listing.item_id, "ul"),
    ]
    assert [entry.source_span for entry in result.blocked] == [table.source_span, listing.source_span]


def test_resources_channels_and_ordinal_gaps_close_batches() -> None:
    first = inventory('<p>Body one.</p><p>Body two.<img alt="Label"/></p>', path="OPS/one.xhtml")
    second = inventory("<p>Other resource.</p>", path="OPS/two.xhtml")
    index = SourceIndex((first, second))
    result = pack_requests("translate", (*first.items, *second.items), glossary(), index, limits())

    assert [boundary.reason for boundary in result.boundaries] == ["channel", "resource", "end"]
    assert [batch.items[0].channel for batch in result.batches] == ["body", "attribute", "body"]
    assert all(len({item.document_id for item in batch.items}) == 1 for batch in result.batches)

    gap = inventory("<p>First.</p><p>Middle.</p><p>Last.</p>")
    separated = pack_requests("translate", (gap.items[0], gap.items[2]), glossary(), SourceIndex((gap,)), limits())
    assert members(separated) == [[gap.items[0].item_id], [gap.items[2].item_id]]
    assert separated.boundaries[0].reason == "adjacency"


def test_budget_drops_oldest_context_before_reducing_the_batch() -> None:
    old_text = "OldTerm " + "0123456789abcdef" * 30
    recent_text = "fedcba9876543210" * 30 + " RecentTerm"
    doc = inventory(f"<p>{old_text}</p><p>{recent_text}</p><p>Current.</p>")
    old, recent, current = doc.items
    frozen = glossary(
        term("old", "OldTerm", "旧词", TermScope(kind="book"), note="old context note"),
        term("recent", "RecentTerm", "近词", TermScope(kind="book"), note="recent context note"),
    )
    index = SourceIndex((doc,))
    request_id = "tx-" + "b" * 32
    two = measured("translate", build_payload("translate", (current,), frozen, index, request_id=request_id))
    one = measured(
        "translate", build_payload("translate", (current,), frozen, index, request_id=request_id, context_count=1)
    )
    assert one.input_reserve + 30 < two.input_reserve

    result = pack_requests(
        "translate",
        doc.items,
        frozen,
        index,
        limits(input_=(one.input_reserve + two.input_reserve) // 2),
        completed={old.item_id, recent.item_id},
    )

    batch = result.batches[0]
    assert batch.manifest.item_ids == (current.item_id,)
    assert batch.context == (recent_text[-400:],)
    assert len(batch.context) == 1 and len(batch.context[0]) <= 400
    wire_items = cast(list[dict[str, Any]], batch.payload["items"])
    selected = cast(list[dict[str, Any]], wire_items[0]["terms"])
    assert [value["term_id"] for value in selected] == ["recent"]
    assert selected[0]["role"] == "context"
    assert selected[0]["note"] == "recent context note"


def test_saved_review_targets_can_regroup_without_changing_translate_groups() -> None:
    doc = inventory("<p>First.</p><p>Second.</p>")
    first, second = doc.items
    index = SourceIndex((doc,))
    frozen = glossary()
    translations = pack_requests("translate", doc.items, frozen, index, limits(input_=5000))
    assert members(translations) == [[first.item_id, second.item_id]]

    targets = {
        first.item_id: record(first, "甲" * 900),
        second.item_id: record(second, "乙" * 900),
    }
    revisions = {first.unit_id: 1, second.unit_id: 2}
    request_id = "tx-" + "c" * 32
    single = measured(
        "review",
        build_payload(
            "review",
            (first,),
            frozen,
            index,
            request_id=request_id,
            targets={first.item_id: targets[first.item_id]},
            revisions={first.unit_id: 1},
        ),
    )
    pair = measured(
        "review",
        build_payload("review", doc.items, frozen, index, request_id=request_id, targets=targets, revisions=revisions),
    )
    assert single.input_reserve < pair.input_reserve

    reviews = pack_requests(
        "review",
        doc.items,
        frozen,
        index,
        limits(input_=(single.input_reserve + pair.input_reserve) // 2),
        targets=targets,
        revisions=revisions,
    )
    assert members(reviews) == [[first.item_id], [second.item_id]]
    assert [cast(list[dict[str, Any]], batch.payload["items"])[0]["target"] for batch in reviews.batches] == [
        "甲" * 900,
        "乙" * 900,
    ]


def test_oversized_single_review_is_blocked_without_mutating_saved_target() -> None:
    doc = inventory("<p>Source.</p>")
    item = doc.items[0]
    saved = record(item, "译" * 4000)
    targets = {item.item_id: saved}

    result = pack_requests(
        "review",
        (item,),
        glossary(),
        SourceIndex((doc,)),
        limits(input_=1000),
        targets=targets,
        revisions={item.unit_id: 7},
    )

    assert result.batches == ()
    assert result.blocked[0].item_id == item.item_id
    assert targets == {item.item_id: saved}
    assert targets[item.item_id].target_projection == "译" * 4000


def test_completed_blocked_and_ready_ids_are_each_accounted_for_once() -> None:
    doc = inventory(f"<p>done</p><p>{'large ' * 200}</p><p>ready</p>")
    done, large, ready = doc.items
    roomy = limits()
    large_budget = measure_budget(
        stage="translate",
        payload=build_payload("translate", (large,), glossary(), SourceIndex((doc,)), request_id="tx-" + "d" * 32),
        limits=roomy,
    )
    ready_budget = measure_budget(
        stage="translate",
        payload=build_payload("translate", (ready,), glossary(), SourceIndex((doc,)), request_id="tx-" + "d" * 32),
        limits=roomy,
    )
    result = pack_requests(
        "translate",
        doc.items,
        glossary(),
        SourceIndex((doc,)),
        limits(source=(large_budget.source_tokens + ready_budget.source_tokens) // 2),
        completed={done.item_id},
    )
    accounted = [item for batch in result.batches for item in batch.manifest.item_ids]
    accounted += [entry.item_id for entry in result.blocked]
    accounted += list(result.skipped)

    assert sorted(accounted) == sorted(item.item_id for item in doc.items)
    assert len(accounted) == len(set(accounted))
    assert result.skipped == (done.item_id,)
    assert [entry.item_id for entry in result.blocked] == [large.item_id]
    assert members(result) == [[ready.item_id]]


def test_manifest_binds_wire_inputs_versions_and_round_trips() -> None:
    doc = inventory("<p>One.</p><p>Two.</p>")
    frozen = glossary()
    versions = {item.unit_id: number + 3 for number, item in enumerate(doc.items)}
    epochs = {item.unit_id: number + 8 for number, item in enumerate(doc.items)}
    result = pack_requests(
        "translate",
        doc.items,
        frozen,
        SourceIndex((doc,)),
        limits(),
        record_versions=versions,
        plan_epochs=epochs,
    )
    batch = result.batches[0]
    manifest = batch.manifest

    assert manifest.glossary_file_sha256 == canonical_hash(frozen)
    assert manifest.freeze_id == frozen.freeze_id
    assert manifest.record_versions == versions
    assert manifest.plan_epochs == epochs
    assert manifest.wire_hash == batch.budget.wire_hash
    assert set(manifest.input_hashes) == {item.item_id for item in doc.items}
    assert parse_contract(canonical_json_bytes(batch), RequestBatch, BATCH_FORMAT) == batch


def test_stage_model_versions_freeze_and_source_identity_change_request_id() -> None:
    doc = inventory("<p>Identity.</p>")
    item = doc.items[0]
    index = SourceIndex((doc,))
    frozen = glossary()

    def request_id(**kwargs) -> str:
        return pack_requests("translate", (item,), frozen, index, limits(), **kwargs).batches[0].manifest.request_id

    baseline = request_id()
    assert request_id(tokenizer_model="unknown-model") != baseline
    assert request_id(record_versions={item.unit_id: 1}) != baseline
    assert request_id(plan_epochs={item.unit_id: 1}) != baseline
    changed_freeze = frozen.model_copy(update={"freeze_id": "other-freeze"})
    assert (
        pack_requests("translate", (item,), changed_freeze, index, limits()).batches[0].manifest.request_id != baseline
    )

    targets = {item.item_id: record(item, "译文")}
    reviewed = pack_requests("review", (item,), frozen, index, limits(), targets=targets, revisions={item.unit_id: 1})
    assert reviewed.batches[0].manifest.request_id != baseline

    other = inventory("<p>Identity.</p>", source_hash="other-book")
    other_id = (
        pack_requests("translate", other.items, glossary(source_hash="other-book"), SourceIndex((other,)), limits())
        .batches[0]
        .manifest.request_id
    )
    assert other_id != baseline
    assert PACKING_VERSION == 1


def test_invalid_duplicates_order_completion_and_review_targets_are_rejected() -> None:
    doc = inventory("<p>First.</p><p>Second.</p>")
    first, second = doc.items
    index = SourceIndex((doc,))
    frozen = glossary()

    with pytest.raises(ValueError, match="repeat"):
        pack_requests("translate", (first, first), frozen, index, limits())
    with pytest.raises(ValueError, match="reading order"):
        pack_requests("translate", (second, first), frozen, index, limits())
    with pytest.raises(ValueError, match="unknown atomic item"):
        pack_requests("translate", doc.items, frozen, index, limits(), completed={"missing"})
    with pytest.raises(ValueError, match="exactly cover"):
        pack_requests(
            "review",
            doc.items,
            frozen,
            index,
            limits(),
            targets={first.item_id: record(first)},
            revisions={first.unit_id: 0, second.unit_id: 0},
        )
    wrong = record(second).model_copy(update={"item_id": first.item_id})
    with pytest.raises(ValueError, match="identity differs"):
        pack_requests(
            "review",
            (first,),
            frozen,
            index,
            limits(),
            targets={first.item_id: wrong},
            revisions={first.unit_id: 0},
        )


@pytest.mark.parametrize("field", ("revisions", "term_ids_by_item", "terms_hashes", "context_hashes", "input_hashes"))
def test_batch_round_trip_rejects_forged_manifest_identity(field: str) -> None:
    doc = inventory("<p>Current source.</p>")
    item = doc.items[0]
    batch = pack_requests(
        "review",
        doc.items,
        glossary(),
        SourceIndex((doc,)),
        limits(),
        targets={item.item_id: record(item, "当前译文。")},
        revisions={item.unit_id: 7},
    ).batches[0]
    data = batch.model_dump(mode="python")
    key = item.unit_id if field == "revisions" else item.item_id
    data["manifest"][field][key] = (
        999 if field == "revisions" else ("ghost",) if field == "term_ids_by_item" else "forged"
    )

    with pytest.raises(ValueError, match="manifest"):
        RequestBatch.model_validate(data)


def test_heading_keeps_its_following_atom_when_the_pair_fits() -> None:
    doc = inventory(f"<p>{'word ' * 90}</p><h2>New section</h2><p>{'word ' * 90}</p>")
    previous, heading, following = doc.items
    index, frozen = SourceIndex((doc,)), glossary()
    request_id = "tx-" + "a" * 32
    pair_budget = measured(
        "translate", build_payload("translate", (heading, following), frozen, index, request_id=request_id)
    )
    constrained = limits(source=pair_budget.source_tokens)

    result = pack_requests("translate", doc.items, frozen, index, constrained)

    assert members(result) == [[previous.item_id], [heading.item_id, following.item_id]]
    assert result.boundaries[0].reason == "heading"
    assert result.batches[-1].items[-1] == following

    smaller = pack_requests("translate", doc.items, frozen, index, limits(source=pair_budget.source_tokens - 1))
    assert all(not (heading in batch.items and following in batch.items) for batch in smaller.batches)
    assert sum(len(batch.items) for batch in smaller.batches) + len(smaller.blocked) == 3


@pytest.mark.parametrize("field", ("revisions", "record_versions", "plan_epochs"))
def test_batch_json_rejects_boolean_versions(field: str) -> None:
    doc = inventory("<p>Current source.</p>")
    item = doc.items[0]
    batch = pack_requests(
        "review",
        doc.items,
        glossary(),
        SourceIndex((doc,)),
        limits(),
        targets={item.item_id: record(item, "当前译文。")},
        revisions={item.unit_id: 1},
    ).batches[0]
    data = batch.model_dump(mode="python")
    data["manifest"][field][item.unit_id] = True
    with pytest.raises(ValueError, match="non-negative integers"):
        RequestBatch.model_validate(data)
