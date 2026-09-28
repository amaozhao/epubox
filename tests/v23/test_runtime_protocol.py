import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from openai import APIStatusError

from engine.agents.protocol_v23 import (
    ProtocolError,
    strict_loads,
    validate_coherence_response,
    validate_review_response,
    validate_translation_response,
)
from engine.agents.runtime_v23 import (
    PROMPT_VERSION,
    ModelRuntime,
    ProviderError,
    RequestError,
    RuntimePaused,
    request_messages,
    wire_hash,
)
from engine.schemas.v23 import Attempt, RequestManifest
from engine.services.store import Store


def payload(kind: str, request_id: str = "r1") -> dict:
    protocols = {
        "translate": "epubox-text-1",
        "review": "epubox-review-1",
        "coherence": "epubox-coherence-1",
    }
    return {"protocol": protocols[kind], "request_id": request_id, "items": []}


def context(request_id: str = "r1", **values) -> dict:
    return {"request_id": request_id, "output_tokens": 10, **values}


class FakeOpenAIClient:
    def __init__(self, completions, *, max_retries: int = 2, options: list[int] | None = None):
        self.chat = SimpleNamespace(completions=completions)
        self.max_retries = max_retries
        self.options = [] if options is None else options

    def with_options(self, *, max_retries: int):
        self.options.append(max_retries)
        return FakeOpenAIClient(self.chat.completions, max_retries=max_retries, options=self.options)


def test_strict_json_rejects_duplicate_keys_nan_and_excessive_depth():
    with pytest.raises(ProtocolError, match="duplicate key"):
        strict_loads('{"protocol":"epubox-text-1","protocol":"other"}')
    with pytest.raises(ProtocolError, match="non-finite"):
        strict_loads('{"value":NaN}')
    with pytest.raises(ProtocolError, match="depth"):
        strict_loads("[" * 65 + "0" + "]" * 65)


def test_request_messages_are_budgetable_and_wire_hash_is_deterministic():
    first = payload("translate") | {"target_language": "zh-Hans"}
    second = {"items": [], "request_id": "r1", "target_language": "zh-Hans", "protocol": "epubox-text-1"}
    messages = request_messages("translate", first)
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "source_markup" not in json.dumps(messages)
    assert wire_hash("translate", first) == wire_hash("translate", second)
    assert wire_hash("translate", first, 10) != wire_hash("translate", first, 20)


def test_review_prompt_shows_no_change_without_target_and_preserves_literal_markup_text():
    system = request_messages("review", payload("review"))[0]["content"]
    no_change_example = system.split("no_change: ", 1)[1].split("\nreplace:", 1)[0]
    assert PROMPT_VERSION == "epubox-v23-4"
    assert '"decision":"no_change"' in no_change_example
    assert '"target"' not in no_change_example
    assert "omit the target key entirely; never return target:null" in system
    assert "literal examples such as <p> are ordinary text and must be preserved as text" in system


def test_translation_salvages_only_items_from_a_complete_valid_batch():
    raw = json.dumps(
        {
            "protocol": "epubox-text-1",
            "request_id": "r1",
            "items": [
                {"item_id": "a", "target": "甲"},
                {"item_id": "b"},
                {"item_id": "c", "target": "未知"},
            ],
        },
        ensure_ascii=False,
    )
    result = validate_translation_response(raw, "r1", {"a", "b"})

    assert result.accepted == {"a": {"item_id": "a", "target": "甲"}}
    assert result.errors == {"b": "target must be a non-empty string"}
    assert result.unknown == ("c",)
    assert result.missing == ()

    with pytest.raises(ProtocolError):
        validate_translation_response(raw[:-1], "r1", {"a", "b"})


def test_translation_duplicate_item_ids_invalidate_all_copies():
    raw = json.dumps(
        {
            "protocol": "epubox-text-1",
            "request_id": "r1",
            "items": [
                {"item_id": "a", "target": "甲"},
                {"item_id": "a", "target": "乙"},
                {"item_id": "b", "target": "丙"},
            ],
        },
        ensure_ascii=False,
    )
    result = validate_translation_response(raw, "r1", {"a", "b"})
    assert result.accepted == {"b": {"item_id": "b", "target": "丙"}}
    assert result.errors == {"a": "duplicate item_id"}


