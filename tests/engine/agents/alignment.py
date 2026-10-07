import json

from engine.agents.runtime import wire_hash
from engine.agents.wire import messages
from tests.engine.agents.wire import payload


def test_v4_translation_and_review_hashes_remain_frozen():
    assert wire_hash("translate", payload(), 1000, compact=True, wire_version="epubox-wire-4") == (
        "d200bf72cd31d394f4140caf5b19d6df4d2becc6153219346b033905b2aa0bcb"
    )
    assert wire_hash("review", payload("review"), 1000, compact=True, wire_version="epubox-wire-4") == (
        "e4961ccf05006ce31f2e42433223e3834681273f9ee526cb4c2d323ab19ff13b"
    )


def test_review_labels_matching_source_and_draft_slots_without_changing_old_wire():
    book = payload("review")
    item = book["items"][0]
    item["source"] = "⟦+b1⟧Tavily⟦-b1⟧⟦=x1⟧⟦+b2⟧ is used.⟦+g1⟧⟦-g1⟧ Its API is simple.⟦-b2⟧"
    item["target"] = "⟦+b1⟧Tavily⟦-b1⟧⟦=x1⟧⟦+b2⟧用于搜索。⟦+g1⟧⟦-g1⟧其接口简单。⟦-b2⟧"
    physical = json.loads(messages("review", book, "base")[1]["content"])["items"][0]
    assert physical["source"].count("<t") == physical["target"].count("<t") == 3
    assert physical["slot_ids"] == ["1", "2", "3"]
    old = json.loads(messages("review", book, "base", version="epubox-wire-4")[1]["content"])["items"][0]
    assert "<t" not in old["target"] and "slot_ids" not in old


def test_review_does_not_invent_correspondence_for_a_different_valid_draft_layout():
    book = payload("review")
    item = book["items"][0]
    item["source"] = "⟦+g1⟧One⟦-g1⟧⟦+g2⟧Two⟦-g2⟧"
    item["target"] = "⟦+g2⟧二⟦-g2⟧⟦+g1⟧一⟦-g1⟧"
    physical = json.loads(messages("review", book, "base")[1]["content"])["items"][0]
    assert physical["target"] == "<g2>二</g2><g1>一</g1>"
    assert physical["slot_ids"] == ["1", "2"]


def test_review_contract_shows_closed_json_and_distinguishes_labels_from_book_tags():
    prompt = messages("review", payload("review"), "base")[0]["content"]
    assert "t labels are not book tags" in prompt
    assert '"checks":{"accuracy":"pass"' in prompt
    assert "do not repeat keys or append commentary" in prompt
