"""
Base translation provider.
Defines the interface and common components for translation providers.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, Generic, Optional, TypeVar

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..errors import ConfigurationError, TranslationError
from ..models import LimitType
from ..models import TranslationProvider as TranslationProviderModel

T = TypeVar("T")


class AsyncContextManager(Generic[T]):
    """Async context manager base class."""

    async def __aenter__(self) -> T:
        """Initialize resources."""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Cleanup resources."""
        await self.cleanup()

    async def initialize(self):
        """Initialize resources."""
        pass

    async def cleanup(self):
        """Cleanup resources."""
        pass


class TranslationProvider(AsyncContextManager["TranslationProvider"], ABC):
    """Base class for translation providers."""

    def __init__(self, provider_model: TranslationProviderModel):
        """Initialize the provider with model configuration."""
        # 确保 config 是字典类型
        if isinstance(provider_model.config, str):
            import json

            try:
                config = json.loads(provider_model.config)
            except json.JSONDecodeError as e:
                raise ConfigurationError(f"Invalid JSON in config: {e}")
        else:
            config = provider_model.config

        # 在设置任何配置之前先验证
        self.validate_config(config)

        self.provider_model = provider_model
        self.config = config
        self.retry_count = provider_model.retry_count
        self.retry_delay = provider_model.retry_delay
        self._initialized = False

    @abstractmethod
    def get_provider_type(self) -> str:
        """Get the provider type identifier."""
        pass

    @abstractmethod
    def validate_config(self, config: dict):
        """Validate provider configuration."""
        pass

    def count_units(self, text: str) -> int:
        """Count text units based on provider's limit type."""
        if self.provider_model.limit_type == LimitType.CHARS:
            return len(text)
        elif self.provider_model.limit_type == LimitType.TOKENS:
            return self._count_tokens(text)
        raise ValueError(f"Unsupported limit type: {self.provider_model.limit_type}")

    def _count_tokens(self, text: str) -> int:
        """Count tokens in text. Override this in token-based providers."""
        raise NotImplementedError("Token counting not implemented for this provider")

    @abstractmethod
    async def check_rate_limit(self):
        """Check rate limit. Override this in providers that need rate limiting."""
        pass

    async def check_limits(self, text: str):
        """Check all applicable limits."""
        count = self.count_units(text)
        if count > self.provider_model.limit_value:
            raise TranslationError(
                f"Text length ({count} {self.provider_model.limit_type.value}) "
                f"exceeds maximum allowed ({self.provider_model.limit_value})"
            )
        await self.check_rate_limit()

    async def translate(
        self, text: str, source_lang: str, target_lang: str, **kwargs
    ) -> str:
        """Translate text from source language to target language."""
        # Check limits
        await self.check_limits(text)

        # Use tenacity for retrying
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.retry_count),
            wait=wait_exponential(multiplier=self.retry_delay, min=1, max=10),
            retry=retry_if_exception_type((TranslationError, ConnectionError)),
            reraise=True,
        ):
            with attempt:
                return await self._translate(
                    text=text,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    **kwargs,
                )

    @abstractmethod
    async def _translate(
        self, text: str, source_lang: str, target_lang: str, **kwargs
    ) -> str:
        """Provider-specific translation implementation."""
        pass
