"""Translation service factory."""

from typing import Optional

from . import ProviderNotConfiguredError, TranslationProvider
from .base import BaseTranslator
from .google import GoogleTranslator
from .mistral import MistralTranslator
from .mock import MockTranslator
from .openai import OpenAITranslator


def create_translator(
    provider: TranslationProvider,
    api_key: str,
    source_lang: str,
    target_lang: str,
    **kwargs,
) -> BaseTranslator:
    """Create a translator instance based on the specified provider.

    Args:
        provider: Translation provider to use
        api_key: Provider API key
        source_lang: Source language code
        target_lang: Target language code
        **kwargs: Additional provider-specific arguments

    Returns:
        BaseTranslator: Configured translator instance

    Raises:
        ProviderNotConfiguredError: If provider is not properly configured
    """
    if provider == TranslationProvider.MOCK:
        return MockTranslator(api_key, source_lang, target_lang)
    elif provider == TranslationProvider.OPENAI:
        model = kwargs.get("model", "gpt-3.5-turbo")
        return OpenAITranslator(api_key, source_lang, target_lang, model=model)
    elif provider == TranslationProvider.MISTRAL:
        model = kwargs.get("model", "mistral-medium")
        return MistralTranslator(api_key, source_lang, target_lang, model=model)
    elif provider == TranslationProvider.GOOGLE:
        return GoogleTranslator(api_key, source_lang, target_lang)
    else:
        raise ProviderNotConfiguredError(
            f"Translation provider {provider} is not configured"
        )
