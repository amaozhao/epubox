from __future__ import annotations

import json

import pytest

import engine.agents.runtime as runtime_module
from engine.agents.runtime import ModelRuntime, model_input_budget, request_messages


def _payload(request_id: str = "r1") -> dict:
    return {"protocol": "epubox-text-1", "request_id": request_id, "items": [], "text": "汉字"}


class _Tokenizer:
    name = "cl100k_base"

    def __init__(self, size: int = 20):
        self.size = size
        self.text = ""

    def encode(self, text: str) -> list[int]:
        self.text = text
        return list(range(self.size))


def test_v2_counts_complete_messages_in_tokens_instead_of_bytes(monkeypatch) -> None:
    tokenizer = _Tokenizer(20)
    monkeypatch.setattr(runtime_module, "_get_tokenizer", lambda: tokenizer)

    budget = model_input_budget("translate", _payload(), algorithm_version=2)

    assert json.loads(tokenizer.text) == {"messages": [*request_messages("translate", _payload())]}
    assert budget["cl100k_tokens"] == 20
    assert budget["estimated_input_tokens"] == 20 + 10 + 256
    assert budget["estimated_input_tokens"] != budget["rendered_utf8_bytes"] + 256


@pytest.mark.asyncio
async def test_v2_missing_tokenizer_fails_before_reservation_or_http(monkeypatch) -> None:
    monkeypatch.setattr(runtime_module, "_get_tokenizer", lambda: None)
    reserved = []
    calls = []

    async def transport(*args):
        calls.append(args)
        return {"raw": "{}"}

    runtime = ModelRuntime(
        transport=transport,
        reserve_attempt=lambda *args: reserved.append(args),
        model_max_output_tokens=10,
        input_budget_version=2,
    )

    with pytest.raises(RuntimeError, match="tokenizer unavailable"):
        await runtime.invoke("translate", _payload(), {"request_id": "r1", "output_tokens": 10})

    assert reserved == calls == []


@pytest.mark.asyncio
async def test_v2_reservation_records_token_algorithm_and_headroom(monkeypatch) -> None:
    monkeypatch.setattr(runtime_module, "_get_tokenizer", lambda: _Tokenizer(20))
    attempts = []

    async def transport(_kind, _payload):
        return {"raw": "{}", "usage": {"input_tokens": 8, "output_tokens": 2}}

    runtime = ModelRuntime(
        transport=transport,
        reserve_attempt=lambda _request_id, attempt: attempts.append(attempt),
        model_max_output_tokens=10,
        input_budget_version=2,
    )
    await runtime.invoke("translate", _payload(), {"request_id": "r1", "output_tokens": 10})

    reservation = attempts[0].reservation
    assert reservation["input_budget_algorithm_version"] == 2
    assert reservation["cl100k_input_tokens"] == 20
    assert reservation["input_wrapper_headroom_bytes"] == 0
    assert reservation["input_wrapper_headroom_tokens"] == 256
    assert reservation["estimated_input_tokens"] == 286


@pytest.mark.asyncio
async def test_default_runtime_keeps_v1_byte_budget(monkeypatch) -> None:
    monkeypatch.setattr(runtime_module, "_get_tokenizer", lambda: _Tokenizer(20))
    attempts = []

    async def transport(_kind, _payload):
        return {"raw": "{}"}

    runtime = ModelRuntime(
        transport=transport,
        reserve_attempt=lambda _request_id, attempt: attempts.append(attempt),
        model_max_output_tokens=10,
    )
    await runtime.invoke("translate", _payload(), {"request_id": "r1", "output_tokens": 10})

    direct = model_input_budget("translate", _payload())
    reservation = attempts[0].reservation
    assert direct["algorithm_version"] == reservation["input_budget_algorithm_version"] == 1
    assert reservation["input_wrapper_headroom_bytes"] == 256
    assert "input_wrapper_headroom_tokens" not in reservation
