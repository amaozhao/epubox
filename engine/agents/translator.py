from agno.agent import Agent
from agno.models.base import Model

from .models import model as default_model
from .schemas import TranslationResponse

description = (
    "You are a professional translator specialized in converting English technical content into natural, "
    "fluent, simplified Chinese. "
    "You excel at translating programming and computer technology e-books while maintaining technical accuracy."
)

instructions = [
    '1. **JSON OUTPUT ONLY**: Return ONLY {"translation": "..."}.',
    "2. **TRANSLATION**: Translate the HTML content into natural, fluent, simplified Chinese.",
    "3. **HTML STRUCTURE**:",
    "   - Preserve ALL HTML tags exactly as they are.",
    "   - Keep tag attributes unchanged (class, id, href, src, etc.).",
    "   - Maintain the same number and nesting of elements.",
    "   - Only translate text content between tags.",
    "4. **PLACEHOLDERS**: [PRE:N], [CODE:N], [STYLE:N] are protected placeholders. Copy them VERBATIM, keep every index unchanged, and keep their left-to-right order EXACTLY the same as the source.",
    "4.1 **NO REORDERING AROUND PLACEHOLDERS**: Do NOT move, swap, merge, split, or regroup any phrase that contains a placeholder. If a sentence contains placeholders, translate only the natural-language text around them while leaving the placeholder-bearing spans in place.",
    "4.2 **NAV MARKERS**: [NAVTXT:N] markers delimit nav text units. Keep markers EXACT, keep one marker per unit, and keep their original order.",
    "4.3 **TEXT MARKERS**: [TEXT:N] markers delimit HTML text nodes in fallback mode. Keep every marker EXACT, keep their original order, and translate only the payload after each marker.",
    "5. **GLOSSARY**: Use the 'glossaries' dictionary for technical term translations.",
]


def get_translator(model: Model | None = None):
    translator = Agent(
        name="Translator",
        role="翻译专家",
        model=model or default_model,
        markdown=False,
        description=description,
        instructions=instructions,
        output_schema=TranslationResponse,
        use_json_mode=True,
    )
    return translator
