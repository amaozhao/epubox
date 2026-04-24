from agno.agent import Agent
from agno.models.base import Model

from .models import model as default_model
from .schemas import TranslationResponse

description = (
    "You are a professional translator specialized in converting English technical content into natural, "
    "fluent, simplified Chinese. "
    "You excel at translating programming and computer technology e-books while maintaining technical accuracy."
)

BASE_INSTRUCTIONS = [
    '1. **JSON OUTPUT ONLY**: Return ONLY {"translation": "..."} with no extra keys or commentary.',
    "2. **GLOSSARY**: Use the 'glossaries' dictionary for technical term translations.",
    "3. **RETRY INPUTS**: The input may include 'validation_error' and 'previous_translation'. 'validation_error' may summarize multiple previous failures. When present, repair the previous translation to satisfy those constraints instead of restarting from scratch.",
    "4. **MINIMAL REPAIR ON RETRY**: If 'previous_translation' is provided, keep all already-correct content unchanged and make only the smallest edit needed to fix the reported problem.",
]

HTML_MODE_INSTRUCTIONS = [
    "5. **TRANSLATION**: Translate the HTML content into natural, fluent, simplified Chinese.",
    "6. **HTML STRUCTURE**:",
    "   - Preserve ALL HTML tags exactly as they are.",
    "   - Keep tag attributes unchanged (class, id, href, src, etc.).",
    "   - Maintain the same number and nesting of elements.",
    "   - Only translate text content between tags.",
    "7. **PLACEHOLDERS**: [PRE:N], [CODE:N], [STYLE:N] are protected placeholders. Copy them VERBATIM, keep every index unchanged, and keep their left-to-right order EXACTLY the same as the source.",
    "7.1 **NO REORDERING AROUND PLACEHOLDERS**: Do NOT move, swap, merge, split, or regroup any phrase that contains a placeholder. If a sentence contains placeholders, translate only the natural-language text around them while leaving the placeholder-bearing spans in place.",
    "7.2 **NAV MARKERS**: [NAVTXT:N] markers delimit nav text units. Keep markers EXACT, keep one marker per unit, and keep their original order.",
]

TEXT_NODE_MODE_INSTRUCTIONS = [
    "5. **TEXT-NODE MODE**: The input is plain text lines prefixed with [TEXT:N] markers.",
    "6. **LINE PRESERVATION**:",
    "   - Output the same number of non-empty lines as the input.",
    "   - Each output line must start with the exact same [TEXT:N] marker as its corresponding input line.",
    "   - Do not add, remove, duplicate, merge, split, or renumber markers.",
    "7. **PAYLOAD ONLY**: Translate only the payload after each marker. Keep the marker untouched.",
    "8. **RETRY REPAIR RULE**: If 'previous_translation' is provided, copy its marker skeleton exactly and only repair the missing or incorrect marker lines/payloads.",
    "9. **NO EXTRA TEXT**: Do not add explanations, headings, code fences, blank lines, or any text before the first marker or after the last marker.",
]


def _build_instructions(mode: str) -> list[str]:
    if mode == "text_node":
        return [*BASE_INSTRUCTIONS, *TEXT_NODE_MODE_INSTRUCTIONS]
    return [*BASE_INSTRUCTIONS, *HTML_MODE_INSTRUCTIONS]


instructions = _build_instructions("html")
text_node_instructions = _build_instructions("text_node")


def get_translator(model: Model | None = None, mode: str = "html"):
    translator = Agent(
        name="Translator",
        role="翻译专家",
        model=model or default_model,
        markdown=False,
        description=description,
        instructions=_build_instructions(mode),
        output_schema=TranslationResponse,
        use_json_mode=True,
    )
    return translator
