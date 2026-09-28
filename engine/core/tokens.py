import re
from functools import lru_cache
from typing import Any

import tiktoken


@lru_cache(maxsize=1)
def _get_tokenizer() -> Any | None:
    """Prefer tiktoken and fall back to a deterministic local estimate."""
    try:
        return tiktoken.encoding_for_model("gpt-3.5-turbo")
    except Exception:  # noqa: BLE001 - tokenizer lookup may fail for local cache or network reasons
        try:
            return tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001 - token counting must retain its offline fallback
            return None


def count_tokens(text: str) -> int:
    """Count tokens without requiring network access."""
    if not text:
        return 0
    tokenizer = _get_tokenizer()
    if tokenizer is None:
        return max(1, len(re.findall(r"\w+|[^\w\s]", text)))
    return len(tokenizer.encode(text))
