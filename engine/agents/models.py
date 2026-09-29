from agno.models.openai.like import OpenAILike

from ..core.config import settings
from .streaming_openai_like import StreamingOpenAILike


def build_primary_model(max_completion_tokens: int | None = None):
    return OpenAILike(
        id=settings.AGNES_MODEL,
        api_key=settings.AGNES_API_KEY,
        base_url=settings.AGNES_BASE_URL,
        provider="Agnes",
        max_completion_tokens=max_completion_tokens,
    )


model = build_primary_model()
proofreader_model = build_primary_model(max_completion_tokens=2048)


def build_fallback_model(max_completion_tokens: int = 4096):
    return StreamingOpenAILike(
        id=settings.CR_PROXY_MODEL,
        api_key=settings.CR_PROXY_API_KEY,
        base_url=settings.CR_PROXY_BASE_URL,
        max_completion_tokens=max_completion_tokens,
    )


fallback_model = build_fallback_model()
proofreader_fallback_model = build_fallback_model(max_completion_tokens=2048)


def build_run_model(provider: str, model_id: str, *, max_output_tokens: int):
    """Use the frozen provider/model identity for every stage of one run."""
    if provider == "agnes":
        key = settings.AGNES_API_KEY
        model = build_primary_model(max_completion_tokens=max_output_tokens)
    elif provider == "cr_proxy":
        key = settings.CR_PROXY_API_KEY
        model = build_fallback_model(max_completion_tokens=max_output_tokens)
    else:
        raise ValueError(f"unsupported model provider: {provider}")
    value = key.get_secret_value() if hasattr(key, "get_secret_value") else key
    if not value or value in {"sk-", "your-api-key-here"}:
        raise ValueError(f"{provider} API key is not configured")
    if model.id != model_id:
        raise ValueError("configured provider model differs from the frozen run identity")
    return model
