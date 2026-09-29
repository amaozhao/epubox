from unittest.mock import patch

from engine.core.tokens import count_tokens


def test_count_tokens_handles_empty_and_regular_text():
    assert count_tokens("") == 0
    assert count_tokens("hello world") > 0


def test_count_tokens_has_deterministic_offline_fallback():
    with patch("engine.core.tokens._get_tokenizer", return_value=None):
        assert count_tokens("hello, world") == 3