def test_review_enforces_all_checks_applicability_and_decision_target_rules():
    base = {
        "protocol": "epubox-review-1",
        "request_id": "r2",
        "items": [
            {
                "item_id": "a",
                "base_revision": 3,
                "decision": "no_change",
                "checks": {
                    "accuracy": "pass",
                    "fluency": "pass",
                    "terminology": "not_applicable",
                    "bindings": "pass",
                    "script": "pass",
                },
                "issues": [],
            }
        ],
    }
    expected = {"a": {"base_revision": 3, "terminology_applicable": False, "bindings_applicable": True}}
    assert validate_review_response(json.dumps(base), "r2", expected).errors == {}

    base["items"][0]["checks"]["bindings"] = "uncertain"
    assert validate_review_response(json.dumps(base), "r2", expected).errors["a"].startswith("blocking check")

    base["items"][0]["decision"] = "replace"
    base["items"][0]["target"] = "完整新目标"
    assert validate_review_response(json.dumps(base), "r2", expected).errors == {}

    base["items"][0]["decision"] = "needs_attention"
    assert "target is forbidden" in validate_review_response(json.dumps(base), "r2", expected).errors["a"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("base_revision", True, "base_revision mismatch"),
        ("decision", [], "invalid decision"),
        ("check", [], "invalid check value"),
        ("severity", [], "invalid issue severity"),
    ],
)
def test_review_non_string_enums_are_local_item_errors(field, value, message):
    item = {
        "item_id": "a",
        "base_revision": 1,
        "decision": "no_change",
        "checks": {
            "accuracy": "pass",
            "fluency": "pass",
            "terminology": "not_applicable",
            "bindings": "not_applicable",
            "script": "pass",
        },
        "issues": [],
    }
    if field == "check":
        item["checks"]["accuracy"] = value
    elif field == "severity":
        item["issues"] = [{"code": "bad", "severity": value, "message": "bad"}]
    else:
        item[field] = value
    raw = json.dumps({"protocol": "epubox-review-1", "request_id": "r", "items": [item]})
    result = validate_review_response(
        raw,
        "r",
        {"a": {"base_revision": 1, "terminology_applicable": False, "bindings_applicable": False}},
    )
    assert result.errors == {"a": message}


def test_review_unknown_item_fields_are_rejected_locally():
    item = {
        "item_id": "a",
        "base_revision": 1,
        "decision": "no_change",
        "checks": {
            "accuracy": "pass",
            "fluency": "pass",
            "terminology": "not_applicable",
            "bindings": "not_applicable",
            "script": "pass",
        },
        "issues": [],
        "confidence": 1,
    }
    raw = json.dumps({"protocol": "epubox-review-1", "request_id": "r", "items": [item]})
    result = validate_review_response(
        raw,
        "r",
        {"a": {"base_revision": 1, "terminology_applicable": False, "bindings_applicable": False}},
    )
    assert result.errors == {"a": "review item has missing or unknown fields"}


def test_review_no_change_rejects_target_null_instead_of_treating_it_as_omitted():
    item = {
        "item_id": "a",
        "base_revision": 1,
        "decision": "no_change",
        "checks": {
            "accuracy": "pass",
            "fluency": "pass",
            "terminology": "not_applicable",
            "bindings": "not_applicable",
            "script": "pass",
        },
        "issues": [],
        "target": None,
    }
    raw = json.dumps({"protocol": "epubox-review-1", "request_id": "r", "items": [item]})
    result = validate_review_response(
        raw,
        "r",
        {"a": {"base_revision": 1, "terminology_applicable": False, "bindings_applicable": False}},
    )
    assert result.errors == {"a": "target is forbidden for no_change"}


def test_coherence_cannot_rewrite_or_name_units_outside_its_manifest():
    response = {
        "protocol": "epubox-coherence-1",
        "request_id": "r3",
        "items": [{"item_id": "w1", "unit_ids": ["u1", "u9"], "issues": []}],
    }
    result = validate_coherence_response(json.dumps(response), "r3", {"w1": {"u1", "u2"}})
    assert result.errors == {"w1": "coherence unit_ids are outside the request manifest"}

    response["items"][0] = {"item_id": "w1", "unit_ids": ["u1"], "issues": [], "target": "改写"}
    result = validate_coherence_response(json.dumps(response), "r3", {"w1": {"u1", "u2"}})
    assert "exactly" in result.errors["w1"]


