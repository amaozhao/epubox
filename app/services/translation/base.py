from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from pydantic import BaseModel
import warnings

class TranslationRequest(BaseModel):
    text: str
    source_language: str
    target_language: str
    context: Optional[Dict] = None

class TranslationResponse(BaseModel):
    translated_text: str
    source_language: str
    target_language: str
    confidence: Optional[float] = None
    service_metadata: Optional[Dict] = None

class BaseTranslationAdapter(ABC):
    """Base class for all translation service adapters."""
    
    def __init__(self, api_key: str, **kwargs):
        self.api_key = api_key
        self.config = kwargs

    @abstractmethod
    async def translate_text(self, request: TranslationRequest) -> TranslationResponse:
        """Translate a single piece of text."""
        pass

    async def translate_text_legacy(self, text: str, source_language: str, target_language: str) -> str:
        """
        Legacy method for translating text. This method is deprecated.
        Please use translate_text with TranslationRequest instead.
        """
        warnings.warn(
            "translate_text_legacy is deprecated. Please use translate_text with TranslationRequest instead.",
            DeprecationWarning,
            stacklevel=2
        )
        request = TranslationRequest(
            text=text,
            source_language=source_language,
            target_language=target_language
        )
        response = await self.translate_text(request)
        return response.translated_text

    @abstractmethod
    async def translate_batch(self, requests: List[TranslationRequest]) -> List[TranslationResponse]:
        """Translate multiple pieces of text in batch."""
        pass

    @abstractmethod
    async def detect_language(self, text: str) -> str:
        """Detect the language of a text."""
        pass

    @abstractmethod
    def get_supported_languages(self) -> List[str]:
        """Get list of supported language codes."""
        pass

    @abstractmethod
    async def get_translation_cost(self, text: str, source_lang: str, target_lang: str) -> float:
        """Calculate the cost of translation."""
        pass

    @abstractmethod
    async def validate_languages(self, source_lang: str, target_lang: str) -> bool:
        """Validate if the language pair is supported."""
        pass

class TranslationError(Exception):
    """Base class for translation-related errors."""
    def __init__(self, message: str, service: str, details: Optional[Dict] = None):
        self.message = message
        self.service = service
        self.details = details or {}
        super().__init__(self.message)

class UnsupportedLanguageError(TranslationError):
    """Raised when a language is not supported."""
    pass

class TranslationQuotaExceededError(TranslationError):
    """Raised when translation quota is exceeded."""
    pass

class TranslationAPIError(TranslationError):
    """Raised when there's an API error."""
    pass

class InvalidRequestError(TranslationError):
    """Raised when the request is invalid."""
    pass
