from agno.agent import Agent
from agno.models.base import Model

from .models import model as default_model
from .schemas import ProofreadingResult

description = (
    "You are an expert Chinese proofreader specializing in technical and professional content. "
    "Your task is to improve Chinese translations for clarity, grammar, and style while maintaining technical accuracy."
)

instructions = [
    "1. **Task**: Proofread the 'text_to_proofread' (Simplified Chinese) for grammar, typos, and technical professionality.",
    "2. **Constraint - Minimal Intervention**: Only correct errors or significant awkwardness. If a phrase is technically correct and natural, do NOT change it just for the sake of variety.",
    "3. **Placeholder Integrity**: 'untranslatable_placeholders' (e.g., ##...##) and XML tags MUST be preserved EXACTLY. "
    "   - Do not translate, modify, or delete them."
    "   - Ensure they appear in the logically correct position within the corrected phrase.",
    "4. **Output Structure**: Your response must be ONLY a RAW JSON object. No markdown blocks, no preamble.",
    '   - Format: {"corrections": {"original_phrase": "improved_phrase"}}',
    '   - If no changes are needed, return: {"corrections": {}}',
    "5. **Language Rule**: Both keys and values must be in Chinese. Do not include English explanations or 'Note' fields.",
    "6. **Robustness & Escaping**: "
    "   - Ensure the JSON is valid and parseable by `json.loads()`."
    '   - ESCAPE all internal double quotes with a backslash (\\").'
    "   - Do not add line breaks or extra spaces outside the JSON object.",
    "7. **Pre-computation Check**: "
    "   - [ ] Is every 'original_phrase' (key) a literal substring of the source text?"
    "   - [ ] Does the 'improved_phrase' (value) maintain the same number of placeholders as the key?",
]


def get_proofer(model: Model | None = None):
    Proofer = Agent(
        name="Proofer",
        role="错词检查专家",
        model=model or default_model,
        markdown=False,
        description=description,
        instructions=instructions,
        output_schema=ProofreadingResult,
        use_json_mode=True,
        # reasoning=False,
        # debug_mode=True,
    )
    return Proofer
