from pathlib import Path

import pytest
from typer.testing import CliRunner

import main
from engine.cli import RunOutcome
from engine.epub.validation import EpubValidationError, ValidationIssue
from engine.item.views import SourceViewError
from engine.services.atomic import IdentityMismatch, StoreLocked


def test_translate_cli_uses_one_pipeline_and_defaults_to_auto_terms(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"fixture")
    called = []

    def fake_translate(path, **options):
        called.append((path, options))
        return RunOutcome("completed", tmp_path / "work", "publication", output_path=tmp_path / "out.epub")

    monkeypatch.setattr(main, "translate_book", fake_translate)
    result = CliRunner().invoke(main.app, ["translate", str(source)])

    assert result.exit_code == 0, result.output
    assert len(called) == 1
    assert called[0][1]["auto_extract"] is True
    assert called[0][1]["work_root"] is None
    assert "max_output_tokens" not in called[0][1]
    assert "输出：" in result.output


def test_translate_cli_explains_active_same_book_run(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"fixture")

    calls = []

    def locked(*_args, **_kwargs):
        calls.append(True)
        raise StoreLocked("run store is locked")

    monkeypatch.setattr(main, "translate_book", locked)
    result = CliRunner().invoke(main.app, ["translate", str(source)])

    assert result.exit_code == 1
    assert "已有翻译进程在运行" in result.output
    assert len(calls) == 1 and "自动续跑" not in result.output


def test_translate_cli_stops_on_checkpoint_identity_errors(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"fixture")
    calls = []

    def mismatched(*_args, **_kwargs):
        calls.append(True)
        raise IdentityMismatch("compact book session does not match the original source")

    monkeypatch.setattr(main, "translate_book", mismatched)
    result = CliRunner().invoke(main.app, ["translate", str(source)])

    assert result.exit_code == 1
    assert len(calls) == 1
    assert "断点身份校验失败" in result.output
    assert "compact book session does not match the original source" in result.output
    assert "自动续跑：" not in result.output
    assert "本轮翻译中断" not in result.output


def test_translate_cli_stops_on_source_projection_errors_without_retrying(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"fixture")
    calls = []

    def invalid_projection(*_args, **_kwargs):
        calls.append(True)
        raise SourceViewError("u-source: projection/source mismatch at s-source[3:4]")

    monkeypatch.setattr(main, "translate_book", invalid_projection)
    result = CliRunner().invoke(main.app, ["translate", str(source)])

    assert result.exit_code == 1
    assert len(calls) == 1
    assert "原文投影校验失败，已停止自动续跑" in result.output
    assert "projection/source mismatch at s-source[3:4]" in result.output
    assert "这是本地结构校验错误" in result.output
    assert "原书和现有断点均已保留" in result.output
    assert "自动续跑：" not in result.output
    assert "本轮翻译中断" not in result.output


@pytest.mark.parametrize(
    ("issues", "resources"),
    [
        (
            (ValidationIssue("scripted_content", "Script in reading-order document", resource="OPS/a.xhtml"),),
            ("OPS/a.xhtml",),
        ),
        (
            (
                ValidationIssue("scripted_content", "Script in reading-order document", resource="OPS/a.xhtml"),
                ValidationIssue("scripted_content", "Script in reading-order document", resource="OPS/a.xhtml"),
                ValidationIssue("scripted_content", "Script in reading-order document", resource="OPS/b.xhtml"),
            ),
            ("OPS/a.xhtml", "OPS/b.xhtml"),
        ),
    ],
)
def test_translate_cli_stops_on_unsupported_source_and_lists_each_resource_once(
    tmp_path: Path, monkeypatch, issues: tuple[ValidationIssue, ...], resources: tuple[str, ...]
) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"fixture")
    calls = []

    def unsupported(*_args, **_kwargs):
        calls.append(True)
        raise EpubValidationError("unsupported_source", issues[0].message, issues=issues)

    monkeypatch.setattr(main, "translate_book", unsupported)
    result = CliRunner().invoke(main.app, ["translate", str(source)])

    assert result.exit_code == 1
    assert len(calls) == 1
    assert "原书包含当前不支持的内容，已停止自动续跑" in result.output
    assert "原书和现有断点均已保留" in result.output
    assert "自动续跑：" not in result.output
    for resource in resources:
        assert result.output.count(resource) == 1