@pytest.mark.asyncio
async def test_runtime_reserves_before_each_http_and_retries_transport_twice():
    calls: list[int] = []
    reserved: list[Attempt] = []

    async def transport(kind, payload):
        calls.append(len(calls) + 1)
        if len(calls) < 3:
            raise OSError("network")
        return {"raw": "{}", "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}}

    async def reserve(_request_id, attempt):
        reserved.append(attempt)

    runtime = ModelRuntime(
        transport=transport,
        rpm=1000,
        max_inflight=2,
        reserve_attempt=reserve,
        sleep=lambda _: asyncio.sleep(0),
        model_max_output_tokens=100,
    )
    result = await runtime.invoke(
        "translate",
        {"protocol": "epubox-text-1", "request_id": "r1", "target_language": "zh-Hans", "items": []},
        context(estimated_tokens=10),
    )

    assert result["usage"]["total_tokens"] == 6
    assert len(calls) == len(reserved) == 3
    assert [attempt.reservation["attempt_number"] for attempt in reserved] == [1, 2, 3]


@pytest.mark.asyncio
async def test_runtime_does_not_call_provider_when_preflight_reservation_fails():
    called = False

    async def transport(kind, payload):
        nonlocal called
        called = True
        return {"raw": "{}"}

    async def reserve(_request_id, _attempt):
        raise RuntimeError("disk full")

    runtime = ModelRuntime(transport=transport, reserve_attempt=reserve, rpm=1000, model_max_output_tokens=100)
    with pytest.raises(RuntimeError, match="disk full"):
        await runtime.invoke("review", payload("review"), context())
    assert called is False


@pytest.mark.asyncio
async def test_runtime_records_each_real_http_attempt_in_the_run_store(tmp_path):
    store = Store(tmp_path)
    store.write_request(
        RequestManifest(
            request_id="r1",
            stage="translate",
            unit_ids=("u1",),
            item_ids=("i1",),
            plan_epochs={"u1": 0},
            revisions={"u1": 1},
            input_hashes={"u1": "input"},
            wire_hash="wire",
        )
    )

    async def transport(kind, request_payload):
        return {
            "raw": "{}",
            "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            "finish_reason": "stop",
            "metadata": {"response_id": "resp", "model": "model", "api_key": "secret"},
        }

    runtime = ModelRuntime(
        transport=transport,
        reserve_attempt=store.reserve_attempt,
        finish_attempt=store.finish_attempt,
        model_max_output_tokens=100,
    )
    await runtime.invoke("translate", payload("translate"), context(item_ids=["i1"]))

    attempt = store.read_request("r1").attempts[0]
    assert attempt.state == "succeeded"
    assert attempt.affected_items == ("i1",)
    assert attempt.usage is not None and attempt.usage.input_tokens == 2
    assert attempt.metadata == {"response_id": "resp", "model": "model", "finish_reason": "stop"}


@pytest.mark.asyncio
async def test_429_sets_shared_cooldown_for_other_request_kinds():
    now = {"value": 0.0}
    sleeps: list[float] = []
    calls = 0

    async def sleep(seconds):
        sleeps.append(seconds)
        now["value"] += seconds

    async def transport(kind, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError("limited", status_code=429, retry_after=7)
        return {"raw": "{}"}

    runtime = ModelRuntime(
        transport=transport,
        rpm=1000,
        sleep=sleep,
        monotonic=lambda: now["value"],
        model_max_output_tokens=100,
    )
    await runtime.invoke("translate", payload("translate"), context())
    await runtime.invoke("review", payload("review", "r2"), context("r2"))

    assert calls == 3
    assert 7 in sleeps


@pytest.mark.asyncio
async def test_repeated_shared_service_failures_pause_instead_of_burning_more_items():
    async def transport(kind, payload):
        raise ProviderError("limited", status_code=429, retry_after=0)

    runtime = ModelRuntime(
        transport=transport,
        rpm=1000,
        max_service_failures=3,
        sleep=lambda _: asyncio.sleep(0),
        model_max_output_tokens=100,
    )
    with pytest.raises(RuntimePaused):
        await runtime.invoke("translate", payload("translate"), context())


@pytest.mark.asyncio
async def test_repeated_connection_failures_pause_after_three_unknown_attempts(tmp_path):
    store = Store(tmp_path)
    store.write_request(
        RequestManifest(
            request_id="r1",
            stage="translate",
            unit_ids=("u1",),
            item_ids=("i1",),
            plan_epochs={"u1": 0},
            revisions={"u1": 1},
            input_hashes={"u1": "input"},
            wire_hash="wire",
        )
    )

    async def transport(kind, request_payload):
        raise OSError("offline")

    runtime = ModelRuntime(
        transport=transport,
        reserve_attempt=store.reserve_attempt,
        finish_attempt=store.finish_attempt,
        max_service_failures=3,
        model_max_output_tokens=100,
        sleep=lambda _: asyncio.sleep(0),
    )
    with pytest.raises(RuntimePaused):
        await runtime.invoke("translate", payload("translate"), context(item_ids=["i1"]))

    attempts = store.read_request("r1").attempts
    assert len(attempts) == 3
    assert all(attempt.state == "unknown" for attempt in attempts)
    assert all(attempt.usage is None and attempt.metadata == {} for attempt in attempts)


@pytest.mark.asyncio
async def test_cancellation_marks_a_reserved_attempt_unknown(tmp_path):
    store = Store(tmp_path)
    store.write_request(
        RequestManifest(
            request_id="r1",
            stage="translate",
            unit_ids=("u1",),
            item_ids=("i1",),
            plan_epochs={"u1": 0},
            revisions={"u1": 1},
            input_hashes={"u1": "input"},
            wire_hash="wire",
        )
    )
    started = asyncio.Event()

    async def transport(kind, request_payload):
        started.set()
        await asyncio.Event().wait()
        return {"raw": "{}"}

    runtime = ModelRuntime(
        transport=transport,
        reserve_attempt=store.reserve_attempt,
        finish_attempt=store.finish_attempt,
        model_max_output_tokens=100,
    )
    task = asyncio.create_task(runtime.invoke("translate", payload("translate"), context(item_ids=["i1"])))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    attempt = store.read_request("r1").attempts[0]
    assert attempt.state == "unknown"
    assert attempt.error == "cancelled with provider outcome unknown"


@pytest.mark.asyncio
async def test_request_larger_than_tpm_fails_before_reservation_or_http():
    called = False

    async def transport(kind, payload):
        nonlocal called
        called = True
        return {"raw": "{}"}

    runtime = ModelRuntime(transport=transport, tpm=10, model_max_output_tokens=100)
    with pytest.raises(RequestError, match="exceed TPM"):
        await runtime.invoke("translate", payload("translate"), context(estimated_tokens=11))
    assert called is False


@pytest.mark.asyncio
async def test_concurrent_output_caps_do_not_mutate_each_other_or_the_payload():
    seen: list[tuple[str, int | None]] = []
    gate = asyncio.Event()
    runtime: ModelRuntime

    async def transport(kind, request_payload):
        seen.append((request_payload["request_id"], runtime._output_cap.get()))
        if len(seen) == 2:
            gate.set()
        await gate.wait()
        assert "__epubox_output_tokens" not in request_payload
        return {"raw": "{}"}

    runtime = ModelRuntime(transport=transport, model_max_output_tokens=100)
    await asyncio.gather(
        runtime.invoke("translate", payload("translate", "r1"), context("r1", output_tokens=10)),
        runtime.invoke("translate", payload("translate", "r2"), context("r2", output_tokens=20)),
    )
    assert sorted(seen) == [("r1", 10), ("r2", 20)]


@pytest.mark.asyncio
async def test_default_provider_uses_per_request_model_copies_and_preserves_response_metadata():
    caps: list[int] = []
    gate = asyncio.Event()

    class Completions:
        async def create(self, **kwargs):
            caps.append(kwargs["max_completion_tokens"])
            if len(caps) == 2:
                gate.set()
            await gate.wait()
            request_id = kwargs["messages"][1]["content"]
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="{}"),
                        finish_reason="stop",
                    )
                ],
                usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
                id=request_id,
                model="fake-model",
                system_fingerprint="fp",
            )

    client = FakeOpenAIClient(Completions())

    class FakeModel:
        id = "fake-model"
        max_completion_tokens = 100
        max_retries = 2
        client = None

        def __init__(self, async_client):
            self.async_client = async_client

        def get_async_client(self):
            return self.async_client

        def _format_all_messages(self, messages, _compress):
            return [{"role": message.role, "content": message.content} for message in messages]

        def get_request_params(self, **_kwargs):
            return {"max_completion_tokens": self.max_completion_tokens}

    model = FakeModel(client)
    runtime = ModelRuntime(model=model, model_max_output_tokens=100)
    results = await asyncio.gather(
        runtime.invoke("translate", payload("translate", "r1"), context("r1", output_tokens=10)),
        runtime.invoke("translate", payload("translate", "r2"), context("r2", output_tokens=20)),
    )

    assert sorted(caps) == [10, 20]
    assert model.max_completion_tokens == 100
    assert results[0]["usage"] == {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}
    assert results[0]["finish_reason"] == "stop"
    assert results[0]["metadata"]["model"] == "fake-model"
    assert client.max_retries == 2
    assert client.options == [0, 0]


