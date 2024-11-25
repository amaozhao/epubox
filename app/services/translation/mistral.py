"""Mistral-based translator implementation."""

import asyncio
from typing import Dict, List, Optional

from mistralai.async_client import MistralAsyncClient
from mistralai.models.chat_completion import ChatMessage

from .base import BaseTranslator


class MistralTranslator(BaseTranslator):
    """Translator using Mistral's API."""

    def __init__(
        self,
        api_key: str,
        source_lang: str,
        target_lang: str,
        model: str = "mistral-medium",
    ):
        """Initialize Mistral translator.

        Args:
            api_key: Mistral API key
            source_lang: Source language code
            target_lang: Target language code
            model: Mistral model to use (default: mistral-medium)
        """
        super().__init__(api_key, source_lang, target_lang)
        self.client = MistralAsyncClient(api_key=api_key)
        self.model = model

    async def translate_text(self, text: str) -> str:
        """Translate text using Mistral.

        Args:
            text: Text to translate

        Returns:
            str: Translated text
        """
        messages = [
            ChatMessage(
                role="system",
                content=f"You are a translator. Translate the following text from {self.source_lang} to {self.target_lang}. Provide only the translation, no explanations.",
            ),
            ChatMessage(role="user", content=text),
        ]

        response = await self.client.chat(model=self.model, messages=messages)
        return response.choices[0].message.content.strip()

    async def translate_batch(self, texts: List[str]) -> List[str]:
        """Translate multiple texts using Mistral.

        Args:
            texts: List of texts to translate

        Returns:
            List[str]: List of translated texts
        """
        tasks = [self.translate_text(text) for text in texts]
        return await asyncio.gather(*tasks)

    async def detect_language(self, text: str) -> str:
        """Detect language using Mistral.

        Args:
            text: Text to analyze

        Returns:
            str: Detected language code
        """
        messages = [
            ChatMessage(
                role="system",
                content="You are a language detector. Detect the language of the following text and respond with only the ISO 639-1 language code (e.g., 'en', 'es', 'fr').",
            ),
            ChatMessage(role="user", content=text),
        ]

        response = await self.client.chat(model=self.model, messages=messages)
        return response.choices[0].message.content.strip().lower()

    def get_supported_languages(self) -> List[str]:
        """Get list of supported languages.

        Returns:
            List[str]: List of supported language codes
        """
        # Mistral supports most languages through its models
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
