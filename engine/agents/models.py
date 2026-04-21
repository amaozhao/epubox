# from agno.models.deepseek import DeepSeek

# from agno.models.google import Gemini
from agno.models.mistral import MistralChat

# from agno.models.openrouter import OpenRouter
# from agno.models.openai.like import OpenAILike
from ..core.config import settings
from .streaming_openai_like import StreamingOpenAILike


def build_primary_model():
    # if settings.MODEL_PROVIDER == "cr_proxy":
    #     return StreamingOpenAILike(
    #         id=settings.CR_PROXY_MODEL,
    #         api_key=settings.CR_PROXY_API_KEY,
    #         base_url=settings.CR_PROXY_BASE_URL,
    #         max_completion_tokens=4096,
    #     )

    # 主模型（直接使用 Mistral 替代失效的模型）
    # model = DeepSeek(base_url=settings.DEEPSEEK_BASE_URL, api_key=settings.DEEPSEEK_API_KEY)
    # model = OpenAILike(id=settings.GLM_MODEL, api_key=settings.GLM_API_KEY, base_url=settings.GLM_BASE_URL)
    # model = OpenRouter(id=settings.OPENROUTER_MODEL, api_key=settings.OPENROUTER_API_KEY)
    # model = Gemini(id=settings.GEMINI_MODEL, api_key=settings.GEMINI_API_KEY)

    # 直接使用 Mistral 模型
    return MistralChat(id=settings.MISTRAL_MODEL, api_key=settings.MISTRAL_API_KEY)


model = build_primary_model()


def build_fallback_model():
    return StreamingOpenAILike(
        id=settings.CR_PROXY_MODEL,
        api_key=settings.CR_PROXY_API_KEY,
        base_url=settings.CR_PROXY_BASE_URL,
        max_completion_tokens=4096,
    )


fallback_model = build_fallback_model()
