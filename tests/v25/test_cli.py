from pathlib import Path
from types import SimpleNamespace

import pytest

from engine import cli


def test_translate_command_routes_only_through_preparation_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"fixture")
    captured = {}
    model = SimpleNamespace(id=cli.settings.AGNES_MODEL)
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: object())

    async def advance(actual_source, output, work_root, config, checker, *, model, overwrite, progress):
        captured.update(
            source=actual_source,
            output=output,
            work_root=work_root,
            config=config,
            model=model,
            overwrite=overwrite,
        )
        return cli.RunOutcome("paused", tmp_path / "work", "terms")

    monkeypatch.setattr(cli, "_advance_source", advance)
    result = cli.translate_book(source, work_root=tmp_path / "work", provider="agnes")

    assert result.status == "paused"
    assert captured["source"] == source.resolve()
    assert captured["config"].auto_extract is True
    assert captured["config"].extraction_config["model"] == model.id
    assert captured["config"].translation_config["target_language"] == "zh-Hans"


def test_resume_uses_frozen_model_and_snapshot_without_user_term_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir = tmp_path / "run"
    work_dir.mkdir()
    (work_dir / "source.epub").write_bytes(b"fixture")
    preparation = SimpleNamespace(
        source_path="source.epub",
        extraction_config={"provider": "agnes", "model": "frozen-model", "max_output_tokens": 128},
    )
    monkeypatch.setattr(cli, "StoreV25", lambda *_: SimpleNamespace(read_preparation=lambda: preparation))
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: SimpleNamespace(id="frozen-model"))
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: object())
    called = []

    async def advance(actual_work_dir, *_args, **_kwargs):
        called.append(actual_work_dir)
        return cli.RunOutcome("paused", actual_work_dir, "resolution")

    monkeypatch.setattr(cli, "_advance_work_dir", advance)
    result = cli.resume_book(work_dir, output=tmp_path / "target.epub")

    assert result.status == "paused"
    assert called == [work_dir.resolve()]


def test_outcome_report_is_derived_from_the_same_work_directory(tmp_path: Path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(cli, "StoreV25", lambda root: SimpleNamespace(root=root))

    def fake_report(store, **fields):
        calls.append((store.root, fields))
        return store.root / "report.json"

    monkeypatch.setattr(cli, "write_report", fake_report)
    result = cli._record(cli.RunOutcome("needs_attention", tmp_path, "translation"))

    assert result.report_path == tmp_path / "report.json"
    assert calls[0][0] == tmp_path
    assert calls[0][1]["status"] == "needs_attention"
