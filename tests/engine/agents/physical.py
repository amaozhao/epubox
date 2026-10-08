import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from engine.agents import wire
from engine.agents.runtime import ModelRuntime, model_input_budget, request_messages
from engine.agents.workflow import run_workflow
from engine.schemas.contracts import Attempt, ItemStatus
from engine.services import state
from engine.services.atomic import IdentityMismatch
from engine.services.journal import BodyJournal
from engine.services.resume import plan_resume
from tests.engine.agents.runtime import FakeOpenAIClient
from tests.engine.agents.wire import payload as wire_payload
from tests.engine.agents.workflow import MODEL, prepare_case, review_item


class Model:
    id = MODEL
    max_tokens = 10_000
    max_completion_tokens = None
    max_retries = 2

    def __init__(self, client):
        self.client = client

    def get_async_client(self):
        return self.client

    def _format_all_messages(self, messages, _compress):
        return [{"role": message.role, "content": message.content} for message in messages]

    def get_request_params(self, **_kwargs):
        return {"max_tokens": self.max_tokens}


def test_v8_provider_hash_budget_and_saved_response_use_actual_slot_map():
    book = wire_payload("review")
    book["wire_version"] = "epubox-wire-8"
    book["items"][0]["source"] = "⟦+g1⟧Source one.⟦-g1⟧⟦=x1⟧⟦+b1⟧Source two.⟦-b1⟧"
    book["items"][0]["target"] = "⟦+g1⟧译文一。⟦-g1⟧⟦=x1⟧⟦+b1⟧译文二。⟦-b1⟧"
    persisted = []
    reserved = []
    sent = []

    class Completions:
        async def create(self, **kwargs):
            physical = json.loads(kwargs["messages"][1]["content"])
            item = physical["items"][0]
            assert isinstance(item["target"], dict)
            sent.append(kwargs)
            decision = review_item(item, decision="replace")
            decision["target"] = {slot: "修订译文。" for slot in item["slot_ids"]}
            raw = json.dumps({"protocol": book["protocol"], "request_id": book["request_id"], "items": [decision]})
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=raw), finish_reason="stop")],
                usage={"prompt_tokens": 10, "completion_tokens": 5},
                id="review-response",
                model=MODEL,
                system_fingerprint=None,
            )

    runtime = ModelRuntime(
        model=Model(FakeOpenAIClient(Completions())),
        model_max_output_tokens=8192,
        input_budget_version=2,
        provider_output_token_field="max_tokens",
        reserve_attempt=lambda _, attempt: reserved.append(attempt),
        persist_response=lambda *args: persisted.append(args[-1]),
    )
    result = asyncio.run(runtime.invoke("review", book, {"request_id": book["request_id"], "output_tokens": 8192}))
    assert reserved[0].metadata["wire_version"] == "epubox-wire-8"
    assert reserved[0].metadata["wire_hash"] == wire.digest(sent[0]["messages"], 8192)
    assert persisted[0]["metadata"]["wire_version"] == "epubox-wire-8"
    assert (
        reserved[0].reservation["estimated_input_tokens"]
        == model_input_budget(
            "review",
            book,
            compact=True,
            algorithm_version=2,
        )["estimated_input_tokens"]
    )
    target = json.loads(result["raw"])["items"][0]["target"]
    assert isinstance(target, str) and "⟦+" in target


def make_case(tmp_path):
    body = "<p>" + "".join(f"<span>Word {i}.</span>" for i in range(12)) + "</p>"
    source = "".join(f"⟦+g{i + 1}⟧Word {i}.⟦-g{i + 1}⟧" for i in range(12))
    return prepare_case(tmp_path, body, (source,))


