from pydantic import BaseModel, Field


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


class TranslationResponse(BaseModel):
    """
    Defines the expected output structure for the translator agent.
    It ensures the agent returns only the translated string.
    """

    translation: str = Field(..., description="The translated Chinese text.")
