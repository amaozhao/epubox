from unittest.mock import patch

from engine.core.tokens import count_tokens, encode_text


def test_count_tokens_handles_empty_and_regular_text():
    assert count_tokens("") == 0
    assert count_tokens("hello world") > 0


def test_count_tokens_has_deterministic_offline_fallback():
    with patch("engine.core.tokens._get_tokenizer", return_value=None):
        assert count_tokens("hello, world") == 3


def test_count_tokens_accepts_text_that_looks_like_a_special_token():
    """EPUB sources may contain "<|endoftext|>" verbatim; counting must not raise."""
    text = "章节结束 <|endoftext|> 下一章"
    assert count_tokens(text) > 0


def test_encode_text_counts_special_token_markers_as_ordinary_text():
    class _Tokenizer:
        def encode(self, value, **kwargs):
            return list(value)

    tokenizer = _Tokenizer()
    assert encode_text(tokenizer, "a <|endoftext|> b") == list("a <|endoftext|> b")
