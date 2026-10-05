import pytest
from pydantic import ValidationError

from engine.core.config import Settings, resolve_chunk_limit


def test_chunk_limit_defaults_and_explicit_value_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EPUB_CHUNK_MAX_TOKENS", raising=False)
    assert Settings(_env_file=None).EPUB_CHUNK_MAX_TOKENS == 2000  # type: ignore[call-arg]

    monkeypatch.setenv("EPUB_CHUNK_MAX_TOKENS", "5000")
    configured = Settings(_env_file=None).EPUB_CHUNK_MAX_TOKENS  # type: ignore[call-arg]

    assert configured == 5000
    assert resolve_chunk_limit(configured=configured) == 5000
    assert resolve_chunk_limit(2000, configured=configured) == 2000


def test_chunk_limit_rejects_non_positive_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPUB_CHUNK_MAX_TOKENS", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="positive integer"):
        resolve_chunk_limit(0)
