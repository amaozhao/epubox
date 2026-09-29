import json

import pytest

from engine.agents.protocol import ProtocolError
from engine.agents.runtime import Stage, request_messages
from engine.agents.term_protocol import (
    validate_resolution_response,
    validate_review_response,
    validate_terms_response,
)


def _terms(items: list[dict]) -> str:
    return json.dumps({"protocol": "epubox-terms-1", "request_id": "r1", "items": items})


def test_terms_response_keeps_valid_items_and_reports_bad_candidates() -> None:
    candidate = {
        "source": "memory",
        "target": "内存",
        "category": "term",
        "evidence": [{"view_id": "v1", "source_quote": "memory allocation"}],
    }
    result = validate_terms_response(
        _terms(
            [
                {"item_id": "i1", "candidates": [candidate, candidate | {"mode": "required"}]},
                {"item_id": "i2", "candidates": []},
                {"item_id": "i3", "candidates": [candidate]},
            ]
        ),
        "r1",
        {"i1", "i2", "i4"},
    )
    assert len(result.accepted["i1"]) == 1
    assert result.accepted["i2"] == ()
    assert result.rejected_candidates["i1"] == ("candidate 1: candidate contains unknown fields",)
    assert result.missing == ("i4",)
    assert result.unknown == ("i3",)


@pytest.mark.parametrize(
    "raw",
    [
        '{"protocol":"epubox-terms-1","request_id":"r1","items":[],"items":[]}',
        '{"protocol":"epubox-terms-1","request_id":"other","items":[]}',
        '{"protocol":"epubox-terms-1","request_id":"r1","items":',
    ],
)
def test_terms_reject_untrusted_root(raw: str) -> None:
    with pytest.raises(ProtocolError):
        validate_terms_response(raw, "r1", set())


def test_resolution_only_selects_requested_candidates_and_units() -> None:
    valid = {
        "protocol": "epubox-term-resolution-1",
        "request_id": "r1",
        "group_id": "g1",
        "decision": "select",
        "selected_candidate_ids": ["c1"],
        "restricted_unit_ids": ["u1"],
        "reason": "The quotation uses the technical sense.",
    }
    assert validate_resolution_response(json.dumps(valid), "r1", "g1", {"c1"}, {"u1"}) == valid
    for invalid in (
        valid | {"selected_candidate_ids": ["ghost"]},
        valid | {"selected_candidate_ids": ["c1", "c2"]},
        valid | {"restricted_unit_ids": ["ghost"]},
        valid | {"decision": "defer"},
    ):
        with pytest.raises(ProtocolError):
            validate_resolution_response(json.dumps(invalid), "r1", "g1", {"c1", "c2"}, {"u1"})


def test_single_runtime_builds_v25_term_and_review_prompts() -> None:
    stages: tuple[tuple[Stage, str], ...] = (
        ("terms", "epubox-terms-1"),
        ("resolution", "epubox-term-resolution-1"),
        ("review", "epubox-review-2"),
    )
    for stage, protocol in stages:
        messages = request_messages(stage, {"protocol": protocol, "request_id": "r1", "items": []})
        assert protocol in messages[0]["content"]
        assert json.loads(messages[1]["content"])["request_id"] == "r1"
    with pytest.raises(ValueError, match="protocol does not match"):
        request_messages("terms", {"protocol": "epubox-review-2", "request_id": "r1"})


def test_review_keeps_major_issue_when_optional_suggestion_is_bad() -> None:
    item = {
        "item_id": "i1",
        "base_revision": 1,
        "decision": "needs_attention",
        "checks": {
            "accuracy": "fail",
            "fluency": "pass",
            "terminology": "pass",
            "bindings": "pass",
            "script": "pass",
        },
        "issues": [{"code": "wrong_sense", "severity": "major", "message": "Wrong sense of memory"}],
        "term_suggestions": [{"source": "memory", "target": "内存", "category": "term", "evidence": []}],
    }
    raw = json.dumps({"protocol": "epubox-review-2", "request_id": "r1", "items": [item]})
    result = validate_review_response(raw, "r1", {"i1": {"base_revision": 1}})
    assert result.accepted["i1"]["issues"] == item["issues"]
    assert result.accepted["i1"]["term_suggestions"] == []
    assert result.rejected_suggestions["i1"]
