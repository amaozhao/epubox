import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import typer
from click import Group
from typer.testing import CliRunner

import main as main_module
from engine import cli
from engine.epub import publish as atomic_publish
from engine.epub.preparation import PreparationConfig, prepare_book
from engine.execution.state import TranslationRunResult
from engine.item.atoms import ADAPTER_VERSION, EXTRACTOR_VERSION
from engine.services import journal as journal_module
from engine.services.preparation import prepare_translation
from engine.services.session import remember
from engine.services.terms.planning import ATOMIC_TERM_PLANNER_VERSION
from main import _progress_printer, app
from tests.engine.agents.workflow import prepare_case
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker
from tests.engine.services.store import _prepare


def test_source_diagnostics_and_inherited_output_errors_are_visible(capsys, tmp_path):
    message = "原书 EPUBCheck：1 个 ERROR；已记录为原书问题，继续翻译。"
    _progress_printer()({"phase": "source_check", "notice": message})
    assert message in capsys.readouterr().out
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"publication_verification": {"baseline": {"inherited_errors": 1}}}))
    main_module._print_result(
        cli.RunOutcome("completed", tmp_path, "publication", output_path=tmp_path / "book-cn.epub", report_path=report)
    )
    text = capsys.readouterr().out
    assert "保留原书 1 个 EPUBCheck 问题" in text
    assert "未新增问题" in text
    assert "EPUBCheck 仍未完全通过" in text


def test_legacy_completed_baseline_recovery_uses_explicit_checker_without_model(tmp_path, monkeypatch):
    from engine.epub import verification as verification_module

    work = tmp_path / "work"
    work.mkdir()
    source = make_epub(work / "source.epub")
    (work / "publish.json").write_text("{}")
    output = tmp_path / "book-cn.epub"
    plan = SimpleNamespace(
        unit_ids=("unit",), source_hash=hashlib.sha256(source.read_bytes()).hexdigest(), required_unit_count=1
    )
    store = SimpleNamespace(read_bookplan=lambda: plan, read_unit=lambda unit: SimpleNamespace(revision=1))
    monkeypatch.setattr(cli, "RunStore", lambda root: store)
    monkeypatch.setattr(cli, "canonical_hash", lambda value: "plan")
    evidence = {"epubcheck": {"passed": False}, "baseline": {"inherited_errors": 1}}
    monkeypatch.setattr(
        cli,
        "recover_publication",
        lambda *args, **kwargs: {"target_path": str(output), "target_hash": "output", "verification": evidence},
    )
    calls = []

    def verify(snapshot, target, proof, checker):
        assert snapshot == source and target == output and proof is evidence
        assert checker.command == ("explicit-checker",)
        calls.append(True)

    monkeypatch.setattr(verification_module, "verify_baseline", verify)
    monkeypatch.setattr(cli, "build_run_model", lambda *args, **kwargs: pytest.fail("model constructed"))
    outcome = cli.RunOutcome("completed", work, "publication", output_path=output)
    monkeypatch.setattr(cli, "_completed_outcome", lambda *args: outcome)
    assert cli._completed_run_outcome(work, output, "explicit-checker") is outcome
    assert calls == [True]


def active_case(tmp_path: Path, **translation):
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Original.</p>"})
    config = {
        "provider": "cr_proxy",
        "model": cli.settings.CR_PROXY_MODEL,
        "context_tokens": 8192,
        "max_input_tokens": 9000,
        "max_output_tokens": 2048,
        "max_source_tokens": 3000,
        "run_http_limit": 17,
        "concurrency": 1,
        "output_budget_version": 3,
    } | translation
    result = asyncio.run(
        prepare_translation(
            source,
            source.with_suffix(""),
            PreparationConfig(
                auto_extract=False,
                adapter_version=ADAPTER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                translation_config=config,
            ),
            StubChecker(),
        )
    )
    remember(source, result.work_dir)
    return source, result


def test_translate_help_separates_source_input_and_output_limits() -> None:
    result = CliRunner().invoke(app, ["translate", "--help"], terminal_width=200)
    command = cast(Group, typer.main.get_command(app)).commands["translate"]
    options = {option for parameter in command.params for option in parameter.opts}

    assert result.exit_code == 0
    assert {"--limit", "--max-input-tokens", "--max-output-tokens"}.issubset(options)


