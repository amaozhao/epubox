from pathlib import Path

import pytest
from typer.testing import CliRunner

import main
from engine.cli import RunOutcome
from engine.services.atomic import StoreLocked


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


@pytest.mark.parametrize("status", ("needs_attention", "paused", "failed", "exception"))
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
        assert result.output.count("request timed out") == 1
    else:
        assert result.output.count("状态：") == 1


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
    import json
    from types import SimpleNamespace
    from zipfile import ZipFile

    from engine import cli
    from engine.services.journal import BodyJournal
    from tests.engine.epub.factory import make_epub
    from tests.engine.epub.preparation import StubChecker
    from tests.engine.execution.atomic import answer

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
            data = json.loads(response["raw"])
            for item in data["items"]:
                item["checks"]["accuracy"] = "not_applicable"
            response["raw"] = json.dumps(data)
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