def test_translate_cli_retries_epubcheck_timeout_five_times(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"fixture")
    calls = []

    def timeout(*_args, **_kwargs):
        calls.append(True)
        raise EpubValidationError("epubcheck_timeout", "EPUBCheck timed out")

    monkeypatch.setattr(main, "translate_book", timeout)
    result = CliRunner().invoke(main.app, ["translate", str(source)])

    assert result.exit_code == 1
    assert len(calls) == 5
    assert result.output.count("自动续跑：") == 4
    assert result.output.count("EPUBCheck timed out") == 5


def test_translate_cli_preserves_checkpoint_when_source_is_replaced(tmp_path: Path, monkeypatch) -> None:
    import hashlib

    from engine import cli
    from engine.services import state
    from tests.engine.epub.test_factory import make_epub

    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Original.</p>"})
    root = source.with_suffix("")
    state.initialize(root, source, hashlib.sha256(source.read_bytes()).hexdigest(), "old-run")
    before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    make_epub(source, {"chapter.xhtml": "<p>Replacement.</p>"})
    replacement = source.read_bytes()
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: pytest.fail("must not build a model"))

    result = CliRunner().invoke(main.app, ["translate", str(source)])

    assert result.exit_code == 1
    assert result.output.count("startup:") == 1
    assert "断点身份校验失败" in result.output
    assert "hash" in result.output and str(root) in result.output
    assert "自动续跑：" not in result.output
    assert source.read_bytes() == replacement
    assert {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()} == before


def test_translate_cli_stops_on_preflight_attention_without_retrying(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"fixture")
    calls = []

    def blocked(*_args, **_kwargs):
        calls.append(True)
        return RunOutcome(
            "needs_attention",
            tmp_path / "work",
            "preflight",
            reason="不可拆分表格超过单次请求上限",
        )

    monkeypatch.setattr(main, "translate_book", blocked)
    result = CliRunner().invoke(main.app, ["translate", str(source)])

    assert result.exit_code == 1
    assert len(calls) == 1
    assert "不可拆分表格超过单次请求上限" in result.output
    assert "自动续跑：" not in result.output


@pytest.mark.parametrize("status", ("paused", "failed", "exception"))
def test_translate_cli_limits_unsuccessful_runs_to_five(tmp_path: Path, monkeypatch, status) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"fixture")
    calls = []

    def unsuccessful(path, **options):
        calls.append((path, options))
        if status == "exception":
            raise RuntimeError("request timed out")
        return RunOutcome(status, tmp_path / "work", "translation")

    monkeypatch.setattr(main, "translate_book", unsuccessful)
    result = CliRunner().invoke(main.app, ["translate", str(source)])

    assert result.exit_code == 1 and len(calls) == 5
    assert result.output.count("自动续跑：") == 4
    assert "第 6/5" not in result.output
    if status == "exception":
        assert result.output.count("request timed out") == 5
    else:
        assert result.output.count("状态：") == 1


def test_translate_cli_stops_when_translation_attention_makes_no_progress(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"fixture")
    calls = []

    def stuck(path, **options):
        calls.append((path, options))
        return RunOutcome(
            "needs_attention",
            tmp_path / "work",
            "translation",
            accepted_units=3066,
            required_units=3068,
            http_attempts=720,
            reason="translate:u-code: untranslated English remains: const index = pc.index('document-search');",
        )

    monkeypatch.setattr(main, "translate_book", stuck)
    result = CliRunner().invoke(main.app, ["translate", str(source)])

    assert result.exit_code == 1 and len(calls) == 2
    assert result.output.count("自动续跑：") == 1
    assert "没有新增已接受单元或 HTTP 请求" in result.output
    assert "再次启动命令可继续重试失败项" in result.output
    assert "重复失败：translate:u-code: untranslated English remains" in result.output


