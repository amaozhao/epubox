"""服务模块"""

from .processors.epub import EPUBProcessor, EPUBProcessorError
from .processors.html import HTMLProcessingError, HTMLProcessor
from .translation.google_translation_service import GoogleTranslationService
from .translation.mistral_translation_service import MistralTranslationService
from .translation.openai_translation_service import OpenAITranslationService
from .translation.translation_manager import TranslationManager
from .translation.translation_service import TranslationError, TranslationService
from .translation.translator import (
    DeepLTranslator,
    GoogleTranslator,
    MistralTranslator,
    OpenAITranslator,
    TranslationProvider,
    create_translator,
)

__all__ = [
    "EPUBProcessor",
    "EPUBProcessorError",
    "HTMLProcessor",
    "HTMLProcessingError",
    "TranslationService",
    "TranslationError",
    "GoogleTranslationService",
    "MistralTranslationService",
    "OpenAITranslationService",
    "TranslationManager",
    "TranslationProvider",
    "OpenAITranslator",
    "GoogleTranslator",
    "MistralTranslator",
    "DeepLTranslator",
    "create_translator",
]
