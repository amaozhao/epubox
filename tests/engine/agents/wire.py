"""Compact provider wire protocol tests."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from engine.agents.wire import VERSION, decode, decode_projection, digest, encode_projection, identity, messages
from engine.item.inline import ProjectionError, normalize_empty_closes, parse_projection, validate_projection


def payload(kind: str = "translate") -> dict:
    item = {
        "item_id": "u-long-one",
        "source": r"A & <tag> \\ \⟦marker\⟧⟦+g1⟧中⟦=x1⟧⟦+b1⟧文⟦-b1⟧⟦-g1⟧ &amp;",
        "context": {},
        "terms": [],
        "hints": {"g1": {"element": "span"}, "x1": {"element": "code", "class": "code", "excerpt": "x"}},
        "constraints": {
            "g1": {"kind": "g", "parent": "root", "movement": "locked", "reorder_allowed": False, "fixed_order": []},
            "x1": {"kind": "x", "parent": "g1", "movement": "fixed", "reorder_allowed": False, "fixed_order": []},
            "b1": {"kind": "b", "parent": "g1", "movement": "locked", "reorder_allowed": False, "fixed_order": []},
        },
    }
    if kind == "review":
        item |= {
            "target": "甲⟦+g1⟧乙⟦=x1⟧⟦+b1⟧丙⟦-b1⟧⟦-g1⟧",
            "base_revision": 3,
            "applicability": {"terminology": False, "bindings": True},
            "bindings": [{"ref": "g1", "source": "中", "target": "乙"}],
            "required_revision": [{"code": "meaning", "message": "fix"}],
        }
    return {
        "protocol": "epubox-text-1" if kind == "translate" else "epubox-review-2",
        "prompt_version": "epubox-members-1",
        "request_id": "request-full",
        "target_language": "zh-Hans",
        "context": [],
        "items": [item],
    }


def test_projection_round_trip_preserves_text_and_nested_marker_identities() -> None:
    source = payload()["items"][0]["source"]
    compact = encode_projection(source)
    assert "<g1>" in compact and "<x1/>" in compact and "<b1>" in compact
    assert "&amp;amp;" in compact and "&lt;tag&gt;" in compact
    decoded = decode_projection(compact)
    assert parse_projection(decoded) == parse_projection(source)


@pytest.mark.parametrize(
    "bad",
    ["<q1>x</q1>", "<x1>x</x1>", "<g1/>", "x &copy;", "x > y"],
)
def test_projection_decoder_rejects_unknown_or_malformed_ascii(bad: str) -> None:
    with pytest.raises(ValueError):
        decode_projection(bad)


def test_projection_decoder_leaves_source_aware_structure_repair_to_validation() -> None:
    empty_source = "⟦+g1⟧⟦-g1⟧"
    duplicate_close = decode_projection("<g1></g1></g1>")
    assert normalize_empty_closes(empty_source, duplicate_close) == empty_source

    crossed_source = "⟦+g1⟧⟦+b1⟧文⟦-b1⟧⟦-g1⟧"
    crossed = decode_projection("<g1><b1>文</g1></b1>")
    with pytest.raises(ProjectionError, match="crossed or unmatched"):
        validate_projection(crossed_source, crossed)

    nonempty_source = "⟦+g1⟧文⟦-g1⟧"
    extra_close = decode_projection("<g1>文</g1></g1>")
    assert normalize_empty_closes(nonempty_source, extra_close) == extra_close
    with pytest.raises(ProjectionError, match="crossed or unmatched"):
        validate_projection(nonempty_source, extra_close)


def test_messages_compact_defaults_without_mutating_canonical_payload() -> None:
    original = payload()
    saved = copy.deepcopy(original)
    result = messages("translate", original, "base")
    physical = json.loads(result[1]["content"])
    item = physical["items"][0]
    assert original == saved
    assert physical["request_id"] == "request-full" and item["item_id"] == "1"
    assert "prompt_version" not in physical and "context" not in physical
    assert "terms" not in item and "context" not in item and "constraints" not in item
    assert item["hints"] == {"g1": "span", "x1": {"element": "code", "class": "code"}}
    assert item["source"].count("<t") == 4
    assert "every source slot number exactly once" in result[0]["content"]


def test_v2_v3_wire_hashes_stay_frozen_and_v4_hides_code_content():
    from engine.agents.runtime import wire_hash

    assert (
        wire_hash("translate", payload(), 1000, compact=True, wire_version="epubox-wire-2")
        == "9bc535c6436d44a4c19693ccf9955c07f06b36582604c0f42225085edd0127a9"
    )
    assert (
        wire_hash("translate", payload(), 1000, compact=True, wire_version="epubox-wire-3")
        == "595e091af5588d055f101661534085082c153155f899fbcc431201cd079b34e1"
    )
    book = payload()
    book["items"][0]["hints"]["x1"].update(readonly="DO_NOT_SEND_CODE", excerpt="DO_NOT_SEND_CODE")
    current = messages("translate", book, "base")
    old = messages("translate", book, "base", version="epubox-wire-2")
    assert "DO_NOT_SEND_CODE" not in current[1]["content"]
    assert "DO_NOT_SEND_CODE" in old[1]["content"]
    assert book["items"][0]["hints"]["x1"]["readonly"] == "DO_NOT_SEND_CODE"


def test_messages_retain_nondefault_constraints_and_term_semantics() -> None:
    original = payload()
    item = original["items"][0]
    item["constraints"]["g1"]["reorder_allowed"] = True
    item["terms"] = [
        {
            "term_id": "t1",
            "role": "target",
            "mode": "required",
            "source": "agent",
            "target": "智能体",
            "aliases": [],
            "match_policy": "casefold",
            "note": "",
        }
    ]
    physical = json.loads(messages("translate", original, "base")[1]["content"])
    compact = physical["items"][0]
    assert compact["constraints"] == item["constraints"]
    assert compact["terms"] == [
        {
            "term_id": "t1",
            "role": "target",
            "mode": "required",
            "source": "agent",
            "target": "智能体",
            "match_policy": "casefold",
        }
    ]


def test_review_keeps_revision_contract_and_drops_derivable_bindings() -> None:
    physical = json.loads(messages("review", payload("review"), "base")[1]["content"])
    item = physical["items"][0]
    assert item["base_revision"] == 3
    assert item["applicability"] == {"terminology": False, "bindings": True}
    assert item["required_revision"] == [{"code": "meaning", "message": "fix"}]
    assert "bindings" not in item
    assert "<t1>" in item["source"] and "<t1>" not in item["target"]
    assert "script is pass or fail for Chinese" in messages("review", payload("review"), "base")[0]["content"]


def test_v4_slot_round_trip_freezes_structure_and_escapes_injected_markers() -> None:
    source = payload()["items"][0]["source"]
    raw = json.dumps(
        {
            "protocol": "epubox-text-1",
            "request_id": "request-full",
            "items": [
                {
                    "item_id": "1",
                    "target": {
                        "1": "甲⟦+g9⟧",
                        "2": "乙",
                        "3": "丙",
                        "4": "丁",
                    },
                }
            ],
        }
    )
    result = json.loads(
        decode(
            "translate",
            raw,
            "request-full",
            ("u-long-one",),
            version=VERSION,
            sources={"u-long-one": source},
        )
    )
    target = result["items"][0]["target"]
    assert validate_projection(source, target)
    assert [event.value for event in parse_projection(target) if event.kind == "marker"] == [
        "+g1",
        "=x1",
        "+b1",
        "-b1",
        "-g1",
    ]
    assert "\\⟦+g9\\⟧" in target


@pytest.mark.parametrize(
    "target",
    [
        {"1": "甲", "2": "乙", "3": "丙"},
        {"1": "甲", "2": "乙", "3": "丙", "4": "丁", "5": "戊"},
        {"1": "甲", "2": "乙", "3": "丙", "4": 4},
        {"1": "甲", "2": "乙", "3": "丙", "4": ""},
        {"1": "甲<g1>", "2": "乙", "3": "丙", "4": "丁"},
    ],
)
def test_v4_decode_rejects_invalid_slot_objects(target: object) -> None:
    raw = json.dumps(
        {
            "protocol": "epubox-text-1",
            "request_id": "request-full",
            "items": [{"item_id": "1", "target": target}],
        }
    )
    item = json.loads(
        decode(
            "translate",
            raw,
            "request-full",
            ("u-long-one",),
            version=VERSION,
            sources={"u-long-one": payload()["items"][0]["source"]},
        )
    )["items"][0]
    assert item["target"] is None


def test_v4_allows_raw_marker_text_only_when_it_was_literal_source_text() -> None:
    source = "Show <b1> literally."
    raw = json.dumps(
        {
            "protocol": "epubox-text-1",
            "request_id": "request-full",
            "items": [{"item_id": "1", "target": {"1": "显示 <b1> 字样。"}}],
        }
    )
    item = json.loads(
        decode(
            "translate",
            raw,
            "request-full",
            ("u-long-one",),
            version=VERSION,
            sources={"u-long-one": source},
        )
    )["items"][0]
    assert item["target"] == "显示 <b1> 字样。"


def test_v4_review_replacement_uses_source_slots_and_no_change_has_no_target() -> None:
    book = payload("review")
    source = book["items"][0]["source"]
    raw = json.dumps(
        {
            "protocol": "epubox-review-2",
            "request_id": "request-full",
            "items": [
                {
                    "item_id": "1",
                    "base_revision": 3,
                    "decision": "replace",
                    "checks": {},
                    "issues": [],
                    "target": {"1": "甲", "2": "乙", "3": "丙", "4": "丁"},
                },
                {
                    "item_id": "1",
                    "base_revision": 3,
                    "decision": "no_change",
                    "checks": {},
                    "issues": [],
                },
            ],
        }
    )
    items = json.loads(
        decode(
            "review",
            raw,
            "request-full",
            ("u-long-one",),
            version=VERSION,
            sources={"u-long-one": source},
        )
    )["items"]
    assert validate_projection(source, items[0]["target"])
    assert "target" not in items[1]


def test_v4_prevents_malformed_copied_markers_that_v3_can_decode() -> None:
    source = "⟦+g1⟧one⟦+b1⟧two⟦-b1⟧⟦-g1⟧"
    malformed = "<g1>甲<b1>乙</g1></b1>"
    legacy_raw = json.dumps(
        {"protocol": "epubox-text-1", "request_id": "request-full", "items": [{"item_id": "1", "target": malformed}]}
    )
    legacy = json.loads(
        decode(
            "translate",
            legacy_raw,
            "request-full",
            ("u-long-one",),
            version="epubox-wire-3",
            sources={"u-long-one": source},
        )
    )["items"][0]["target"]
    with pytest.raises(ProjectionError, match="crossed or unmatched"):
        validate_projection(source, legacy)

    v4_raw = json.dumps(
        {
            "protocol": "epubox-text-1",
            "request_id": "request-full",
            "items": [{"item_id": "1", "target": {"1": "甲", "2": "乙"}}],
        }
    )
    repaired = json.loads(
        decode(
            "translate",
            v4_raw,
            "request-full",
            ("u-long-one",),
            version=VERSION,
            sources={"u-long-one": source},
        )
    )["items"][0]["target"]
    assert validate_projection(source, repaired)


@pytest.mark.parametrize("decision", ["no_change", "replace"])
def test_decode_maps_only_exact_short_ids_and_decodes_replacement(decision: str) -> None:
    item = {
        "item_id": "1",
        "base_revision": 3,
        "decision": decision,
        "checks": {},
        "issues": [],
    }
    if decision == "replace":
        item["target"] = "甲<g1>乙</g1>"
    raw = json.dumps({"protocol": "epubox-review-2", "request_id": "request-full", "items": [item]})
    result = json.loads(decode("review", raw, "request-full", ("u-long-one",)))
    assert result["items"][0]["item_id"] == "u-long-one"
    if decision == "replace":
        assert result["items"][0]["target"] == "甲⟦+g1⟧乙⟦-g1⟧"
    else:
        assert "target" not in result["items"][0]


def test_decode_never_binds_unknown_or_duplicate_ids_by_order() -> None:
    raw = json.dumps(
        {
            "protocol": "epubox-text-1",
            "request_id": "request-full",
            "items": [
                {"item_id": "u-long-one", "target": "甲"},
                {"item_id": "1", "target": "乙"},
                {"item_id": "1", "target": "丙"},
                {"item_id": "9", "target": "丁"},
            ],
        }
    )
    items = json.loads(decode("translate", raw, "request-full", ("u-long-one",)))["items"]
    assert [item["item_id"] for item in items] == [
        "unknown-wire:u-long-one",
        "u-long-one",
        "u-long-one",
        "unknown-wire:9",
    ]


def test_decode_keeps_bad_roots_raw_and_localizes_bad_targets() -> None:
    malformed = '{"protocol":"epubox-text-1","protocol":"epubox-text-1"}'
    assert decode("translate", malformed, "request-full", ("u",)) == malformed
    wrong = '{"protocol":"other","request_id":"request-full","items":[]}'
    assert decode("translate", wrong, "request-full", ("u",)) == wrong
    raw = '{"protocol":"epubox-text-1","request_id":"request-full","items":[{"item_id":"1","target":"<z1>"}]}'
    assert json.loads(decode("translate", raw, "request-full", ("u",)))["items"][0]["target"] is None


def test_digest_and_persisted_identity_are_strict() -> None:
    physical = messages("translate", payload(), "base")
    wire_hash = digest(physical, 4096)
    assert len(wire_hash) == 64 and digest(physical, 4096) == wire_hash
    request = SimpleNamespace(
        stage="translate",
        wire_hash="legacy",
        attempts=(SimpleNamespace(attempt_id="a", metadata={"wire_version": VERSION, "wire_hash": wire_hash}),),
    )
    assert identity(request, "a") == (VERSION, wire_hash)
    request.attempts = (SimpleNamespace(attempt_id="a", metadata={}),)
    assert identity(request, "a") == (None, "legacy")
    request.attempts = (SimpleNamespace(attempt_id="a", metadata={"wire_version": VERSION}),)
    with pytest.raises(ValueError, match="incomplete"):
        identity(request, "a")
    request.attempts = (SimpleNamespace(attempt_id="a", metadata={"wire_version": VERSION, "wire_hash": wire_hash}),)
    request.stage = "terms"
    with pytest.raises(ValueError, match="forbidden"):
        identity(request, "a")
