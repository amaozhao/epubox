import os
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from engine.core.config import Settings, agnes_keys, resolve_chunk_limit


def test_chunk_limit_defaults_and_explicit_value_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EPUB_CHUNK_MAX_TOKENS", raising=False)
    assert Settings(_env_file=None).EPUB_CHUNK_MAX_TOKENS == 1500  # type: ignore[call-arg]

    monkeypatch.setenv("EPUB_CHUNK_MAX_TOKENS", "5000")
    configured = Settings(_env_file=None).EPUB_CHUNK_MAX_TOKENS  # type: ignore[call-arg]

    assert configured == 5000
    assert resolve_chunk_limit(configured=configured) == 1500
    assert resolve_chunk_limit(2000, configured=configured) == 1500
    assert resolve_chunk_limit(1000, configured=configured) == 1000


def test_chunk_limit_rejects_non_positive_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPUB_CHUNK_MAX_TOKENS", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="positive integer"):
        resolve_chunk_limit(0)


def test_agnes_keys_combines_overrides_and_deduplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name == "AGNES_API_KEY" or name.startswith("AGNES_API_KEY_"):
            monkeypatch.delenv(name)
    configured = SimpleNamespace(
        AGNES_API_KEY=" base ",
        model_extra={
            "AGNES_API_KEY_2": " duplicate ",
            "AGNES_API_KEY_1": "dotenv-one",
            "AGNES_API_KEY_3": "sk-",
        },
    )
    monkeypatch.setenv("AGNES_API_KEY_1", " process-one ")
    monkeypatch.setenv("AGNES_API_KEY_2", "base")
    monkeypatch.setenv("AGNES_API_KEY_4", "process-four")

    assert agnes_keys(configured) == ("base", "process-one", "process-four")


def test_agnes_keys_accepts_numbered_keys_without_base(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name == "AGNES_API_KEY" or name.startswith("AGNES_API_KEY_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("AGNES_API_KEY_2", "second")
    monkeypatch.setenv("AGNES_API_KEY_1", "first")

    configured = SimpleNamespace(AGNES_API_KEY="your-api-key-here", model_extra={})

    assert agnes_keys(configured) == (
        "first",
        "second",
    )
