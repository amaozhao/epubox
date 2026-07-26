from agno.models.openai.like import OpenAILike

from ..core.config import settings
from .streaming_openai_like import StreamingOpenAILike


def build_primary_model():
    return OpenAILike(
        id=settings.AGNES_MODEL,
        api_key=settings.AGNES_API_KEY,
        base_url=settings.AGNES_BASE_URL,
        provider="Agnes",
    )


model = build_primary_model()


def build_fallback_model():
    return StreamingOpenAILike(
        id=settings.CR_PROXY_MODEL,
        api_key=settings.CR_PROXY_API_KEY,
        base_url=settings.CR_PROXY_BASE_URL,
        max_completion_tokens=4096,
    )


fallback_model = build_fallback_model()
