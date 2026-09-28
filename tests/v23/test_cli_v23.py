from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from typer.testing import CliRunner

from engine.cli_v23 import load_terms
from engine.schemas.v23 import RunConfig
from main import app


def test_default_translate_uses_only_v23(tmp_path: Path):
    source = tmp_path / "input.epub"
    source.write_bytes(b"mock input")
    completed = SimpleNamespace(
        status="completed", work_dir=tmp_path, report_path=None, output_path=tmp_path / "out.epub"
    )
    with patch("engine.cli_v23.translate_v23", return_value=completed) as translate:
        response = CliRunner().invoke(app, ["translate", str(source)])
    assert response.exit_code == 0
    assert "翻译引擎: v23" in response.output
    assert translate.call_args.kwargs["context_tokens"] == 32768
    assert translate.call_args.kwargs["max_output_tokens"] == 4096
    help_output = CliRunner().invoke(app, ["translate", "--help"]).output
    assert "--engine" not in help_output
    assert "--limit" not in help_output
    assert "--preserve-fonts" not in help_output


def test_legacy_translate_entry_is_removed(tmp_path: Path):
    source = tmp_path / "input.epub"
    source.write_bytes(b"not an epub")
    with patch("engine.cli_v23.translate_v23") as translate:
        result = CliRunner().invoke(app, ["translate", str(source), "--engine", "legacy"])
    assert result.exit_code == 2
    assert "--engine" in result.output
    translate.assert_not_called()


def test_legacy_glossary_preserves_user_terms_but_not_empty_candidates(tmp_path: Path):
    source = tmp_path / "terms.json"
    source.write_text('{"cache":"缓存","candidate":""}', encoding="utf-8")
    assert load_terms(source) == [
        {"source": "cache", "target": "缓存", "scope": "book", "mode": "required", "note": "user glossary"}
    ]


def test_building_resume_rejects_changed_extractor_before_provider(tmp_path: Path):
    (tmp_path / "bookplan.json").write_text("{}")
    config = RunConfig(
        model="test", provider="test", prompt_version="test", extractor_version="old-extractor", run_http_limit=0
    )
    book = SimpleNamespace(frozen_config=config.model_dump(mode="json"), preparation_state="building")
    with (
        patch("engine.cli_v23.Store.read_bookplan", return_value=book),
        patch("engine.cli_v23.model_for") as model,
    ):
        result = CliRunner().invoke(app, ["resume", str(tmp_path), "--output", str(tmp_path / "out.epub")])
    assert result.exit_code == 1
    assert "extractor version changed" in result.output
    model.assert_not_called()


def test_old_checkpoint_is_rejected_without_creating_new_state(tmp_path: Path):
    (tmp_path / "old_checkpoint.json").write_text("{}")
    with patch("engine.cli_v23.model_for") as model:
        result = CliRunner().invoke(app, ["resume", str(tmp_path), "--output", str(tmp_path / "out.epub")])
    assert result.exit_code == 1
    assert "unsupported legacy checkpoint" in result.output
    assert sorted(path.name for path in tmp_path.iterdir()) == ["old_checkpoint.json"]
    model.assert_not_called()
