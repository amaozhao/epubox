import asyncio
import json
from collections import Counter

import pytest

from engine.orchestrator import run_translation
from engine.services.journal import BodyJournal
from tests.engine.agents.workflow import prepare_case
from tests.engine.execution.atomic import answer


def test_final_reason_contains_only_current_member_failures(tmp_path):
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p>", ("First.", "Second."))
    first, second = (item.item_id for item in case.batch.items)
    reviews = Counter()

    async def transport(stage, payload):
        response = answer(stage, payload)
        if stage != "review":
            return response
        value = json.loads(response["raw"])
        for item in value["items"]:
            reviews[item["item_id"]] += 1
            message = "fixed later" if item["item_id"] == first else "still broken"
            if item["item_id"] == second or reviews[first] == 1:
                item.update(
                    decision="needs_attention",
                    issues=[{"code": "accuracy", "severity": "major", "message": message}],
                )
        response["raw"] = json.dumps(value)
        return response

    result = asyncio.run(run_translation(case.session.store.root, transport=transport))

    assert result.status == "needs_attention"
    assert result.reason is not None and "fixed later" not in result.reason
    assert result.reason.count("still broken") == 1
    assert first not in result.reason and second in result.reason
    assert result.reason.startswith(f"review:{second}:")


@pytest.mark.parametrize("recover", (True, False))
def test_shared_unknown_review_retries_are_bounded(tmp_path, recover):
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p>", ("First.", "Second."))
    expected = {item.item_id for item in case.batch.items}
    failed = case.batch.items[0].item_id
    reviews = []

    async def transport(stage, payload):
        ids = {item["item_id"] for item in payload["items"]}
        if stage == "review" and failed in ids:
            reviews.append(ids)
            if not recover or len(reviews) < 3:
                raise TimeoutError("review timed out")
        return answer(stage, payload)

    result = asyncio.run(run_translation(case.session.store.root, transport=transport))

    assert result.status == ("translated" if recover else "needs_attention")
    assert reviews == [expected, expected, expected]
    if recover:
        journal = BodyJournal(case.session.store)
        request = next(
            value for value in journal._requests.values() if value.stage == "review" and len(value.attempts) == 3
        )
        assert not journal._ambiguous(request)


def test_late_review_timeout_uses_transport_retries_after_content_retries(tmp_path):
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    failed = case.batch.items[0].item_id
    reviews = []

    async def transport(stage, payload):
        if stage == "review" and failed in {item["item_id"] for item in payload["items"]}:
            reviews.append(payload["request_id"])
            if len(reviews) < 3:
                response = answer(stage, payload)
                value = json.loads(response["raw"])
                value["items"][0].update(
                    decision="needs_attention",
                    issues=[{"code": "accuracy", "severity": "major", "message": "retry"}],
                )
                response["raw"] = json.dumps(value)
                return response
            if len(reviews) == 3:
                raise TimeoutError("Request timed out.")
        return answer(stage, payload)

    result = asyncio.run(run_translation(case.session.store.root, transport=transport))

    assert result.status == "translated"
    assert len(reviews) == 4
    assert reviews[2] == reviews[3]