def test_each_translate_command_has_one_restart_grant_across_automatic_rounds(tmp_path, monkeypatch):
    from engine.services.retry import claim_restart

    source = tmp_path / "source.epub"
    source.write_bytes(b"fixture")
    claims = []

    def stuck(_path, **_options):
        claims.append(claim_restart(tmp_path / "work"))
        return RunOutcome("needs_attention", tmp_path / "work", "translation", reason="still failed")

    monkeypatch.setattr(main, "translate_book", stuck)
    for _ in range(2):
        CliRunner().invoke(main.app, ["translate", str(source)])
    assert claims == [True, False, True, False]


def test_plain_resume_command_grants_recovery_but_explicit_targets_do_not(tmp_path, monkeypatch):
    from engine import cli
    from engine.services.retry import claim_restart

    claims = []

    def probe(root):
        claims.extend((claim_restart(root), claim_restart(root)))
        raise RuntimeError("restart scope probe before loading checkpoint")

    monkeypatch.setattr(cli, "RunStore", probe)
    args = ["resume", str(tmp_path), "--output", str(tmp_path / "output.epub")]
    for _ in range(2):
        CliRunner().invoke(main.app, args)
    assert claims == [True, False, True, False]
    CliRunner().invoke(main.app, [*args, "--retry-unit", "selected-unit"])
    assert claims[-2:] == [False, False]


def test_translate_cli_keeps_retrying_translation_attention_with_new_http_progress(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"fixture")
    calls = []

    def progressing(path, **options):
        calls.append((path, options))
        return RunOutcome(
            "needs_attention",
            tmp_path / "work",
            "translation",
            accepted_units=3,
            required_units=5,
            http_attempts=len(calls),
        )

    monkeypatch.setattr(main, "translate_book", progressing)
    result = CliRunner().invoke(main.app, ["translate", str(source)])

    assert result.exit_code == 1 and len(calls) == 5
    assert result.output.count("自动续跑：") == 4


def test_translate_cli_retries_errors_then_resumes_validated_options_and_stops_on_success(tmp_path, monkeypatch):
    source = tmp_path / "source.epub"
    source.write_bytes(b"fixture")
    calls = []

    def recover(path, **options):
        calls.append((path, options))
        if len(calls) == 1:
            raise RuntimeError("temporary failure")
        status = "needs_attention" if len(calls) == 2 else "completed"
        return RunOutcome(status, tmp_path / "work", "publication", output_path=tmp_path / "out.epub")

    monkeypatch.setattr(main, "translate_book", recover)
    result = CliRunner().invoke(main.app, ["translate", str(source), "--concurrency", "3"])

    assert result.exit_code == 0 and len(calls) == 3
    assert [options["explicit_options"] for _, options in calls] == [
        frozenset({"concurrency"}),
        frozenset({"concurrency"}),
        frozenset(),
    ]
    assert all(path == source and options["concurrency"] == 3 for path, options in calls)
    assert result.output.count("状态：") == 1 and "输出：" in result.output
    assert "本轮翻译中断：temporary failure" in result.output


def test_translate_cli_does_not_restart_after_keyboard_interrupt(tmp_path, monkeypatch):
    source = tmp_path / "source.epub"
    source.write_bytes(b"fixture")
    calls = []

    def interrupted(*_args, **_kwargs):
        calls.append(True)
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "translate_book", interrupted)
    result = CliRunner().invoke(main.app, ["translate", str(source)])
    assert result.exit_code != 0 and len(calls) == 1
    assert "自动续跑" not in result.output


