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
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.logging import get_logger
from app.db.models import LimitType
from app.db.models import TranslationProvider as TranslationProviderModel

from ..errors import ConfigurationError, TranslationError

T = TypeVar("T")

logger = get_logger(__name__)


def log_retry_attempt(retry_state):
    """记录重试尝试."""
    exception = retry_state.outcome.exception()
    if exception:
        logger.warning(
            "Translation request failed, retrying",
            attempt=retry_state.attempt_number,
            error=str(exception),
        )


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

    async def check_limits(self, text: str):
        """Check all applicable limits."""
        count = self.count_units(text)
        if count > self.provider_model.limit_value:
            raise TranslationError(
                f"Text length ({count} {self.provider_model.limit_type.value}) "
                f"exceeds maximum allowed ({self.provider_model.limit_value})"
            )

    @abstractmethod
    async def _translate(
        self, text: str, source_lang: str, target_lang: str, **kwargs
    ) -> str:
        """Translate text from source language to target language.

        This is the core translation method that each provider must implement.
        It should focus only on the translation logic, without handling retries or limits.

        Args:
            text: Text to translate.
            source_lang: Source language code.
            target_lang: Target language code.
            **kwargs: Additional provider-specific arguments.

        Returns:
            Translated text.

        Raises:
            TranslationError: If translation fails.
        """
        pass

    @retry(
        stop=stop_after_attempt(3),  # 最多重试3次
        wait=wait_exponential(multiplier=1, min=4, max=10),  # 指数退避：4s, 8s, 16s
        before_sleep=log_retry_attempt,  # 使用自定义的日志函数
        retry=(
            retry_if_exception_type(TranslationError)
            | retry_if_exception_type(ConfigurationError)
        ),
    )
    async def translate(
        self, text: str, source_lang: str, target_lang: str, **kwargs
    ) -> str:
        """Translate text from source language to target language.

        This method handles all the common logic like:
        - Empty text checking
        - Length/token limits checking
        - Retries with exponential backoff
        - Error handling and logging

        Args:
            text: Text to translate.
            source_lang: Source language code.
            target_lang: Target language code.
            **kwargs: Additional provider-specific arguments.

        Returns:
            Translated text.

        Raises:
            TranslationError: If translation fails after all retries.
        """
        if not text:
            return text

        await self.check_limits(text)

        try:
            return await self._translate(text, source_lang, target_lang, **kwargs)
        except Exception as e:
            raise TranslationError(f"Translation failed: {str(e)}")
