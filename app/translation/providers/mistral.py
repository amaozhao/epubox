"""Mistral translation provider."""

from typing import Optional

import tiktoken
from mistralai import Mistral, models

from ..errors import ConfigurationError, TranslationError
from ..models import LimitType
from .base import TranslationProvider


class MistralProvider(TranslationProvider):
    """Mistral translation provider implementation."""

    def __init__(self, provider_model: "TranslationProviderModel"):
        super().__init__(provider_model)
        if provider_model.limit_type != LimitType.TOKENS:
            raise ValueError("Mistral provider must use token-based limits")

        self.api_key = self.config.get("api_key")
        self.model = self.config.get("model", "mistral-tiny")
        self.client = Mistral(api_key=self.api_key)
        self.tokenizer = self._initialize_tokenizer()

    def get_provider_type(self) -> str:
        """Get provider type identifier."""
        return "mistral"

    def validate_config(self, config: dict) -> bool:
        """Validate provider configuration."""
        if not config.get("api_key"):
            raise ConfigurationError("Mistral API key is required")
        return True

    def _initialize_tokenizer(self):
        """Initialize the tokenizer for counting tokens."""
        # Mistral uses cl100k_base encoding (same as GPT-4)
        return tiktoken.get_encoding("cl100k_base")

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

        try:
            messages = [
                models.UserMessage(
                    content=f"Translate the following text from {source_lang} to {target_lang}. "
                    f"Provide only the translated text without any explanations: {text}"
                )
            ]

            response = await self.client.chat.complete_async(
                model=self.model, messages=messages, **kwargs
            )

            return response.choices[0].message.content.strip()
        except models.SDKError as e:
            if e.status_code == 429:  # Rate limit error
                raise TranslationError(f"Rate limit exceeded: {e.message}")
            raise TranslationError(f"Mistral API error: {e.message}")
        except models.HTTPValidationError as e:
            raise TranslationError(f"Invalid request: {str(e)}")
        except Exception as e:
            raise TranslationError(f"Translation failed: {str(e)}")
