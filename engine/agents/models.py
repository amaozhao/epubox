# from agno.models.deepseek import DeepSeek

# from agno.models.google import Gemini
from agno.models.mistral import MistralChat

# from agno.models.openai.like import OpenAILike
from ..core.config import settings

# 直接创建 DeepSeek 模型实例
# model = DeepSeek(base_url=settings.DEEPSEEK_BASE_URL, api_key=settings.DEEPSEEK_API_KEY)
# model = OpenAILike(id=settings.KIMI_MODEL, api_key=settings.KIMI_API_KEY, base_url=settings.KIMI_BASE_URL)
# model = OpenAILike(id=settings.GLM_MODEL, api_key=settings.GLM_API_KEY, base_url=settings.GLM_BASE_URL)
# model = Gemini(id=settings.GEMINI_MODEL, api_key=settings.GEMINI_API_KEY)
model = MistralChat(id=settings.MISTRAL_MODEL, api_key=settings.MISTRAL_API_KEY)