@pytest.mark.parametrize("tamper_stage", ("translate", "review"))
@pytest.mark.parametrize("crash", (False, True))
@pytest.mark.parametrize("replace", (False, True))
def test_real_provider_adapter_saves_physical_wire_and_replays_canonical_results(
    tmp_path, tamper_stage, crash, replace, monkeypatch
):
    case = make_case(tmp_path)
    journal = BodyJournal(case.session.store, case.session)
    sent = {}

    class Crash(BaseException):
        pass

    persist = journal._persist

    def save_response(stage, request_id, attempt_id, response):
        persist(stage, request_id, attempt_id, response)
        if crash and stage == tamper_stage:
            raise Crash

    monkeypatch.setattr(journal, "_persist", save_response)

    class Completions:
        async def create(self, **kwargs):
            payload = json.loads(kwargs["messages"][1]["content"])
            kind = "translate" if payload["protocol"] == "epubox-text-1" else "review"
            assert payload["items"][0]["item_id"] == "1"
            assert "⟦" not in payload["items"][0]["source"]
            assert payload["items"][0]["constraints"]["g1"]["reorder_allowed"] is True
            if kind == "translate":
                items = []
                for item in payload["items"]:
                    target = {slot: "译文。" for slot in re.findall(r"<t(\d+)>", item["source"])}
                    items.append({"item_id": item["item_id"], "target": target})
            else:
                assert "bindings" not in payload["items"][0]
                items = [
                    review_item(item, decision="replace" if replace else "no_change") for item in payload["items"]
                ]
                if replace:
                    for item, decision in zip(payload["items"], items, strict=True):
                        decision["target"] = {slot: "修订译文。" for slot in re.findall(r"<t(\d+)>", item["source"])}
            raw = json.dumps({"protocol": payload["protocol"], "request_id": payload["request_id"], "items": items})
            sent[payload["request_id"]] = (kwargs["messages"], kwargs["max_tokens"], raw)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=raw), finish_reason="stop")],
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                id="provider-response",
                model=MODEL,
                system_fingerprint=None,
            )

    events = []
    workflow = run_workflow(
        case.prepared,
        case.batch,
        case.index,
        journal.runtime(model=Model(FakeOpenAIClient(Completions())), progress=events.append),
        session=case.session,
        save=journal.save,
    )
    if crash:
        with pytest.raises(Crash):
            asyncio.run(workflow)
    else:
        assert asyncio.run(workflow).status == "completed"
    restored = BodyJournal(case.session.store)
    for request_id, (messages, cap, raw) in sent.items():
        request = restored.store.read_request(request_id)
        attempt = request.attempts[-1]
        assert attempt.metadata["wire_version"] == wire.VERSION
        assert attempt.metadata["wire_hash"] == wire.digest(messages, cap)
        path = restored.store.root / "responses" / request.stage / request_id / f"{attempt.attempt_id}.json"
        saved = json.loads(state.read(path))
        assert saved["response"]["raw"] == raw
        assert saved["wire_hash"] == attempt.metadata["wire_hash"] != request.wire_hash
        logical = restored.store.read_model_response(request.stage, request_id, attempt.attempt_id)
        assert logical is not None
        assert json.loads(logical.raw)["items"][0]["item_id"] == request.item_ids[0]
        with pytest.raises(IdentityMismatch, match="physical wire"):
            restored.store.finish_attempt(
                request_id, attempt.attempt_id, state="succeeded", metadata={"wire_hash": "0" * 64}
            )
    expected_status = (
        (ItemStatus.PENDING if tamper_stage == "translate" else ItemStatus.LOCAL_VALID)
        if crash
        else ItemStatus.REVIEWED
    )
    assert all(restored.records()[item.item_id].status == expected_status for item in case.batch.items)
    assert not plan_resume(restored.store.root).reasons
    translated = restored.store.read_request(case.batch.manifest.request_id)
    expected = model_input_budget("translate", case.batch.payload, algorithm_version=2, compact=True)
    assert translated.attempts[-1].reservation["cl100k_input_tokens"] == expected["cl100k_tokens"]
    event = next(event for event in events if event.get("event") == "request" and event.get("stage") == "translate")
    assert event["estimated_input_tokens"] == expected["cl100k_tokens"]
    assert event["reserved_input_tokens"] == expected["estimated_input_tokens"]
    assert (
        request_messages("translate", case.batch.payload)[1]["content"] != sent[translated.request_id][0][1]["content"]
    )
    tampered = next(request for request in restored._requests.values() if request.stage == tamper_stage)
    request_path = restored.store.root / "requests" / f"{tampered.request_id}.json"
    altered = json.loads(state.read(request_path))
    altered["attempts"][-1]["metadata"]["wire_hash"] = "0" * 64
    state.write(request_path, json.dumps(altered).encode())
    response_path = (
        restored.store.root
        / "responses"
        / tamper_stage
        / tampered.request_id
        / f"{tampered.attempts[-1].attempt_id}.json"
    )
    altered_response = json.loads(state.read(response_path))
    altered_response["wire_hash"] = "0" * 64
    state.write(response_path, json.dumps(altered_response).encode())
    before = state.read(request_path)
    with pytest.raises((ValueError, IdentityMismatch), match="physical wire"):
        BodyJournal(restored.store)
    assert any("physical wire" in reason for reason in plan_resume(restored.store.root).reasons)
    assert state.read(request_path) == before


def test_physical_response_tampering_and_invalid_marker_do_not_pass_as_canonical(tmp_path):
    case = make_case(tmp_path)
    journal = BodyJournal(case.session.store, case.session)

    async def bad_response(_kind, payload):
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-text-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": "1", "target": "<wrong>译文</wrong>"}],
                }
            )
        }

    class Completions:
        async def create(self, **kwargs):
            payload = json.loads(kwargs["messages"][1]["content"])
            response = await bad_response("translate", payload)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=response["raw"]), finish_reason="stop")],
                usage=None,
                id="bad",
                model=MODEL,
                system_fingerprint=None,
            )

    runtime = journal.runtime(model=Model(FakeOpenAIClient(Completions())))
    result = asyncio.run(
        run_workflow(case.prepared, case.batch, case.index, runtime, session=case.session, save=journal.save)
    )
    assert result.status == "needs_attention"
    restored = BodyJournal(case.session.store)
    request = restored.store.read_request(case.batch.manifest.request_id)
    attempt = request.attempts[-1]
    path = restored.store.root / "responses" / "translate" / request.request_id / f"{attempt.attempt_id}.json"
    saved = json.loads(state.read(path))
    saved["wire_hash"] = request.wire_hash
    state.write(path, json.dumps(saved).encode())
    with pytest.raises(IdentityMismatch, match="response identity"):
        restored.store.read_model_response("translate", request.request_id, attempt.attempt_id)


