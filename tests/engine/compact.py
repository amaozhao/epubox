import asyncio
import hashlib
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from engine import cli
from engine.epub.preparation import PreparationConfig, prepare_book
from engine.item.atoms import ADAPTER_VERSION, EXTRACTOR_VERSION
from engine.schemas.contracts import canonical_hash, canonical_json_bytes
from engine.services import state
from engine.services.journal import BodyJournal
from engine.services.preflight import prepare_preflight
from engine.services.preparation import prepare_translation
from engine.services.ready import ReadySession, limits_from_config
from engine.services.resume import plan_resume
from engine.services.session import remember
from engine.services.store import RunStore
from engine.services.terms.planning import ATOMIC_TERM_PLANNER_VERSION
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker
from tests.engine.execution.atomic import answer


def test_plain_command_completes_and_resumes_with_only_source_and_one_json(tmp_path, monkeypatch):
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": '<h1 id="heading">Safe</h1><p>Keep data.</p>'})
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(cli, "build_run_model", lambda *args, **kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *args: StubChecker())
    monkeypatch.setattr(cli.settings, "AGNES_TEXT_RPM", 1000)
    runtime = BodyJournal.runtime
    calls = []

    async def transport(stage, request):
        calls.append(stage)
        return answer(stage, request)

    monkeypatch.setattr(
        BodyJournal,
        "runtime",
        lambda self, model=None, **kwargs: runtime(
            self, model=model, transport=transport, progress=kwargs.get("progress")
        ),
    )
    first = cli.translate_book(source, auto_extract=False)
    root = source.with_suffix("")
    assert first.status == "completed"
    assert first.work_dir == root and first.report_path == root / "state.json"
    assert {path.name for path in root.iterdir()} == {"source", "state.json"}
    assert not (root / "source.epub").exists()
    assert calls and set(calls) == {"translate", "review"}
    assert first.output_path is not None
    with ZipFile(first.output_path) as archive:
        assert archive.testzip() is None
        assert "译文" in archive.read("OEBPS/chapter.xhtml").decode()
    before = tuple(calls)
    monkeypatch.setattr(cli, "build_run_model", lambda *args, **kwargs: pytest.fail("completed book created a model"))
    second = cli.translate_book(source)
    assert second.status == "completed" and second.http_attempts == first.http_attempts
    assert tuple(calls) == before
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    assert state.is_file(root / "prepared.json")


def test_plain_command_translates_titles_and_derives_navigation_without_translation_frames(tmp_path, monkeypatch):
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<h1>Chapter 1</h1><p>Keep data.</p>"})
    monkeypatch.setattr(cli, "build_run_model", lambda *args, **kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *args: StubChecker())
    monkeypatch.setattr(cli.settings, "AGNES_TEXT_RPM", 1000)
    runtime = BodyJournal.runtime

    async def transport(stage, request):
        return answer(stage, request)

    monkeypatch.setattr(
        BodyJournal,
        "runtime",
        lambda self, model=None, **kwargs: runtime(
            self, model=model, transport=transport, progress=kwargs.get("progress")
        ),
    )
    result = cli.translate_book(source, auto_extract=False)
    assert result.status == "completed"
    assert result.output_path is not None
    journal = BodyJournal(RunStore(result.work_dir))
    assert journal.session.prepared.plan.derived_sources
    preview = plan_resume(result.work_dir)
    assert preview.phase == "publication" and preview.status == "ready"
    assert journal.progress_snapshot()["accepted_units"] == journal.session.prepared.plan.required_unit_count
    with ZipFile(result.output_path) as archive:
        assert "译文" in archive.read("OEBPS/nav.xhtml").decode()
    attempts = result.http_attempts
    again = cli.translate_book(source)
    assert again.status == "completed" and again.http_attempts == attempts
    derived = next(iter(journal.session.prepared.plan.derived_sources))
    item_id = journal.session.prepared.plan.unit_members[derived][0]
    path = result.work_dir / "results" / f"{item_id}.json"
    original_record = journal.records((item_id,))[item_id]
    altered = original_record.model_copy(update={"checks": {"source_unit_id": "unknown"}})
    state.write(path, canonical_json_bytes(altered))
    with pytest.raises(ValueError, match="canonical dependency"):
        BodyJournal(RunStore(result.work_dir))
    assert plan_resume(result.work_dir).status == "needs_attention"
    state.write(path, canonical_json_bytes(original_record))
    fake_target = original_record.model_copy(
        update={"target_projection": "假译文", "target_hash": canonical_hash("假译文")}
    )
    state.write(path, canonical_json_bytes(fake_target))
    with pytest.raises(ValueError, match="canonical dependency"):
        BodyJournal(RunStore(result.work_dir))