def test_translate_command_passes_only_commandline_configuration_sources(tmp_path, monkeypatch) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"fixture")
    seen = []

    def translate(*_args, **kwargs):
        seen.append(kwargs["explicit_options"])
        return cli.RunOutcome("completed", tmp_path / "work", "publication", output_path=tmp_path / "book-cn.epub")

    monkeypatch.setattr(main_module, "translate_book", translate)
    runner = CliRunner()
    assert runner.invoke(app, ["translate", str(source)]).exit_code == 0
    assert (
        runner.invoke(
            app,
            ["translate", str(source), "--provider", "agnes", "--http-limit", "0", "--no-auto-extract"],
        ).exit_code
        == 0
    )

    assert seen == [frozenset(), frozenset({"provider", "http_limit", "auto_extract"})]


def test_translate_freezes_atomic_versions_and_explicit_chunk_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    captured = {}
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: StubChecker())

    async def advance(actual_source, _output, work_root, config, checker, **_kwargs):
        captured["config"] = config
        prepared = prepare_book(actual_source, work_root, config, checker)
        return cli.RunOutcome("paused", prepared.work_dir, "terms")

    monkeypatch.setattr(cli, "_advance_source", advance)
    result = cli.translate_book(source, work_root=tmp_path / "work", limit=5000, max_input_tokens=24000)
    config = captured["config"]

    assert result.status == "paused"
    assert config.extraction_config["strategy"] == ATOMIC_TERM_PLANNER_VERSION
    assert config.translation_config["prompt_version"] == "epubox-members-1"
    assert config.translation_config["input_budget_version"] == 2
    assert config.translation_config["output_budget_version"] == 3
    assert config.translation_config["max_source_tokens"] == 5000
    assert config.translation_config["max_input_tokens"] == 24000
    assert config.translation_config["rpm"] == cli.settings.AGNES_TEXT_RPM


def test_implicit_environment_limit_change_reuses_run_but_explicit_change_refuses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    work_root = tmp_path / "work"
    checker = StubChecker()
    run_ids: list[str | None] = []
    resumed: list[Path] = []
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: checker)

    async def advance(actual_source, _output, actual_work_root, config, actual_checker, **_kwargs):
        run_ids.append(config.run_id)
        prepared = prepare_book(actual_source, actual_work_root, config, actual_checker)
        return cli.RunOutcome("paused", prepared.work_dir, "terms")

    monkeypatch.setattr(cli, "_advance_source", advance)

    async def resume(work_dir, *_args, **_kwargs):
        resumed.append(work_dir)
        return cli.RunOutcome("paused", work_dir, "terms")

    monkeypatch.setattr(cli, "_advance_work_dir", resume)
    monkeypatch.setattr(cli.settings, "EPUB_CHUNK_MAX_TOKENS", 2000)
    first = cli.translate_book(source, work_root=work_root)
    monkeypatch.setattr(cli.settings, "EPUB_CHUNK_MAX_TOKENS", 5000)
    second = cli.translate_book(source, work_root=work_root)

    assert second.work_dir == first.work_dir
    assert run_ids == [first.work_dir.name]
    assert resumed == [first.work_dir]
    with pytest.raises(ValueError, match="different frozen configuration"):
        cli.translate_book(source, work_root=work_root, limit=5000)