def test_tiny_request_keeps_legacy_wire_when_compact_rules_cost_more():
    payload = {
        "protocol": "epubox-text-1",
        "prompt_version": "epubox-members-1",
        "request_id": "tiny",
        "items": [{"item_id": "u-small", "source": "Hello.", "terms": [], "hints": {}, "constraints": {}}],
        "context": [],
    }
    assert (
        model_input_budget("translate", payload, compact=True)["cl100k_tokens"]
        > model_input_budget("translate", payload)["cl100k_tokens"]
    )

    class Completions:
        async def create(self, **kwargs):
            sent = json.loads(kwargs["messages"][1]["content"])
            assert sent == payload
            raw = json.dumps(
                {
                    "protocol": "epubox-text-1",
                    "request_id": "tiny",
                    "items": [{"item_id": "u-small", "target": "你好。"}],
                }
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=raw), finish_reason="stop")],
                usage=None,
                id="reply",
                model=MODEL,
                system_fingerprint=None,
            )

    runtime = ModelRuntime(
        model=Model(FakeOpenAIClient(Completions())),
        model_max_output_tokens=1000,
        provider_output_token_field="max_tokens",
        input_budget_version=2,
    )
    response = asyncio.run(
        runtime.invoke("translate", payload, {"request_id": "tiny", "item_ids": ["u-small"], "output_tokens": 100})
    )
    assert "wire_version" not in response["metadata"]
    assert json.loads(response["raw"])["items"][0]["item_id"] == "u-small"


def test_wire_mode_cannot_be_added_after_a_legacy_attempt_is_reserved(tmp_path):
    case = make_case(tmp_path)
    store = case.session.store
    store.write_request(case.batch.manifest)
    store.reserve_attempt(
        case.batch.manifest.request_id,
        Attempt(attempt_id="legacy", affected_items=case.batch.manifest.item_ids, created_at="now"),
    )
    with pytest.raises(IdentityMismatch, match="before dispatch"):
        store.finish_attempt(
            case.batch.manifest.request_id,
            "legacy",
            state="sent",
            metadata={"wire_version": wire.VERSION, "wire_hash": "0" * 64},
        )


def test_custom_transport_cannot_enable_a_different_wire_contract():
    async def transport(_kind, _payload):
        raise AssertionError("must not dispatch")

    with pytest.raises(TypeError, match="compact"):
        ModelRuntime(transport=transport, compact=True)  # type: ignore[call-arg]


def test_code_hint_content_is_never_sent_even_for_a_tiny_request():
    payload = {
        "protocol": "epubox-text-1",
        "prompt_version": "epubox-members-1",
        "request_id": "code",
        "items": [
            {
                "item_id": "u-code",
                "source": "⟦=x1⟧",
                "terms": [],
                "hints": {"x1": {"element": "code", "class": "code", "readonly": "ZX", "excerpt": "ZX"}},
                "constraints": {
                    "x1": {
                        "kind": "x",
                        "parent": "root",
                        "movement": "fixed",
                        "reorder_allowed": False,
                        "fixed_order": ["x1"],
                    }
                },
            }
        ],
    }
    assert (
        model_input_budget("translate", payload, compact=True)["cl100k_tokens"]
        > model_input_budget("translate", payload)["cl100k_tokens"]
    )

    class Completions:
        async def create(self, **kwargs):
            message = kwargs["messages"][1]["content"]
            assert "ZX" not in message
            item = json.loads(message)["items"][0]
            assert item["item_id"] == "1" and item["source"] == "<x1/>"
            raw = json.dumps(
                {"protocol": "epubox-text-1", "request_id": "code", "items": [{"item_id": "1", "target": {}}]}
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=raw), finish_reason="stop")],
                usage=None,
                id="reply",
                model=MODEL,
                system_fingerprint=None,
            )

    runtime = ModelRuntime(
        model=Model(FakeOpenAIClient(Completions())),
        model_max_output_tokens=1000,
        provider_output_token_field="max_tokens",
        input_budget_version=2,
    )
    result = asyncio.run(
        runtime.invoke("translate", payload, {"request_id": "code", "item_ids": ["u-code"], "output_tokens": 100})
    )
    assert result["metadata"]["wire_version"] == wire.VERSION
    assert json.loads(result["raw"])["items"][0]["target"] == "⟦=x1⟧"
