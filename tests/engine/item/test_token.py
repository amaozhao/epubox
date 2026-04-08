
from engine.item.token import (
    MAX_TOKEN_LIMIT,
    estimate_tokens,
)


class TestConstants:
    def test_max_token_limit(self):
        assert MAX_TOKEN_LIMIT == 1200


class TestEstimateTokens:
    def test_basic_string(self):
        result = estimate_tokens("Hello")
        assert result > 0

    def test_longer_string(self):
        result = estimate_tokens("Hello World")
        assert result >= estimate_tokens("Hello")

    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_repeated_text(self):
        assert estimate_tokens("word " * 100) > estimate_tokens("word " * 10)


