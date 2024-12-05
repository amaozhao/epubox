"""Test translation error handling."""

import pytest

from app.translation.errors import (
    ConfigurationError,
    ProviderError,
    RateLimitError,
    TranslationError,
)


def test_translation_error_hierarchy():
    """Test that all error types inherit from TranslationError."""
    assert issubclass(RateLimitError, TranslationError)
    assert issubclass(ConfigurationError, TranslationError)
    assert issubclass(ProviderError, TranslationError)


def test_base_error():
    """Test base TranslationError creation and properties."""
    # Test with message only
    error = TranslationError("Base error")
    assert str(error) == "Base error"
    assert error.details == {}

    # Test with details
    details = {"key": "value"}
    error = TranslationError("Base error", details=details)
    assert str(error) == "Base error"
    assert error.details == details


def test_rate_limit_error():
    """Test RateLimitError creation and message."""
    # Test with message only
    error = RateLimitError("Rate limit exceeded")
    assert str(error) == "Rate limit exceeded"
    assert error.details == {}
    assert isinstance(error, TranslationError)

    # Test with details
    details = {"provider": "openai", "rate_limit": 3, "current_requests": 5}
    error = RateLimitError("Rate limit exceeded", details=details)
    assert str(error) == "Rate limit exceeded"
    assert error.details == details


def test_configuration_error():
    """Test ConfigurationError creation and message."""
    # Test with message only
    error = ConfigurationError("Invalid API key")
    assert str(error) == "Invalid API key"
    assert error.details == {}
    assert isinstance(error, TranslationError)

    # Test with details
    details = {"provider": "google", "config_key": "api_key", "reason": "missing"}
    error = ConfigurationError("Invalid API key", details=details)
    assert str(error) == "Invalid API key"
    assert error.details == details


def test_provider_error():
    """Test ProviderError creation and message."""
    # Test with message only
    error = ProviderError("Provider not found")
    assert str(error) == "Provider not found"
    assert error.details == {}
    assert isinstance(error, TranslationError)

    # Test with details
    details = {"provider_id": 123, "provider_type": "openai", "status": "disabled"}
    error = ProviderError("Provider not found", details=details)
    assert str(error) == "Provider not found"
    assert error.details == details