def test_startup_progress_is_emitted_before_source_hashing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"fixture")
    reports: list[dict] = []
    original = cli._sha256_file

    def source_hash(path: Path) -> str:
        assert reports and reports[0]["phase"] == "startup"
        return original(path)

    monkeypatch.setattr(cli, "_sha256_file", source_hash)
    monkeypatch.setattr(cli, "_existing_run_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: object())

    async def advance(*_args, **_kwargs):
        source_hash_value = hashlib.sha256(source.read_bytes()).hexdigest()
        return cli.RunOutcome("paused", tmp_path / "work" / source_hash_value / "run", "preflight")

    monkeypatch.setattr(cli, "_advance_source", advance)
    cli.translate_book(source, work_root=tmp_path / "work", progress=reports.append)

    assert reports[0] == {
        "phase": "startup",
        "execution_state": "running",
        "planned": 0,
        "succeeded": 0,
        "failed": 0,
        "pending": 0,
        "http_attempts": 0,
    }


def test_atomic_batch_progress_keeps_estimates_reserves_and_actual_usage_distinct(capsys) -> None:
    printer = _progress_printer()
    report = {
        "phase": "review",
        "execution_state": "running",
        "request_id": "tx-1",
        "batch_status": "completed",
        "decision": "replace",
        "revised": True,
        "batch_issues": ["术语已修正"],
        "required_items": 12,
        "translated_items": 8,
        "reviewed_items": 7,
        "accepted_units": 7,
        "needs_attention_units": 1,
        "http_attempts": 4,
        "source_tokens": 1200,
        "estimated_input_tokens": 1800,
        "reserved_input_tokens": 2956,
        "reserved_output_tokens": 4096,
        "input_tokens": 1333,
        "output_tokens": 777,
        "actual_input_tokens": 333,
        "actual_output_tokens": 77,
        "elapsed_seconds": 2.5,
    }

    printer(report)
    output = capsys.readouterr().out

    assert "批次=tx-1" in output and "结果=修订，修订=是" in output and "耗时=2.5秒" in output
    assert "输入估算=1800 tokens" in output and "输入预留=2956 tokens" in output
    assert "输出预留=4096 tokens" in output and "实际累计输出=777 tokens" in output
    assert "本次实际输入=333 tokens" in output and "本次实际输出=77 tokens" in output
    assert "原因=术语已修正" in output

    printer(report | {"request_id": "tx-2", "decision": "no_change", "revised": False, "batch_issues": []})
    assert "结果=通过，修订=否" in capsys.readouterr().out


def test_finish_routes_atomic_ready_to_atomic_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    output = tmp_path / "book-cn.epub"

    async def translated(*_args, **_kwargs):
        return TranslationRunResult("translated", 1, 0, 0, 2, 2)

    def publish(store, target, _checker, *, overwrite):
        assert store.root == case.session.store.root
        assert target == output and not overwrite
        return {"sha256": "f" * 64}

    monkeypatch.setattr(cli, "run_translation", translated)
    monkeypatch.setattr(atomic_publish, "publish_atomic", publish)
    result = asyncio.run(cli._finish("ready", "ready", case.session.store.root, output, object(), object(), False))

    assert result.status == "completed"
    assert result.required_units == case.prepared.plan.required_unit_count
    assert result.output_sha256 == "f" * 64


def test_atomic_resume_uses_frozen_body_model_and_nondefault_output_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir = tmp_path / ("a" * 64) / "run"
    work_dir.mkdir(parents=True)
    (work_dir / "source.epub").write_bytes(b"fixture")
    preparation = SimpleNamespace(
        run_id="run",
        source_hash="a" * 64,
        source_path="source.epub",
        extraction_config={
            "strategy": ATOMIC_TERM_PLANNER_VERSION,
            "provider": "agnes",
            "model": "term-model",
            "max_output_tokens": 1024,
        },
        translation_config={"provider": "cr_proxy", "model": "body-model", "max_output_tokens": 8192},
    )
    built = []
    automatic = []
    monkeypatch.setattr(cli, "RunStore", lambda *_args: SimpleNamespace(read_preparation=lambda: preparation))
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: object())

    def build(provider, model, *, max_output_tokens):
        built.append((provider, model, max_output_tokens))
        return SimpleNamespace(id=model)

    async def resume(actual_work_dir, *_args, **_kwargs):
        automatic.append(_kwargs["automatic"])
        return cli.RunOutcome("paused", actual_work_dir, "translation")

    monkeypatch.setattr(cli, "build_run_model", build)
    monkeypatch.setattr(cli, "_advance_work_dir", resume)
    result = cli.resume_book(work_dir, output=tmp_path / "book-cn.epub")

    assert result.status == "paused"
    assert built == [("cr_proxy", "body-model", 8192)]
    assert automatic == [False]


def test_completed_resume_recovers_before_output_check_or_model_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir = tmp_path / ("a" * 64) / "run"
    work_dir.mkdir(parents=True)
    output = tmp_path / "book-cn.epub"
    output.write_bytes(b"published")
    preparation = SimpleNamespace(run_id="run", source_hash="a" * 64)
    completed = cli.RunOutcome("completed", work_dir, "publication", output_path=output)
    monkeypatch.setattr(cli, "RunStore", lambda *_args: SimpleNamespace(read_preparation=lambda: preparation))
    monkeypatch.setattr(cli, "_completed_run_outcome", lambda actual, target, epubcheck=None: completed)
    monkeypatch.setattr(
        cli,
        "build_run_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("completed resume needs no model")),
    )
    monkeypatch.setattr(
        cli,
        "check_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("completed output must be recovered first")),
    )

    assert cli.resume_book(work_dir, output=output) is completed


def test_atomic_resume_rejects_output_alias_before_model_or_body_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    output = tmp_path / "original.epub"
    output.write_bytes(b"modified original")

    def reject(*_args):
        raise ValueError("output aliases the recorded original EPUB")

    monkeypatch.setattr(atomic_publish, "validate_atomic_output", reject)
    monkeypatch.setattr(
        cli,
        "build_run_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("invalid output must fail before model")),
    )
    monkeypatch.setattr(
        cli,
        "_advance_work_dir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("invalid output must fail before body")),
    )

    with pytest.raises(ValueError, match="recorded original"):
        cli.resume_book(case.session.store.root, output=output, overwrite=True)


