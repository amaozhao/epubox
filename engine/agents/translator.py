from agno.agent import Agent
from agno.models.mistral import MistralChat

# from agno.models.deepseek import DeepSeek
# from agno.models.openai.like import OpenAILike
# from agno.models.google import Gemini
from pydantic import BaseModel, Field

from ..core.config import settings

# 直接创建 DeepSeek 模型实例
# model = DeepSeek(
#     api_key=settings.DEEPSEEK_API_KEY,
# )
# model = OpenAILike(id=settings.KIMI_MODEL, api_key=settings.KIMI_API_KEY, base_url=settings.KIMI_BASE_URL)
# model = Gemini(id=settings.GEMINI_MODEL, api_key=settings.GEMINI_API_KEY)
model = MistralChat(id=settings.MISTRAL_MODEL, api_key=settings.MISTRAL_API_KEY)


class TranslationResponse(BaseModel):
    """
    Defines the expected output structure for the translator agent.
    It ensures the agent returns only the translated string.
    """

    translation: str = Field(..., description="The translated Chinese text.")


description = (
    "You are a professional translator. Your task is to translate English text into simplified Chinese."
    "You must pay close attention to preserving specific placeholders and XML tags."
)

instructions = [
    "1. Translate the user-provided 'text_to_translate' into natural, fluent, simplified Chinese.",
    "2. **Crucially, do not translate or modify the provided 'untranslatable_placeholders'.** Keep them exactly as they are.",
    "3. **Preserve all XML tags** (e.g., <p>, </p>, <br/>) in their original positions. Do not translate the content of the tags themselves.",
    "4. Your final output must be only the translated text, structured according to the response model. Do not add any greetings, explanations, or extraneous text.",
]


def get_translator():
    translator = Agent(
        name="Translator",
        role="翻译专家",
        model=model,
        markdown=False,
        description=description,
        instructions=instructions,
        response_model=TranslationResponse,
        use_json_mode=True,
        # reasoning=False,
        # debug_mode=True,
    )
    return translator
