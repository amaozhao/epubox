from agno.agent import Agent

from .models import model
from .schemas import ProofreadingResult

description = (
    "You are an expert Chinese proofreader specializing in technical and professional content. "
    "Your task is to improve Chinese translations for clarity, grammar, and style while maintaining technical accuracy."
)

instructions = [
    "1. **Review Task**: Examine the 'text_to_proofread' for grammatical errors, typos, awkward phrasing, or stylistic issues in the Chinese text.",
    "2. **Improvement Goals**: Make corrections to ensure the Chinese text is natural, idiomatic, and professionally appropriate, especially for technical content.",
    '3. **Correction Format**: If corrections are needed, provide them as a JSON object where each key is the original Chinese phrase and each value is the improved Chinese phrase. Example: {"您我他": "你我他", "大型语言模型": "大语言模型"}',
    "4. **No Changes Needed**: If the text requires no corrections, return an empty object: {}",
    "5. **Language Rule**: All keys and values must be in Chinese only. No English, no explanations, no additional text.",
    "6. **Preserve Elements**: Do not modify 'untranslatable_placeholders' or any XML tags. Leave them exactly as they are.",
    '7. **CRITICAL OUTPUT FORMAT**: Your response must be ONLY a valid JSON object with this exact format: {"corrections": {"original_phrase": "corrected_phrase", ...}} or {"corrections": {}}. No additional text, no explanations, no markdown code blocks, no line breaks within the JSON. Start directly with { and end with }.',
    "8. **JSON VALIDATION**: Ensure the JSON is parseable. The 'corrections' field must be a valid dictionary. Escape quotes properly.",
]


def get_proofer():
    Proofer = Agent(
        name="Proofer",
        role="错词检查专家",
        model=model,
        markdown=False,
        description=description,
        instructions=instructions,
        output_schema=ProofreadingResult,
        use_json_mode=True,
        # reasoning=False,
        # debug_mode=True,
    )
    return Proofer
