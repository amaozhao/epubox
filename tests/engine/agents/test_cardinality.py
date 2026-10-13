"""Body response bounds must cover the token-budgeted request manifest."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from engine.agents import workflow
from engine.agents.protocol import ProtocolError, validate_translation_response
from engine.agents.terms import validate_review_response
from engine.orchestrator import run_translation
from engine.services import failures
from engine.services.journal import BodyJournal
from tests.engine.agents.test_workflow import prepare_case
from tests.engine.execution.test_atomic import answer


def response(stage, count):
    payload = {
        "request_id": "large-index",
        "items": [
            {
                "item_id": f"i{number}",
                "source": "Index entry",
                "base_revision": 0,
                "applicability": {"terminology": False, "bindings": False},
            }
            for number in range(count)
        ],
    }
    return answer(stage, payload)["raw"]


def validate(stage, raw, count):
    if stage == "translate":
        return validate_translation_response(raw, "large-index", {f"i{number}" for number in range(count)})
    return validate_review_response(
        raw,
        "large-index",
        {
            f"i{number}": {
                "base_revision": 0,
                "terminology_applicable": False,
                "bindings_applicable": False,
            }
            for number in range(count)
        },
    )


@pytest.mark.parametrize("stage", ["translate", "review"])
@pytest.mark.parametrize("count", [256, 257, 262])
def test_body_response_accepts_every_requested_item(stage, count):
    result = validate(stage, response(stage, count), count)
    assert len(result.accepted) == count
    assert not result.errors and not result.missing and not result.unknown


@pytest.mark.parametrize("stage", ["translate", "review"])
@pytest.mark.parametrize("count", [1, 262])
def test_body_response_still_bounds_unsolicited_array_growth(stage, count):
    with pytest.raises(ProtocolError):
        validate(stage, response(stage, max(256, count) + 1), count)


@pytest.mark.parametrize("stage", ["translate", "review"])
def test_large_response_still_rejects_duplicate_ids_and_isolates_unknown_ids(stage):
    body = json.loads(response(stage, 262))
    body["items"][1]["item_id"] = "i0"
    body["items"][2]["item_id"] = "unknown"
    result = validate(stage, json.dumps(body), 262)
    assert result.errors == {"i0": "duplicate item_id"}
    assert set(result.missing) == {"i1", "i2"}
    assert result.unknown == ("unknown",)
    assert len(result.accepted) == 259


@pytest.fixture
def index_case(tmp_path):
    projections = tuple(f"Index {number}." for number in range(262))
    case = prepare_case(
        tmp_path,
        "".join(f"<p>{text}</p>" for text in projections),
        projections,
        output_budget_version=7,
    )
    assert len(case.batch.items) == 262
    return case


def test_262_short_index_entries_complete_and_resume_without_retranslation(index_case):
    case = index_case
    calls = []

    async def transport(stage, payload):
        calls.append((stage, tuple(item["item_id"] for item in payload["items"])))
        return answer(stage, payload)

    def run():
        return asyncio.run(
            run_translation(
                case.session.store.root,
                model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
                transport=transport,
            )
        )

    result = run()
    assert result.status == "translated", result.reason
    assert result.accepted_units == case.prepared.plan.required_unit_count
    for stage in ("translate", "review"):
        requested = [item for kind, items in calls if kind == stage for item in items]
        assert len(requested) == len(set(requested))
        assert set(case.batch.manifest.item_ids).issubset(requested)
    before = len(calls)
    resumed = run()
    assert resumed.status == "translated" and len(calls) == before


@pytest.mark.parametrize("stage", ["translate", "review"])
def test_exhausted_legacy_limit_resumes_and_preserves_valid_drafts(index_case, stage, monkeypatch):
    case = index_case
    journal = BodyJournal(case.session.store)
    old_error = "items exceeds 256" if stage == "translate" else "items must be an array of at most 256 entries"
    calls = []

    async def transport(kind, payload):
        calls.append((kind, tuple(item["item_id"] for item in payload["items"])))
        return answer(kind, payload)

    def old_validator(*_args):
        raise ProtocolError(old_error)

    with monkeypatch.context() as old:
        old.setattr(
            workflow, f"validate_{'translation' if stage == 'translate' else 'review'}_response", old_validator
        )
        result = asyncio.run(
            workflow.run_workflow(
                case.prepared,
                case.batch,
                case.index,
                journal.runtime(transport=transport),
                session=case.session,
                save=journal.save,
                records=journal.records(case.batch.manifest.item_ids),
            )
        )
    assert result.status == "needs_attention" and any(old_error in issue for issue in result.issues)
    before = len(calls)
    monkeypatch.setattr(failures, "failure_count", lambda *_args: failures.CONTENT_FAILURE_LIMIT)
    recovered = asyncio.run(
        run_translation(
            case.session.store.root,
            model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
            transport=transport,
            reopen_attention=True,
        )
    )
    assert recovered.status == "translated", recovered.reason
    if stage == "review":
        body_ids = set(case.batch.manifest.item_ids)
        assert not any(kind == "translate" and body_ids.intersection(items) for kind, items in calls[before:])
