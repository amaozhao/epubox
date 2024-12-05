"""Translation service related errors."""

from typing import Any, Dict, Optional


class TranslationError(Exception):
    """Base class for translation errors."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        """Initialize the error.

        Args:
            message: The error message
            details: Optional dictionary containing additional error details
        """
        super().__init__(message)
        self.details = details or {}


class RateLimitError(TranslationError):
    """Raised when rate limit is exceeded."""

    pass


class ConfigurationError(TranslationError):
    """Raised when provider configuration is invalid."""

    pass


class ProviderError(TranslationError):
    """Raised when provider encounters an error."""

    pass
