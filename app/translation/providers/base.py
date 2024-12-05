"""
Base translation provider.
Defines the interface for translation providers.
"""

from abc import ABC, abstractmethod


class TranslationProvider(ABC):
    """Abstract base class for translation providers."""

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    async def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        """Translate text from source language to target language."""
        pass

    @abstractmethod
    def validate_config(self) -> bool:
        """Validate provider configuration."""
        pass
