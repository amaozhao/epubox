from agno.agent import Agent
from pydantic import BaseModel, Field

from .models import model


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
