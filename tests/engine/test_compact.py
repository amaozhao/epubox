import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
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
from tests.engine.epub.test_factory import make_epub
from tests.engine.epub.test_preparation import StubChecker
from tests.engine.execution.test_atomic import answer


def test_plain_command_completes_and_resumes_with_source_and_five_shards(tmp_path, monkeypatch):
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
    assert {path.name for path in root.iterdir()} == {
        "source",
        "origin.json",
        "mapping.json",
        "plan.json",
        "terms.json",
        "state.json",
    }
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
    body = (
        "<table>"
        + "".join(f'<tr><td>Entry {number}, <a href="#p{number}">{number}</a></td></tr>' for number in range(125))
        + "</table>"
    )
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
            "target_language": "zh-Hans",
            "max_source_tokens": 2000,
            "minimum_source_tokens": 500,
            "max_input_tokens": 50000,
            "max_output_tokens": 4096,
            "context_tokens": 32768,
            "prompt_version": "epubox-members-1",
            "planner_version": "epubox-member-planner-2",
            "input_budget_version": 2,
            "output_budget_version": 6,
            "run_http_limit": 0,
            "concurrency": 2,
            "rpm": cli.settings.AGNES_TEXT_RPM,
        },
    )
    root = source.with_suffix("")
    prepared = prepare_book(source, root, config, StubChecker())
    # Reproduce the historical checkpoint from before table-row splitting was supported.
    with monkeypatch.context() as historical:
        historical.setattr("engine.services.tables.split_table", lambda *_args: ())
        old = prepare_preflight(
            RunStore(prepared.work_dir),
            limits_from_config(prepared.preparation.translation_config),
            cli.settings.AGNES_MODEL,
        )
    assert not old.passed
    upgraded = replace(
        config,
        extraction_config=dict(prepared.preparation.extraction_config),
        translation_config=dict(prepared.preparation.translation_config)
        | {"max_output_tokens": 8192, "output_budget_version": 6},
    )
    assert cli._replan_local(prepared.work_dir, upgraded)
    state.write(prepared.work_dir / "glossary" / "plan.json", b"{}")
    assert not cli._replan_local(prepared.work_dir, upgraded)
    state.unlink(prepared.work_dir / "glossary" / "plan.json")
    cli._write_source_hint(prepared.work_dir, source, prepared.preparation.source_hash, "old")
    remember(source, prepared.work_dir)
    before = {
        path.relative_to(prepared.work_dir): path.read_bytes()
        for path in prepared.work_dir.rglob("*")
        if path.is_file()
    }
    capped = replace(
        upgraded,
        translation_config=dict(upgraded.translation_config) | {"max_output_tokens": 4096},
    )
    assert not cli._replan_local(prepared.work_dir, capped)
    glossary = tmp_path / "terms.json"
    glossary.write_text('{"A":"甲"}')
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: pytest.fail("conflict built a model"))
    conflicts: tuple[tuple[dict[str, Any], frozenset[str]], ...] = (
        ({"provider": "proxy"}, frozenset({"provider"})),
        ({"glossary": glossary}, frozenset({"glossary"})),
    )
    for options, explicit in conflicts:
        with pytest.raises(ValueError, match="different frozen configuration|conflicts"):
            cli.translate_book(source, explicit_options=explicit, **options)
        assert {
            path.relative_to(prepared.work_dir): path.read_bytes()
            for path in prepared.work_dir.rglob("*")
            if path.is_file()
        } == before
    monkeypatch.setattr(cli, "build_run_model", lambda *args, **kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *args: StubChecker())
    reached = []

    async def local(work_root, output, checker, **kwargs):
        from engine.services.preflight import receipt_path

        saved = RunStore(work_root).read_preparation()
        original = state.read(work_root / "checks" / "preflight.json")
        report = prepare_preflight(
            RunStore(work_root),
            limits_from_config(saved.translation_config, output_unlimited=True),
            cli.settings.AGNES_MODEL,
        )
        assert report.passed and saved.translation_config["output_budget_version"] == 6
        assert saved.translation_config["max_output_tokens"] == 4096
        assert receipt_path(work_root).name == "output.json"
        assert state.read(work_root / "checks" / "preflight.json") == original
        reached.append(True)
        return cli._record(cli.RunOutcome("paused", work_root, "preflight"))

    monkeypatch.setattr(cli, "_advance_work_dir", local)
    result = cli.translate_book(source)
    assert result.status == "paused" and reached == [True]
    assert result.http_attempts == 0
    assert {path.name for path in root.iterdir()} == {
        "source",
        "origin.json",
        "mapping.json",
        "plan.json",
        "terms.json",
        "state.json",
    }


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