def test_translate_cli_resumes_real_checkpoint_and_publishes_without_retranslation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from zipfile import ZipFile

    from engine import cli
    from engine.services.journal import BodyJournal
    from tests.engine.epub.test_factory import make_epub
    from tests.engine.epub.test_preparation import StubChecker
    from tests.engine.execution.test_atomic import answer

    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    original_runtime = BodyJournal.runtime
    runs: list[RunOutcome] = []
    round_number = 0
    translated = []

    async def transport(stage, payload):
        response = answer(stage, payload)
        if stage == "translate":
            translated.extend(item["item_id"] for item in payload["items"])
        elif stage == "review" and round_number == 1:
            raise TimeoutError("temporary provider failure; content retry budget is unaffected")
        return response

    def run(path, **options):
        nonlocal round_number
        round_number += 1
        result = cli.translate_book(path, **options)
        runs.append(result)
        return result

    monkeypatch.setattr(main, "translate_book", run)
    monkeypatch.setattr(cli, "build_run_model", lambda *args, **kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *args: StubChecker())
    monkeypatch.setattr(cli.settings, "AGNES_TEXT_RPM", 1000)
    monkeypatch.setattr(
        BodyJournal,
        "runtime",
        lambda self, model=None, **kwargs: original_runtime(
            self,
            model=model,
            transport=transport,
            progress=kwargs.get("progress"),
        ),
    )
    result = CliRunner().invoke(main.app, ["translate", str(source), "--no-auto-extract"])

    assert result.exit_code == 0, result.output
    assert [run.status for run in runs] == ["needs_attention", "completed"]
    assert len(translated) == len(set(translated))
    assert runs[0].work_dir == runs[1].work_dir == source.with_suffix("")
    assert (source.with_suffix("") / "state.json").is_file()
    with ZipFile(source.with_name("book-cn.epub")) as archive:
        assert archive.testzip() is None
        assert "译文" in archive.read("OEBPS/chapter.xhtml").decode()


def test_resume_cli_reports_unfinished_run_as_nonzero(tmp_path: Path, monkeypatch) -> None:
    work_dir = tmp_path / "run"
    work_dir.mkdir()
    monkeypatch.setattr(
        main,
        "resume_book",
        lambda *_args, **_kwargs: RunOutcome(
            "needs_attention", work_dir, "translation", reason="one item needs repair"
        ),
    )
    result = CliRunner().invoke(main.app, ["resume", str(work_dir), "--output", str(tmp_path / "out.epub")])

    assert result.exit_code == 1
    assert "needs_attention" in result.output
    assert "one item needs repair" not in result.output


def test_resume_cli_forwards_explicit_retry_and_budget_targets(tmp_path: Path, monkeypatch) -> None:
    work_dir = tmp_path / "run"
    work_dir.mkdir()
    captured = {}

    def fake_resume(*_args, **options):
        captured.update(options)
        return RunOutcome("paused", work_dir, "translation")

    monkeypatch.setattr(main, "resume_book", fake_resume)
    result = CliRunner().invoke(
        main.app,
        [
            "resume",
            str(work_dir),
            "--output",
            str(tmp_path / "target.epub"),
            "--retry-unit",
            "u1",
            "--add-unit-http",
            "6",
            "--add-run-http",
            "6",
            "--retry-check",
            "d1",
            "--add-check-http",
            "3",
            "--authorization-id",
            "manual-1",
        ],
    )

    assert result.exit_code == 1
    assert captured["retry_units"] == ("u1",)
    assert captured["add_unit_http"] == captured["add_run_http"] == 6
    assert captured["retry_checks"] == ("d1",)
    assert captured["add_check_http"] == 3
    assert captured["authorization_id"] == "manual-1"


def test_plan_resume_cli_only_previews_old_format(tmp_path: Path) -> None:
    (tmp_path / "bookplan.json").write_text('{"format":"epubox-book-1"}')
    before = (tmp_path / "bookplan.json").read_bytes()

    result = CliRunner().invoke(main.app, ["plan-resume", str(tmp_path)])

    assert result.exit_code == 0
    assert "start_new_run" in result.output
    assert (tmp_path / "bookplan.json").read_bytes() == before