def test_source_hint_is_bound_to_original_path_inode_source_and_run(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    case = prepare_case(tmp_path / "case", "<p>First.</p>", ("First.",))
    snapshot = case.session.store.root / "source.epub"

    cli._write_source_hint(
        case.session.store.root, snapshot, case.prepared.plan.source_hash, case.prepared.plan.run_id
    )
    saved = cli.strict_json_loads((case.session.store.root / "source.json").read_bytes())

    assert saved == {
        "format": "epubox-source-1",
        "run_id": case.prepared.plan.run_id,
        "source_hash": case.prepared.plan.source_hash,
        "original_path": str(snapshot.resolve()),
        "st_dev": snapshot.stat().st_dev,
        "st_ino": snapshot.stat().st_ino,
    }
    with pytest.raises(cli.IdentityMismatch, match="identity changed"):
        cli._write_source_hint(
            case.session.store.root, source, case.prepared.plan.source_hash, case.prepared.plan.run_id
        )


def test_source_hint_rejects_a_symbolic_link_record(tmp_path: Path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    snapshot = case.session.store.root / "source.epub"
    payload = tmp_path / "outside.json"
    payload.write_text("{}")
    (case.session.store.root / "source.json").symlink_to(payload)

    with pytest.raises(cli.IdentityMismatch, match="symbolic link"):
        cli._write_source_hint(
            case.session.store.root, snapshot, case.prepared.plan.source_hash, case.prepared.plan.run_id
        )


def test_blocked_preflight_returns_attention_without_starting_body_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _prepare(tmp_path)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("blocked preflight must not enter body execution")

    monkeypatch.setattr(cli, "run_translation", forbidden)
    result = asyncio.run(
        cli._finish(
            "needs_attention",
            "preflight",
            store.root,
            tmp_path / "book-cn.epub",
            object(),
            object(),
            False,
            reason="atom-1: source_tokens",
        )
    )

    assert result.status == "needs_attention" and result.phase == "preflight"
    assert result.reason == "atom-1: source_tokens"
    report = cli.strict_json_loads((store.root / "report.json").read_bytes())
    assert isinstance(report, dict)
    assert report["reason"] == "atom-1: source_tokens"


def test_atomic_retry_validates_then_adds_run_quota_then_reopens_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    unit_id = case.batch.items[0].unit_id
    events = []

    class Journal:
        def __init__(self, store):
            assert store.root == case.session.store.root

        def validate_retry_units(self, units):
            events.append(("validate", units))

        def retry_units(self, units):
            events.append(("retry", units))

    def add_budget(*_args, **kwargs):
        events.append(("budget", kwargs["add_run_http"]))

    monkeypatch.setattr(journal_module, "BodyJournal", Journal)
    monkeypatch.setattr(cli, "add_http_budget", add_budget)
    cli._authorize_resume_actions(
        case.session.store,
        retry_units=(unit_id,),
        add_unit_http=0,
        add_run_http=2,
        retry_checks=(),
        add_check_http=0,
        repair_file=None,
        authorization_id="atomic-review-retry",
    )

    assert events == [("validate", (unit_id,)), ("budget", 2), ("retry", (unit_id,))]


def test_v3_cli_pipeline_reserves_full_output_and_completed_resume_sends_nothing(tmp_path, monkeypatch):
    from engine.services.journal import BodyJournal
    from tests.engine.execution.atomic import answer

    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>AI services.</p>"})
    monkeypatch.setattr(cli, "build_run_model", lambda *args, **kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *args: StubChecker())
    monkeypatch.setattr(cli.settings, "AGNES_TEXT_RPM", 1000)
    original_runtime = BodyJournal.runtime
    calls = []
    events = []

    async def transport(stage, request):
        calls.append(request["request_id"])
        return answer(stage, request)

    def fake_runtime(self, model=None, transport_arg=None, *, progress=None, **kwargs):
        return original_runtime(self, model=model, transport=transport, progress=progress)

    monkeypatch.setattr(BodyJournal, "runtime", fake_runtime)
    result = cli.translate_book(source, auto_extract=False, progress=events.append)
    assert result.status == "completed"
    requests = [event for event in events if event.get("event") == "request"]
    assert requests and all(event["reserved_output_tokens"] == 4096 for event in requests)
    before = len(calls)
    repeated = cli.translate_book(source, auto_extract=False)
    assert repeated.status == "completed"
    assert len(calls) == before


def test_plain_translate_reopens_failed_review_and_skips_completed_translation(tmp_path, monkeypatch):
    from engine.services.journal import BodyJournal
    from tests.engine.execution.atomic import answer

    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Original.</p>"})
    monkeypatch.setattr(cli, "build_run_model", lambda *args, **kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *args: StubChecker())
    monkeypatch.setattr(cli.settings, "AGNES_TEXT_RPM", 1000)
    original_runtime = BodyJournal.runtime
    calls = []
    fail = True

    async def transport(stage, request):
        calls.append(stage)
        response = answer(stage, request)
        if stage == "review" and fail:
            import json

            data = json.loads(response["raw"])
            for item in data["items"]:
                item["checks"]["accuracy"] = "not_applicable"
            response["raw"] = json.dumps(data)
        return response

    monkeypatch.setattr(
        BodyJournal,
        "runtime",
        lambda self, model=None, **kwargs: original_runtime(
            self, model=model, transport=transport, progress=kwargs.get("progress")
        ),
    )
    first = cli.translate_book(source, auto_extract=False)
    assert first.status == "needs_attention"
    assert (source.with_suffix("") / "active.json").is_file()
    before = calls.count("translate")
    fail = False
    second = cli.translate_book(source)
    assert second.status == "completed"
    assert second.work_dir == first.work_dir
    assert calls.count("translate") == before
    before = len(calls)
    third = cli.translate_book(source)
    assert third.status == "completed" and len(calls) == before


def test_plain_translate_reuses_all_omitted_frozen_options(tmp_path, monkeypatch):
    source, prepared = active_case(tmp_path)
    calls = []

    def resume(work_dir, **kwargs):
        calls.append((work_dir, kwargs))
        return cli.RunOutcome("needs_attention", work_dir, "translation")

    monkeypatch.setattr(cli, "resume_book", resume)
    result = cli.translate_book(source)

    assert result.work_dir == prepared.work_dir
    assert calls[0][0] == prepared.work_dir and calls[0][1]["_automatic"] is True


def test_matching_explicit_option_resumes_without_automatic_retry(tmp_path, monkeypatch):
    source, prepared = active_case(tmp_path)
    calls = []

    def resume(work_dir, **kwargs):
        calls.append(kwargs["_automatic"])
        return cli.RunOutcome("needs_attention", work_dir, "translation")

    monkeypatch.setattr(cli, "resume_book", resume)
    result = cli.translate_book(source, provider="cr_proxy", explicit_options=frozenset({"provider"}))

    assert result.work_dir == prepared.work_dir
    assert calls == [False]


@pytest.mark.parametrize(
    ("kwargs", "explicit"),
    (
        ({"provider": "agnes"}, "provider"),
        ({"context_tokens": 32768}, "context_tokens"),
        ({"max_input_tokens": 50000}, "max_input_tokens"),
        ({"max_output_tokens": 4096}, "max_output_tokens"),
        ({"limit": 2000}, "limit"),
        ({"http_limit": 0}, "http_limit"),
        ({"concurrency": 2}, "concurrency"),
        ({"auto_extract": True}, "auto_extract"),
        ({"repair_terms": True}, "repair_terms"),
    ),
)
def test_active_session_rejects_each_explicit_conflicting_option_before_model(tmp_path, monkeypatch, kwargs, explicit):
    source, prepared = active_case(tmp_path)
    before = {
        path.relative_to(prepared.work_dir): path.read_bytes()
        for path in prepared.work_dir.rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(
        cli,
        "build_run_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("conflict must precede model creation")),
    )

    with pytest.raises(ValueError, match="active session conflicts|cannot replace"):
        cli.translate_book(source, explicit_options=frozenset({explicit}), **kwargs)

    after = {
        path.relative_to(prepared.work_dir): path.read_bytes()
        for path in prepared.work_dir.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_active_session_rejects_explicit_glossary_change(tmp_path):
    source, _ = active_case(tmp_path)
    glossary = tmp_path / "terms.json"
    glossary.write_text('{"Original":"原文"}')

    with pytest.raises(ValueError, match="explicit --glossary"):
        cli.translate_book(source, glossary=glossary, explicit_options=frozenset({"glossary"}))


def test_direct_api_infers_nondefault_provider_as_explicit(tmp_path):
    source, _ = active_case(
        tmp_path,
        provider="agnes",
        model=cli.settings.AGNES_MODEL,
        rpm=cli.settings.AGNES_TEXT_RPM,
    )

    with pytest.raises(ValueError, match="explicit --provider"):
        cli.translate_book(source, provider="cr_proxy")
