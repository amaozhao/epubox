from agno.agent import Agent
from pydantic import BaseModel, Field

from .models import model


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
