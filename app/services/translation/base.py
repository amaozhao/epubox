"""Base translation service interface."""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from . import TranslationProvider


class BaseTranslator(ABC):
    """Abstract base class for translation providers."""

    def __init__(self, api_key: str, source_lang: str, target_lang: str):
        """Initialize translator with API key and language settings.

        Args:
            api_key: Provider API key
            source_lang: Source language code
            target_lang: Target language code
        """
        self.api_key = api_key
        self.source_lang = source_lang
        self.target_lang = target_lang

    @abstractmethod
    async def translate_text(self, text: str) -> str:
        """Translate a single text string.

        Args:
            text: Text to translate

        Returns:
            str: Translated text
        """
        pass

    @abstractmethod
    async def translate_batch(self, texts: List[str]) -> List[str]:
        """Translate multiple texts in batch.

        Args:
            texts: List of texts to translate

        Returns:
            List[str]: List of translated texts
        """
        pass

    @abstractmethod
    async def detect_language(self, text: str) -> str:
        """Detect the language of input text.

        Args:
            text: Text to analyze

        Returns:
            str: Detected language code
        """
        pass

    @abstractmethod
    def get_supported_languages(self) -> List[str]:
        """Get list of supported language codes.

        Returns:
            List[str]: List of supported language codes
        """
        pass