def test_completed_older_code_policy_resumes_with_its_original_checkpoint(tmp_path, monkeypatch):
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": '<h1 id="heading">Safe</h1><p>Keep data.</p>'})
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
    with monkeypatch.context() as previous:
        previous.setattr(cli, "ATOMIC_EXTRACTOR_VERSION", "epubox-atomic-6")
        first = cli.translate_book(source, auto_extract=False)
    assert first.status == "completed"
    root = source.with_suffix("")
    assert calls
    before = {
        path: state.read(path)
        for folder in ("documents", "results", "requests")
        for path in state.glob(root / folder, "*.json")
    }
    assert before and any(b'"policy":"code-context-1"' in data for data in before.values())
    assert first.output_path is not None
    output_before = first.output_path.read_bytes()
    monkeypatch.setattr(cli, "build_run_model", lambda *args, **kwargs: pytest.fail("completed book created a model"))

    second = cli.translate_book(source)

    assert second.status == "completed"
    assert second.http_attempts == first.http_attempts
    assert first.output_path.read_bytes() == output_before
    assert {path: state.read(path) for path in before} == before


def test_completed_book_republishes_when_code_protection_expands(tmp_path, monkeypatch):
    import tests.engine.agents.test_workflow as workflow_fixture
    from engine.epub import preservation
    from engine.epub.publish import publish_atomic, recover_atomic
    from tests.engine.epub.test_publish import _case

    monkeypatch.setattr(workflow_fixture, "EXTRACTOR_VERSION", "epubox-atomic-5")
    case = _case(
        tmp_path,
        '<pre>const original = 0;</pre><div class="programcode1"><div class="fixedline1">let value = 1;</div></div>'
        "<p>Explanation.</p>",
        ("let value = 1;", "Explanation."),
    )
    store = case.session.store
    targets = {
        item.item_id: item.source_projection.replace("let value = 1;", "let 数值 = 1;").replace(
            "Explanation.", "解释。"
        )
        for inventory in case.index.inventories
        for item in inventory.items
    }
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)
    output = tmp_path / "reader.epub"
    classifier = preservation.CodeContext
    with monkeypatch.context() as historical:
        historical.setattr(
            preservation, "CodeContext", lambda *args, **kwargs: classifier(*args, **kwargs, policy="code-context-2")
        )
        first = publish_atomic(store, output, StubChecker())
    verification = first["verification"]
    assert isinstance(verification, dict) and verification["code_stats"]["selected_regions"] == 1
    before = {path: state.read(path) for path in state.glob(store.root / "results", "*.json")}
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: StubChecker())
    monkeypatch.setattr(cli, "_record", lambda outcome: outcome)

    outcome = cli._completed_run_outcome(store.root, output)

    assert outcome is not None and outcome.status == "completed"
    result = recover_atomic(store, output, StubChecker())
    assert result is not None
    verification = result["verification"]
    assert isinstance(verification, dict) and verification["code_stats"]["selected_regions"] == 2
    with ZipFile(output) as archive:
        assert b"let value = 1;" in archive.read("OEBPS/chapter.xhtml")
    assert {path: state.read(path) for path in before} == before
