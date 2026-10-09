import pytest

from engine.agents.runtime import ModelRuntime, RequestError
from engine.item.budget import request_source_tokens
from tests.engine.agents.runtime import context, payload


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["translate", "review"])
@pytest.mark.parametrize("size", [1500, 1501])
async def test_dispatch_source_ceiling_ignores_context_and_markers(stage, size):
    request = payload(stage)
    request["items"] = [{"item_id": "i1", "source": "word"}]
    request["items"][0]["source"] = "\u27e6+g1\u27e7" + " ".join(["word"] * size) + "\u27e6-g1\u27e7"
    request["context"] = ["context " * 1000]
    calls = []

    async def transport(kind, body):
        calls.append(body)
        return {"raw": "{}"}

    runtime = ModelRuntime(transport=transport)
    assert request_source_tokens(request, "gpt-3.5-turbo") == size
    if size > 1500:
        with pytest.raises(RequestError, match="source budget 1501 exceeds hard limit 1500"):
            await runtime.invoke(stage, request, context())
        assert calls == []
    else:
        await runtime.invoke(stage, request, context())
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_saved_oversized_response_is_replayed_without_new_http():
    request = payload("translate")
    request["items"] = [{"item_id": "i1", "source": "word"}]
    request["items"][0]["source"] = " ".join(["word"] * 2000)

    async def forbidden(*_args):
        raise AssertionError("historical replay must not send a new request")

    runtime = ModelRuntime(transport=forbidden, replay_response=lambda *_args: {"raw": "saved"})
    assert await runtime.invoke("translate", request, context()) == {"raw": "saved"}


@pytest.mark.asyncio
async def test_dispatch_keeps_a_lower_saved_limit():
    request = payload("translate")
    request["items"] = [{"item_id": "i1", "source": " ".join(["word"] * 1001)}]

    async def forbidden(*_args):
        raise AssertionError("the lower limit must be checked before sending")

    runtime = ModelRuntime(transport=forbidden)
    with pytest.raises(RequestError, match="hard limit 1000"):
        await runtime.invoke("translate", request, context(source_hard_limit=1000))