@pytest.mark.asyncio
async def test_agnes_provider_sends_max_tokens_and_clears_max_completion_tokens():
    requests: list[dict] = []

    class Completions:
        async def create(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason="stop")],
                usage={"prompt_tokens": 1, "completion_tokens": 1},
                id="response",
                model="agnes-3.0-flash",
                system_fingerprint=None,
            )

    client = FakeOpenAIClient(Completions())

    class FakeAgnesModel:
        id = "agnes-3.0-flash"
        provider = "Agnes"
        max_tokens = 65_536
        max_completion_tokens = 999
        max_retries = 2
        client = None

        def __init__(self, async_client):
            self.async_client = async_client

        def get_async_client(self):
            return self.async_client

        def _format_all_messages(self, messages, _compress):
            return [{"role": message.role, "content": message.content} for message in messages]

        def get_request_params(self, **_kwargs):
            return {
                key: value
                for key, value in {
                    "max_tokens": self.max_tokens,
                    "max_completion_tokens": self.max_completion_tokens,
                }.items()
                if value is not None
            }

    model = FakeAgnesModel(client)
    runtime = ModelRuntime(model=model, model_max_output_tokens=65_536)
    await runtime.invoke("translate", payload("translate"), context(output_tokens=321))

    assert requests[0]["max_tokens"] == 321
    assert "max_completion_tokens" not in requests[0]
    assert model.max_tokens == 65_536
    assert model.max_completion_tokens == 999
    assert client.max_retries == 2
    assert client.options == [0]