def test_plain_command_rebuilds_only_unsent_blocked_local_budget_plan(tmp_path, monkeypatch):
    body = "<table>" + "<tr><td><p>A</p></td><td><p>B</p></td></tr>" * 25 + "</table>"
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": body})
    config = PreparationConfig(
        run_id="old",
        auto_extract=False,
        adapter_version=ADAPTER_VERSION,
        extractor_version=EXTRACTOR_VERSION,
        extraction_config={
            "strategy": ATOMIC_TERM_PLANNER_VERSION,
            "provider": "agnes",
            "model": cli.settings.AGNES_MODEL,
        },
        translation_config={
            "provider": "agnes",
            "model": cli.settings.AGNES_MODEL,
            "max_source_tokens": 2000,
            "max_output_tokens": 4096,
            "context_tokens": 32768,
            "output_budget_version": 3,
        },
    )
    root = source.with_suffix("")
    prepared = prepare_book(source, root, config, StubChecker())
    old = prepare_preflight(
        RunStore(prepared.work_dir),
        limits_from_config(prepared.preparation.translation_config),
        cli.settings.AGNES_MODEL,
    )
    assert not old.passed
    cli._write_source_hint(prepared.work_dir, source, prepared.preparation.source_hash, "old")
    remember(source, prepared.work_dir)
    monkeypatch.setattr(cli, "build_run_model", lambda *args, **kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *args: StubChecker())
    reached = []

    async def local(actual_source, output, work_root, updated, checker, **kwargs):
        assert updated.auto_extract is False
        new = prepare_book(actual_source, work_root, updated, checker)
        report = prepare_preflight(
            RunStore(work_root), limits_from_config(new.preparation.translation_config), cli.settings.AGNES_MODEL
        )
        assert report.passed and updated.translation_config["output_budget_version"] == 4
        reached.append(True)
        return cli._record(cli.RunOutcome("paused", work_root, "preflight"))

    monkeypatch.setattr(cli, "_advance_source", local)
    result = cli.translate_book(source)
    assert result.status == "paused" and reached == [True]
    assert result.http_attempts == 0
    assert {path.name for path in root.iterdir()} == {"source", "state.json"}


def test_interrupted_after_ready_derived_navigation_is_readonly_resumable(tmp_path):
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<h1>Chapter 1</h1><p>Keep data.</p>"})
    root = source.with_suffix("")
    state.initialize(root, source, hashlib.sha256(source.read_bytes()).hexdigest(), "ready")
    asyncio.run(
        prepare_translation(
            source,
            root,
            PreparationConfig(
                run_id="ready",
                auto_extract=False,
                adapter_version=ADAPTER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                extraction_config={"strategy": ATOMIC_TERM_PLANNER_VERSION, "model": cli.settings.AGNES_MODEL},
                translation_config={"model": cli.settings.AGNES_MODEL, "output_budget_version": 4},
            ),
            StubChecker(),
        )
    )
    assert ReadySession(RunStore(root)).prepared.plan.derived_sources
    before = (root / "state.json").read_bytes()
    plan = plan_resume(root)
    assert plan.phase == "translation" and plan.status == "ready" and plan.actions == ("translate",)
    assert (root / "state.json").read_bytes() == before
