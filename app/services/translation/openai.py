"""OpenAI-based translator implementation."""

import asyncio
from typing import Dict, List, Optional

from openai import AsyncOpenAI

from app.core.logging import get_logger

from .base import BaseTranslator

# Get logger for translation service
logger = get_logger("app.services.translation.openai")


class OpenAITranslator(BaseTranslator):
    """Translator using OpenAI's API."""

    def __init__(
        self,
        api_key: str,
        source_lang: str,
        target_lang: str,
        model: str = "gpt-3.5-turbo",
    ):
        """Initialize OpenAI translator.

        Args:
            api_key: OpenAI API key
            source_lang: Source language code
            target_lang: Target language code
            model: OpenAI model to use (default: gpt-3.5-turbo)
        """
        super().__init__(api_key, source_lang, target_lang)
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        logger.info(
            "Initialized OpenAI Translator",
            model=model,
            source_lang=source_lang,
            target_lang=target_lang,
        )

    async def translate_text(self, text: str) -> str:
        """Translate text using OpenAI.

        Args:
            text: Text to translate

        Returns:
            str: Translated text
        """
        try:
            logger.debug(
                "Starting translation request",
                text_length=len(text),
                model=self.model,
                source_lang=self.source_lang,
                target_lang=self.target_lang,
            )

            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": f"You are a translator. Translate the following text from {self.source_lang} to {self.target_lang}. Provide only the translation, no explanations.",
                    },
                    {"role": "user", "content": text},
                ],
            )
            translated = response.choices[0].message.content.strip()

            logger.debug(
                "Translation successful",
                text_length=len(text),
                translated_length=len(translated),
                model=self.model,
            )

            return translated
        except Exception as e:
            logger.error(
                "Translation failed",
                error=str(e),
                text_length=len(text),
                model=self.model,
                source_lang=self.source_lang,
                target_lang=self.target_lang,
            )
            raise

    async def translate_batch(self, texts: List[str]) -> List[str]:
        """Translate multiple texts using OpenAI.

        Args:
            texts: List of texts to translate

        Returns:
            List[str]: List of translated texts
        """
        logger.info(
            "Starting batch translation",
            batch_size=len(texts),
            model=self.model,
            source_lang=self.source_lang,
            target_lang=self.target_lang,
        )

        tasks = [self.translate_text(text) for text in texts]
        return await asyncio.gather(*tasks)

    async def detect_language(self, text: str) -> str:
        """Detect language using OpenAI.

        Args:
            text: Text to analyze

        Returns:
            str: Detected language code
        """
        try:
            logger.debug(
                "Starting language detection", text_length=len(text), model=self.model
            )

            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a language detector. Detect the language of the following text and respond with only the ISO 639-1 language code (e.g., 'en', 'es', 'fr').",
                    },
                    {"role": "user", "content": text},
                ],
            )
            detected_lang = response.choices[0].message.content.strip().lower()

            logger.debug(
                "Language detection successful",
                text_length=len(text),
                detected_lang=detected_lang,
                model=self.model,
            )

            return detected_lang
        except Exception as e:
            logger.error(
                "Language detection failed",
                error=str(e),
                text_length=len(text),
                model=self.model,
            )
            raise

    def get_supported_languages(self) -> List[str]:
        """Get list of supported languages.

        Returns:
            List[str]: List of supported language codes
        """
        return [
            "en",
            "es",
            "fr",
            "de",
            "it",
            "pt",
            "nl",
            "pl",
            "ru",
            "ja",
            "ko",
            "zh",
            "ar",
            "hi",
            "vi",
            "th",
            "id",
        ]
