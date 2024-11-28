"""Translation service for EPUB content."""

import asyncio
from typing import Dict, List, Optional

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.logging import services_logger as logger
from app.services.translation import TranslationError

logger = logger.bind(service="translator")


class TranslationService:
    """Service for translating text content while preserving markers."""

    def __init__(self, api_key: Optional[str] = None):
        """Initialize translation service with API key."""
        self.client = AsyncOpenAI(api_key=api_key or settings.openai_api_key)
        self.system_prompt = """You are a professional translator. 
        Translate the text while preserving any special markers in the format __TAG_X__ or __UNTRANSLATABLE_X__.
        These markers must remain exactly as they are in the original text.
        Maintain the same tone and style as the original text."""

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        reraise=True,
    )
    async def _translate_text(
        self, text: str, source_lang: str, target_lang: str
    ) -> str:
        """Translate a single piece of text using OpenAI API."""
        try:
            user_prompt = f"Translate the following text from {source_lang} to {target_lang}:\n\n{text}"

            response = await self.client.chat.completions.create(
                model="gpt-4-1106-preview",  # Using latest GPT-4 for best translation quality
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,  # Lower temperature for more consistent translations
                max_tokens=4000,
            )

            translated_text = response.choices[0].message.content.strip()
            return translated_text

        except Exception as e:
            logger.error(
                "Translation failed",
                error=str(e),
                source_lang=source_lang,
                target_lang=target_lang,
            )
            raise TranslationError(f"Failed to translate text: {str(e)}")

    async def translate_chunks(
        self, chunks: List[Dict], source_lang: str, target_lang: str
    ) -> List[Dict]:
        """Translate a list of content chunks while preserving markers."""
        tasks = []
        translated_chunks = []

        for chunk in chunks:
            if chunk["content_type"] == "translatable":
                # Create translation task
                task = asyncio.create_task(
                    self._translate_text(chunk["content"], source_lang, target_lang)
                )
                tasks.append((chunk, task))
            else:
                # Keep untranslatable chunks as is
                translated_chunks.append(chunk)

        # Wait for all translations to complete
        for chunk, task in tasks:
            try:
                translated_text = await task
                chunk_copy = chunk.copy()
                chunk_copy["translated_content"] = translated_text
                translated_chunks.append(chunk_copy)
            except Exception as e:
                logger.error(
                    "Chunk translation failed",
                    error=str(e),
                    chunk_id=chunk.get("node_index"),
                    source_lang=source_lang,
                    target_lang=target_lang,
                )
                raise TranslationError(f"Failed to translate chunk: {str(e)}")

        # Sort chunks back to original order
        translated_chunks.sort(key=lambda x: x["node_index"])
        return translated_chunks

    async def validate_translation(self, original: str, translated: str) -> bool:
        """Validate that markers are preserved in translation."""
        import re

        # Extract all markers from original and translated text
        marker_pattern = r"(__TAG_\d+__|__UNTRANSLATABLE_\d+__)"
        original_markers = set(re.findall(marker_pattern, original))
        translated_markers = set(re.findall(marker_pattern, translated))

        # Check if all markers are preserved
        return original_markers == translated_markers
