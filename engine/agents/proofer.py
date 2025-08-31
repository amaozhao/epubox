from agno.agent import Agent
from agno.models.mistral import MistralChat

# from agno.models.deepseek import DeepSeek
# from agno.models.openai.like import OpenAILike
# from agno.models.google import Gemini
from pydantic import BaseModel, Field

from ..core.config import settings

# 直接创建 DeepSeek 模型实例
# model = DeepSeek(api_key=settings.DEEPSEEK_API_KEY)
# model = OpenAILike(id=settings.KIMI_MODEL, api_key=settings.KIMI_API_KEY, base_url=settings.KIMI_BASE_URL)
# model = Gemini(id=settings.GEMINI_MODEL, api_key=settings.GEMINI_API_KEY)
model = MistralChat(id=settings.MISTRAL_MODEL, api_key=settings.MISTRAL_API_KEY)


class ProofreadingResult(BaseModel):
    """
    Defines the expected output for the proofer agent.
    It ensures the agent returns a dictionary of corrections, fulfilling requirement.
    """

    corrections: dict[str, str] = Field(
        ...,
        description="A dictionary mapping original phrases to their corrected versions. "
        "This will be empty if no corrections are needed.",
    )


description = (
    "You are an expert Chinese proofreader. Your job is to review a Chinese translation for correctness, grammar, and style."
    "You must identify areas for improvement and provide corrections, while leaving specified placeholders and XML tags untouched."
)
instructions = [
    "1. Review the provided 'text_to_proofread'. Identify any grammatical errors, typos, or awkward phrasing.",
    '2. If corrections are needed, return a JSON dictionary where keys are the original phrases and values are the corrected phrases (e.g., {"您我他": "你我他"}).',
    "3. If the text is perfect and requires no changes, return an empty dictionary: {}.",
    "4. **Do not modify the 'untranslatable_placeholders' or any XML tags.** They must remain as is.",
    "5. Your response must be a single, valid JSON object, as defined by the response model. Do not add any other text.",
]


def get_proofer():
    Proofer = Agent(
        name="Proofer",
        role="错词检查专家",
        model=model,
        # markdown=False,
        description=description,
        instructions=instructions,
        response_model=ProofreadingResult,
        use_json_mode=True,
        # reasoning=False,
        # debug_mode=True,
    )
    return Proofer
