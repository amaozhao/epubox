from pathlib import Path
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from engine.cli_v23 import load_terms
from main import app


def test_legacy_none_and_exception_return_nonzero(tmp_path: Path):
    source = tmp_path / "input.epub"
    source.write_bytes(b"mock input")
    for result in (AsyncMock(return_value=None), AsyncMock(side_effect=RuntimeError("failed"))):
        with patch("main.Orchestrator.translate_epub", result):
            response = CliRunner().invoke(app, ["translate", str(source)])
        assert response.exit_code == 1
        assert "翻译完成！" not in response.output


def test_v23_unknown_capacity_fails_before_provider_or_source_read(tmp_path: Path):
    source = tmp_path / "input.epub"
    source.write_bytes(b"not an epub")
    with patch("engine.cli_v23.model_for") as model:
        result = CliRunner().invoke(app, ["translate", str(source), "--engine", "v23"])
    assert result.exit_code == 1
    assert "--context-tokens" in result.output
    model.assert_not_called()


def test_legacy_glossary_preserves_user_terms_but_not_empty_candidates(tmp_path: Path):
    source = tmp_path / "terms.json"
    source.write_text('{"cache":"缓存","candidate":""}', encoding="utf-8")
    assert load_terms(source) == [
        {"source": "cache", "target": "缓存", "scope": "book", "mode": "required", "note": "user glossary"}
    ]