@pytest.mark.asyncio
async def test_provider_error_redacts_only_the_exact_configured_api_key(tmp_path):
    api_key = "exact-provider-secret"

    class Completions:
        async def create(self, **kwargs):
            response = httpx.Response(500, request=httpx.Request("POST", "https://provider.invalid/v1/chat"))
            raise APIStatusError(
                f"provider echoed {api_key}; book text sk-example remains",
                response=cast(Any, response),
                body=None,
            )

    client = FakeOpenAIClient(Completions())

    class FakeModel:
        id = "fake-model"
        provider = "test"
        max_completion_tokens = 100
        max_retries = 2
        client = None

        def __init__(self, async_client):
            self.async_client = async_client
            self.api_key = api_key

        def get_async_client(self):
            return self.async_client

        def _format_all_messages(self, messages, _compress):
            return [{"role": message.role, "content": message.content} for message in messages]

        def get_request_params(self, **_kwargs):
            return {"max_completion_tokens": self.max_completion_tokens}

    store = Store(tmp_path)
    store.write_request(
        RequestManifest(
            request_id="r1",
            stage="translate",
            unit_ids=("u1",),
            item_ids=("i1",),
            plan_epochs={"u1": 0},
            revisions={"u1": 1},
            input_hashes={"u1": "input"},
            wire_hash="wire",
        )
    )
    runtime = ModelRuntime(
        model=FakeModel(client),
        model_max_output_tokens=100,
        max_transport_retries=0,
        reserve_attempt=store.reserve_attempt,
        finish_attempt=store.finish_attempt,
    )

    with pytest.raises(RequestError):
        await runtime.invoke("translate", payload("translate"), context(item_ids=["i1"]))

    error = store.read_request("r1").attempts[0].error
    assert error is not None
    assert api_key not in error
    assert "[REDACTED]" in error
    assert "sk-example remains" in error
    assert client.max_retries == 2
    assert client.options == [0]


@pytest.mark.asyncio
async def test_partial_provider_usage_remains_unknown_instead_of_becoming_zero():
    async def transport(kind, request_payload):
        return {"raw": "{}", "usage": {"prompt_tokens": 4}}

    runtime = ModelRuntime(transport=transport, model_max_output_tokens=100)
    result = await runtime.invoke("translate", payload("translate"), context())
    assert result["usage"] is None


@pytest.mark.asyncio
async def test_agno_style_usage_attributes_are_preserved():
    async def transport(kind, request_payload):
        return {
            "raw": "{}",
            "usage": SimpleNamespace(input_tokens=7, output_tokens=3, total_tokens=10, cost=0.02),
        }

    runtime = ModelRuntime(transport=transport, model_max_output_tokens=100)
    result = await runtime.invoke("translate", payload("translate"), context())
    assert result["usage"] == {
        "input_tokens": 7,
        "output_tokens": 3,
        "total_tokens": 10,
        "known_cost": 0.02,
    }


def test_runtime_rejects_source_markup_before_provider_call():
    async def transport(kind, payload):
        return {"raw": "{}"}

    runtime = ModelRuntime(transport=transport)
    with pytest.raises(ValueError, match="source_markup"):
        asyncio.run(runtime.invoke("translate", {"source_markup": "<p>x</p>"}, {"request_id": "r1"}))
