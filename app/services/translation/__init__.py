"""Translation service package for handling EPUB content translation."""

from enum import Enum
from typing import Dict, List, Optional, Union


class TranslationProvider(str, Enum):
    """Supported translation providers."""

    GOOGLE = "google"
    DEEPL = "deepl"
    MOCK = "mock"  # For testing purposes
    OPENAI = "openai"
    MISTRAL = "mistral"


class TranslationError(Exception):
    """Base exception for translation-related errors."""

    pass


class ProviderNotConfiguredError(TranslationError):
    """Raised when a translation provider is not properly configured."""

    pass


class TranslationLimitExceededError(TranslationError):
    """Raised when translation quota or rate limit is exceeded."""

    pass
