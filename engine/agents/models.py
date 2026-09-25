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
