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

from ..errors import ConfigurationError, ProviderError, RateLimitError, TranslationError
from ..models import LimitType
from ..models import TranslationProvider as TranslationProviderModel

T = TypeVar("T")


class RateLimiter:
    """Rate limiter for translation providers."""

    def __init__(self, requests_per_second: int = 1, time_window: int = 1):
        self.rate_limit = requests_per_second
        self.time_window = time_window
        self.tokens = requests_per_second
        self.last_update = time.time()
        self.lock = asyncio.Lock()

    async def acquire(self):
        """Acquire a token from the rate limiter."""
        async with self.lock:
            now = time.time()
            time_passed = now - self.last_update
            self.tokens = min(
                self.rate_limit,
                self.tokens + time_passed * (self.rate_limit / self.time_window),
            )
            self.last_update = now

            # Check if we have enough tokens
            if self.tokens < 1:
                raise RateLimitError("Rate limit exceeded")

            # If we have enough tokens, decrease the count
            self.tokens -= 1


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
        self.provider_model = provider_model
        self.config = provider_model.config
        self.rate_limiter = RateLimiter(requests_per_second=provider_model.rate_limit)
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
        await self.rate_limiter.acquire()

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
