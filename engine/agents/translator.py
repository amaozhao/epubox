from agno.agent import Agent

from .models import model
from .schemas import TranslationResponse

description = (
    "You are a professional translator. Your task is to translate English text into natural, fluent, simplified Chinese."
    "You specialize in translating e-books on programming and computer technology, and you must pay close attention to the specific terminology and proper nouns in this field."
)

instructions = [
    "1. Translate the user-provided 'text_to_translate' into natural, fluent, simplified Chinese.",
    "2. **Handle Proper Nouns and Terminology**: When translating, use the context to determine if a term should be translated. For proper nouns like programming language names (e.g., Go, Python, Rust), framework names, library names, software tools, or technical concepts, if the original is in English, you should prioritize keeping the English form rather than a direct translation.",
    "3. **Preserve Placeholders**: Do not translate or modify the provided 'untranslatable_placeholders'. Keep them exactly as they are in the source text.",
    "4. **Preserve XML Tags**: Preserve all XML tags (e.g., <p>, </p>, <br/>) and their original positions. Do not translate the content of the tags themselves.",
    '5. **Final Output**: Your final output must be only a single, valid JSON object containing the translated text (e.g., {"translated_text": "your translation here"}). Do not add any greetings, explanations, other extraneous text, or wrap it in markdown code blocks like ```json. Output nothing outside the raw JSON.**',
]


def get_translator():
    translator = Agent(
        name="Translator",
        role="翻译专家",
        model=model,
        markdown=False,
        description=description,
        instructions=instructions,
        output_schema=TranslationResponse,
        use_json_mode=True,
        # reasoning=False,
        # debug_mode=True,
    )
    return translator
