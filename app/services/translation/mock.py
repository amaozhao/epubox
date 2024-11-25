"""Mock translator for testing purposes."""

import asyncio
from typing import Dict, List

from .base import BaseTranslator


class MockTranslator(BaseTranslator):
    """Mock translator that returns modified input for testing."""

    async def translate_text(self, text: str) -> str:
        """Mock translate by adding a prefix.

        Args:
            text: Text to translate

        Returns:
            str: Modified text with [TRANSLATED] prefix
        """
        await asyncio.sleep(0.1)  # Simulate API delay
        return f"[TRANSLATED] {text}"

    async def translate_batch(self, texts: List[str]) -> List[str]:
        """Mock batch translate.

        Args:
            texts: List of texts to translate

        Returns:
            List[str]: List of modified texts
        """
        return [await self.translate_text(text) for text in texts]

    async def detect_language(self, text: str) -> str:
        """Mock language detection.

        Args:
            text: Text to analyze

        Returns:
            str: Always returns 'en'
        """
        await asyncio.sleep(0.1)
        return "en"

    def get_supported_languages(self) -> List[str]:
        """Get mock supported languages.

        Returns:
            List[str]: List of common language codes
        """
        return ["en", "es", "fr", "de", "zh", "ja", "ko"]
