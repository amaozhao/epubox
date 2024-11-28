"""EPUB translation service."""

import asyncio
import os
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup
from ebooklib import epub

from app.core.config import settings
from app.core.logging import get_logger
from app.models.translation_chunk import TranslationChunk
from app.models.translation_project import TranslationProject
from app.services.epub import HTMLSplitter

from .base import BaseTranslator
from .factory import create_translator

logger = get_logger(__name__)


class EPUBTranslator:
    """Service for translating EPUB files while preserving formatting."""

    def __init__(
        self,
        translator: BaseTranslator,
        source_lang: str,
        target_lang: str,
        preserve_formatting: bool = True,
    ):
        """Initialize EPUB translator.

        Args:
            translator: Translation service to use
            source_lang: Source language code
            target_lang: Target language code
            preserve_formatting: Whether to preserve HTML formatting
        """
        self.translator = translator
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.preserve_formatting = preserve_formatting
        self.html_splitter = HTMLSplitter()

    async def translate_epub(self, input_path: str, output_path: str) -> Dict:
        """Translate an EPUB file.

        Args:
            input_path: Path to input EPUB file
            output_path: Path to save translated EPUB

        Returns:
            Dict containing translation statistics
        """
        # Read input EPUB
        book = epub.read_epub(input_path)

        # Track statistics
        stats = {
            "total_items": 0,
            "translated_items": 0,
            "total_words": 0,
            "translated_words": 0,
            "skipped_items": 0,
        }

        # Process each document
        for item in book.get_items():
            if item.get_type() == epub.ITEM_DOCUMENT:
                stats["total_items"] += 1

                try:
                    # Get HTML content
                    content = item.get_content().decode("utf-8")

                    # Translate content
                    translated_content = await self._translate_html_content(content)

                    # Update item content
                    item.set_content(translated_content.encode("utf-8"))

                    stats["translated_items"] += 1

                except Exception as e:
                    logger.error(f"Failed to translate item: {e}")
                    stats["skipped_items"] += 1

        # Save translated EPUB
        epub.write_epub(output_path, book)

        return stats

    async def _translate_html_content(self, html_content: str) -> str:
        """Translate HTML content while preserving formatting.

        Args:
            html_content: HTML content to translate

        Returns:
            Translated HTML content
        """
        if self.preserve_formatting:
            # Split content into translatable parts
            parts = self.html_splitter.split_content(html_content)

            # Extract translatable text
            texts_to_translate = []
            for part in parts:
                if part["type"] == "translatable":
                    texts_to_translate.append(part["content"])

            # Translate texts in batches
            translated_texts = await self.translator.translate_batch(texts_to_translate)

            # Update parts with translations
            text_index = 0
            for part in parts:
                if part["type"] == "translatable":
                    part["content"] = translated_texts[text_index]
                    text_index += 1

            # Reassemble HTML
            return self.html_splitter.reassemble_content(parts, html_content)
        else:
            # Simple translation without preserving formatting
            soup = BeautifulSoup(html_content, "html.parser")
            text = soup.get_text()
            translated_text = await self.translator.translate_text(text)
            return f"<html><body>{translated_text}</body></html>"

    @classmethod
    async def create(
        cls,
        source_lang: str,
        target_lang: str,
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
        preserve_formatting: bool = True,
    ) -> "EPUBTranslator":
        """Create an EPUB translator instance.

        Args:
            source_lang: Source language code
            target_lang: Target language code
            provider: Translation provider to use
            api_key: Provider API key
            preserve_formatting: Whether to preserve HTML formatting

        Returns:
            EPUBTranslator instance
        """
        # Create translator instance
        translator = await create_translator(
            provider or "mistral",
            api_key or settings.MISTRAL_API_KEY,
            source_lang,
            target_lang,
        )

        return cls(translator, source_lang, target_lang, preserve_formatting)
