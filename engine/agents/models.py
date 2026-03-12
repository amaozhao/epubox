# from agno.models.deepseek import DeepSeek

from agno.models.google import Gemini
from agno.models.mistral import MistralChat

# from agno.models.openrouter import OpenRouter
# from agno.models.openai.like import OpenAILike
# from agno.models.nvidia import Nvidia
from ..core.config import settings

# 主模型（直接使用 Mistral 替代失效的模型）
# model = DeepSeek(base_url=settings.DEEPSEEK_BASE_URL, api_key=settings.DEEPSEEK_API_KEY)
# model = OpenAILike(id=settings.GLM_MODEL, api_key=settings.GLM_API_KEY, base_url=settings.GLM_BASE_URL)
# model = OpenAILike(id=settings.XFYUN_MIMIMAX_MODEL, api_key=settings.XFYUN_API_KEY, base_url=settings.XFYUN_BASE_URL)
# model = OpenAILike(id=settings.XFYUN_GLM_MODEL, api_key=settings.XFYUN_API_KEY, base_url=settings.XFYUN_BASE_URL)
# model = OpenAILike(id=settings.XFYUN_KIMI_MODEL, api_key=settings.XFYUN_API_KEY, base_url=settings.XFYUN_BASE_URL)
model = Gemini(id=settings.GEMINI_MODEL, api_key=settings.GEMINI_API_KEY)

# 直接使用 Mistral 模型
# model = MistralChat(id=settings.MISTRAL_MODEL, api_key=settings.MISTRAL_API_KEY)

# 备用模型（Mistral，用于内容安全审核失败时 fallback）
fallback_model = MistralChat(id=settings.MISTRAL_MODEL, api_key=settings.MISTRAL_API_KEY)
# model = OpenRouter(id=settings.OPENROUTER_MODEL, api_key=settings.OPENROUTER_API_KEY)
# model = Nvidia(id=settings.NVIDIA_MODEL, api_key=settings.NVIDIA_API_KEY)
