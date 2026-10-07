import asyncio
import json
from collections import Counter

import pytest

from engine.orchestrator import run_translation
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
