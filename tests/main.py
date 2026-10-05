from pathlib import Path

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
    assert "输出：" in result.output


def test_translate_cli_explains_active_same_book_run(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"fixture")

    def locked(*_args, **_kwargs):
        raise StoreLocked("run store is locked")

    monkeypatch.setattr(main, "translate_book", locked)
    result = CliRunner().invoke(main.app, ["translate", str(source)])

    assert result.exit_code == 1
    assert "已有翻译进程在运行" in result.output


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
    assert "one item needs repair" in result.output


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
