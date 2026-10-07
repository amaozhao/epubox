from dataclasses import replace
from typing import Any, cast

import pytest

from engine.core.config import Settings
from engine.epub import preparation as preparation_module
from engine.epub.preparation import _frozen_translation_config
from engine.item.atoms import ADAPTER_VERSION, EXTRACTOR_VERSION, extract_resource
from engine.item.members import MemberIndex, materialize_members, pack_members
from engine.services.preflight import preflight_atomic_resources
from engine.services.ready import limits_from_config
from tests.engine.item.members import glossary, limits, prepared, source
from tests.engine.services.preparing import atomic_config


@pytest.mark.parametrize("minimum", (100, 300, 500))
def test_environment_body_minimum_is_frozen_in_the_request_policy(monkeypatch, minimum):
    monkeypatch.setenv("EPUB_CHUNK_MIN_TOKENS", str(minimum))
    settings = cast(Any, Settings)(_env_file=None)
    monkeypatch.setattr(preparation_module, "settings", settings)
    config = replace(atomic_config(), adapter_version=ADAPTER_VERSION, extractor_version=EXTRACTOR_VERSION)
    frozen = _frozen_translation_config(config)
    capacity = limits_from_config(frozen)
    assert frozen["minimum_source_tokens"] == capacity.minimum_source_tokens == minimum
    assert capacity.source_ceiling == capacity.source_tokens + 1000


def packed(body: str, minimum: int):
    inventory, report = prepared(body, cap=2000)
    members = materialize_members((inventory,), report)
    index = MemberIndex((inventory,), report, members)
    capacity = replace(
        limits(),
        output_tokens=4096,
        context_tokens=32768,
        output_version=4,
        minimum_source_tokens=minimum,
    )
    return pack_members("translate", members, glossary(), index, capacity)


def test_small_body_tail_rebalances_whole_members_to_reach_500_tokens():
    body = "".join("<p>" + "word " * 250 + "</p>" for _ in range(6))
    legacy = packed(body, 0)
    planned = packed(body, 500)

    assert [batch.budget.source_tokens for batch in legacy.batches] == [1255, 251]
    assert [batch.budget.source_tokens for batch in planned.batches] == [753, 753]
    assert [boundary.reason for boundary in planned.boundaries] == ["minimum", "end"]
    assert [item.item_id for batch in planned.batches for item in batch.items] == [
        item.item_id for batch in legacy.batches for item in batch.items
    ]


def test_one_short_html_body_is_an_explicit_unavoidable_exception():
    planned = packed("<p>Short body.</p>", 500)

    assert len(planned.batches) == 1
    assert planned.batches[0].budget.source_tokens < 500
    assert planned.boundaries[0].reason == "minimum_unavoidable"
    assert "HTML body has no adjacent request batch" in planned.boundaries[0].failures[0]


def test_minimum_rule_uses_body_source_tokens_and_excludes_metadata():
    raw = source("<p>Short body.</p>").replace(b"<head/>", b"<head><title>Book Code</title></head>")
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    capacity = replace(limits(), minimum_source_tokens=500)
    report = preflight_atomic_resources((inventory,), {"OPS/chapter.xhtml": raw}, capacity, "gpt-3.5-turbo")
    members = materialize_members((inventory,), report)
    planned = pack_members("translate", members, glossary(), MemberIndex((inventory,), report, members), capacity)

    metadata = next(batch for batch in planned.batches if batch.items[0].channel == "metadata")
    assert metadata.budget.source_tokens < 500
    boundary = planned.boundaries[planned.batches.index(metadata)]
    assert boundary.reason != "minimum_unavoidable"


def test_short_complete_html_bodies_remain_one_request_per_html():
    first_raw = source("<p>First short chapter.</p>")
    second_raw = source("<p>Second short chapter.</p>")
    first = extract_resource(first_raw, "OPS/first.xhtml", "book")
    second = extract_resource(second_raw, "OPS/second.xhtml", "book")
    capacity = replace(limits(), minimum_source_tokens=500)
    report = preflight_atomic_resources(
        (first, second),
        {"OPS/first.xhtml": first_raw, "OPS/second.xhtml": second_raw},
        capacity,
        "gpt-3.5-turbo",
    )
    members = materialize_members((first, second), report)
    planned = pack_members("translate", members, glossary(), MemberIndex((first, second), report, members), capacity)

    assert len(planned.batches) == 2
    assert {batch.items[0].document_id for batch in planned.batches} == {
        first.document.document_id,
        second.document.document_id,
    }
    assert all(batch.context == () for batch in planned.batches)
    assert all(boundary.reason == "minimum_unavoidable" for boundary in planned.boundaries)


def test_attributes_between_body_atoms_do_not_create_separate_small_body_requests():
    inventory, report = prepared('<p>First<img alt="Image caption"/></p><p>Second</p>')
    members = materialize_members((inventory,), report)
    index = MemberIndex((inventory,), report, members)
    planned = pack_members("translate", members, glossary(), index, replace(limits(), minimum_source_tokens=500))
    body = [batch for batch in planned.batches if batch.items[0].channel == "body"]
    assert len(body) == 1 and len(body[0].items) == 2
    assert any(batch.items[0].channel == "attribute" for batch in planned.batches)
    assert sum(len(batch.items) for batch in planned.batches) == len(members)


def test_soft_body_target_floats_up_to_absorb_a_tail_instead_of_sending_it_alone():
    body = "".join("<p>" + "word " * 100 + "</p>" for _ in range(8))
    inventory, report = prepared(body, cap=2000)
    members = materialize_members((inventory,), report)
    index = MemberIndex((inventory,), report, members)
    capacity = replace(
        limits(),
        source_tokens=700,
        source_tolerance_tokens=1000,
        minimum_source_tokens=500,
        output_version=5,
        output_tokens=10000,
    )
    planned = pack_members("translate", members, glossary(), index, capacity)
    assert planned.ready and len(planned.batches) == 1
    assert 700 < planned.batches[0].budget.source_tokens <= 1700
    assert planned.batches[0].payload["wire_version"] == "epubox-wire-5"


def test_no_small_split_body_request_is_emitted_when_provider_limits_prevent_merging():
    body = "".join("<p>" + "word " * 100 + "</p>" for _ in range(6))
    inventory, report = prepared(body, cap=2000)
    members = materialize_members((inventory,), report)
    index = MemberIndex((inventory,), report, members)
    capacity = replace(
        limits(),
        source_tokens=2000,
        source_tolerance_tokens=1000,
        minimum_source_tokens=500,
        output_version=5,
        output_tokens=650,
    )
    planned = pack_members("translate", members, glossary(), index, capacity)
    assert not planned.ready
    assert not any(batch.items[0].channel == "body" and batch.budget.source_tokens < 500 for batch in planned.batches)
