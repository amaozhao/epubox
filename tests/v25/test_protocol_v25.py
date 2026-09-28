import json

import pytest

from engine.agents.protocol_v23 import ProtocolError
from engine.agents.protocol_v25 import validate_resolution_response, validate_terms_response


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
        valid | {"restricted_unit_ids": ["ghost"]},
        valid | {"decision": "defer"},
    ):
        with pytest.raises(ProtocolError):
            validate_resolution_response(json.dumps(invalid), "r1", "g1", {"c1"}, {"u1"})
