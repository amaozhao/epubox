from __future__ import annotations

import asyncio

import pytest

from engine.execution import atomic
from engine.services.atomic import IdentityMismatch
from engine.services.journal import BodyJournal
from tests.engine.agents.test_workflow import prepare_case
from tests.engine.execution.test_atomic import answer


@pytest.mark.parametrize("interrupted", (False, True))
def test_atomic_closes_its_runtime_before_the_event_loop_returns(tmp_path, monkeypatch, interrupted) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    closed: list[bool] = []
    original = BodyJournal.runtime

    def runtime(self, **options):
        instance = original(self, **options)
        close = instance.aclose

        async def finish():
            await close()
            closed.append(asyncio.get_running_loop().is_running())

        monkeypatch.setattr(instance, "aclose", finish)
        return instance

    async def transport(stage, payload):
        return answer(stage, payload)

    async def fail(*_args, **_kwargs):
        raise IdentityMismatch("saved review frame fixture failure")

    monkeypatch.setattr(BodyJournal, "runtime", runtime)
    if interrupted:
        monkeypatch.setattr(atomic, "run_workflow", fail)
    result = asyncio.run(atomic.run_atomic(case.session.store.root, transport=transport, ready_session=case.session))

    assert result.status == ("paused" if interrupted else "translated")
    assert closed == [True]


def test_atomic_closes_runtime_when_initial_progress_callback_fails(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    closed: list[bool] = []

    async def finish():
        closed.append(asyncio.get_running_loop().is_running())

    instance = SimpleNamespace(key_count=1, snapshot={"enabled_keys": 1}, workflow_capacity=1, aclose=finish)
    monkeypatch.setattr(BodyJournal, "runtime", lambda *_args, **_kwargs: instance)

    def progress(event):
        if "个去重后的 key" in event.get("notice", ""):
            raise RuntimeError("progress fixture failure")

    with pytest.raises(RuntimeError, match="progress fixture failure"):
        asyncio.run(atomic.run_atomic(case.session.store.root, ready_session=case.session, progress=progress))
    assert closed == [True]
