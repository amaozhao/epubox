import tiktoken

MAX_TOKEN_LIMIT = 1200


def estimate_tokens(text: str) -> int:
    """Estimate token count for text using tiktoken."""
    if not text:
        return 0
    try:
        tokenizer = tiktoken.encoding_for_model("gpt-3.5-turbo")
    except KeyError:
        tokenizer = tiktoken.get_encoding("cl100k_base")
    return len(tokenizer.encode(text))
