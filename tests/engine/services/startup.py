from __future__ import annotations

import asyncio

import pytest

import engine.item.members as members_module
import engine.services.ready as ready_module
from engine.execution.repair import run_translation
from engine.services.journal import BodyJournal
from engine.services.preparation import prepare_translation, resume_preparation
from engine.services.store import RunStore
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker
from tests.engine.services.preparing import atomic_config


class FirstProviderCall(RuntimeError):
    pass


def test_resume_reports_slow_checks_and_reuses_session_until_provider(tmp_path, monkeypatch) -> None:
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Translate this text.</p>"})
    prepared = asyncio.run(prepare_translation(source, tmp_path / "work", atomic_config(), StubChecker()))
    timeline: list[str] = []
    reads = 0
    read_ready = ready_module._read_ready
    require_preflight = ready_module.require_preflight
    pack_members = members_module.pack_members

    def counted_read(*args, **kwargs):
        nonlocal reads
        reads += 1
        return read_ready(*args, **kwargs)

    def counted_preflight(*args, **kwargs):
        timeline.append("preflight")
        return require_preflight(*args, **kwargs)

    def counted_pack(*args, **kwargs):
        timeline.append("pack")
        return pack_members(*args, **kwargs)

    def progress(event) -> None:
        notice = getattr(event, "notice", None) or (event.get("notice") if isinstance(event, dict) else None)
        if notice:
            timeline.append(notice)

    monkeypatch.setattr(ready_module, "_read_ready", counted_read)
    monkeypatch.setattr(ready_module, "require_preflight", counted_preflight)
    monkeypatch.setattr(members_module, "pack_members", counted_pack)
    resumed = asyncio.run(resume_preparation(prepared.work_dir, StubChecker(), progress=progress))

    assert resumed.ready_session is not None and reads == 1
    assert timeline.index("准备：验证原文快照和预检记录。") < timeline.index("preflight")
    budget_notice = next(item for item in timeline if item.startswith("准备：复核 "))
    assert timeline.index(budget_notice) < timeline.index("pack")

    async def first_provider(_stage, _payload):
        raise FirstProviderCall

    with pytest.raises(FirstProviderCall):
        asyncio.run(
            run_translation(
                prepared.work_dir,
                transport=first_provider,
                ready_session=resumed.ready_session,
                progress=progress,
            )
        )
    assert reads == 1


def test_fresh_prepare_and_body_journal_share_one_full_verification(tmp_path, monkeypatch) -> None:
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Translate this text.</p>"})
    verifications = 0
    verify = ready_module._verify

    def counted(*args, **kwargs):
        nonlocal verifications
        verifications += 1
        return verify(*args, **kwargs)

    monkeypatch.setattr(ready_module, "_verify", counted)
    prepared = asyncio.run(prepare_translation(source, tmp_path / "work", atomic_config(), StubChecker()))

    assert prepared.ready_session is not None and verifications == 1
    BodyJournal(RunStore(prepared.work_dir), prepared.ready_session)
    assert verifications == 1
