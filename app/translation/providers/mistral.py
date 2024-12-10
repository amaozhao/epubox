"""Mistral translation provider."""

from typing import Dict

import tiktoken
from mistralai import Mistral, models

from app.core.logging import get_logger
from app.translation.errors import ConfigurationError, TranslationError
from app.translation.models import LimitType
from app.translation.models import TranslationProvider as TranslationProviderModel
from app.translation.providers.base import TranslationProvider

logger = get_logger(__name__)


class MistralProvider(TranslationProvider):
    """Mistral translation provider implementation."""

    def __init__(self, provider_model: TranslationProviderModel):
        super().__init__(provider_model)
        if provider_model.limit_type != LimitType.TOKENS:
            raise ValueError("Mistral provider must use token-based limits")

        self.api_key = self.config.get("api_key")
        self.model = self.config.get("model", "mistral-tiny")
        self.client = Mistral(api_key=self.api_key)
        self.tokenizer = tiktoken.get_encoding("cl100k_base")

    def get_provider_type(self) -> str:
        """Get provider type identifier."""
        return "mistral"

    def validate_config(self, config: dict) -> bool:
        """Validate provider configuration."""
        if not config.get("api_key"):
            raise ConfigurationError("Mistral API key is required")
        return True

    def _count_tokens(self, text: str) -> int:
        """Count tokens in text using tiktoken."""
        if not self.tokenizer:
            raise ConfigurationError("Tokenizer not initialized")
        return len(self.tokenizer.encode(text))

    async def _translate(
        self, text: str, source_lang: str, target_lang: str, **kwargs
    ) -> str:
        """Translate text using Mistral API."""
        if not self.client:
            raise ConfigurationError("Mistral client not initialized")

        if not text:
            raise ValueError("Empty text provided for translation")

        token_count = self._count_tokens(text)
        if token_count > self.provider_model.limit_value:
            raise TranslationError(
                f"Text length ({token_count} tokens) exceeds maximum allowed ({self.provider_model.limit_value} tokens)"
            )

        messages = [
            models.UserMessage(
                content=(
                    f"Translate the following text from {source_lang} to {target_lang}. "
                    "CRITICAL REQUIREMENTS:\n"
                    "1. PRESERVE ALL HTML TAGS EXACTLY AS THEY APPEAR\n"
                    "   - Keep all HTML tags unchanged\n"
                    "   - Maintain all HTML attributes\n"
                    "   - Only translate the text content between tags\n"
                    "2. PRESERVE ALL PLACEHOLDERS\n"
                    "   - Placeholders are in the format †number† (e.g., †0†, †1†)\n"
                    "   - Keep them EXACTLY as they appear\n"
                    "   - DO NOT translate them\n"
                    "   - DO NOT change their format\n"
                    "3. Return the complete text with all HTML structure and placeholders intact\n"
                    "4. Do not add any explanations\n\n"
                    "Example:\n"
                    "Input: '<p>Hello †0†, <span class=\"name\">world</span>!</p>'\n"
                    "Output: '<p>你好 †0†，<span class=\"name\">世界</span>！</p>'\n\n"
                    f"Text to translate: {text}"
                )
            )
        ]

        try:
            response = await self.client.chat.complete_async(
                model=self.model,
                messages=messages,
                **kwargs,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error("Translation request failed", error=str(e))
            raise TranslationError(f"Translation failed: {str(e)}")
