"""
Base translation provider.
Defines the interface and common components for translation providers.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, Generic, Optional, TypeVar

from ..errors import ConfigurationError, ProviderError, RateLimitError, TranslationError

T = TypeVar("T")


class RateLimiter:
    """Rate limiter for translation providers."""

    def __init__(self, rate_limit: int, time_window: int = 60):
        self.rate_limit = rate_limit
        self.time_window = time_window
        self.tokens = rate_limit
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

            if self.tokens < 1:
                raise RateLimitError("Rate limit exceeded")

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


class TranslationProvider(AsyncContextManager["TranslationProvider"]):
    """Base class for translation providers."""

    def __init__(
        self,
        config: dict,
        rate_limit: int = 3,
        retry_count: int = 3,
        retry_delay: int = 1,
    ):
        self.config = config
        self.rate_limiter = RateLimiter(rate_limit)
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        self._initialized = False

    async def initialize(self):
        """Initialize the provider."""
        if not self._initialized:
            self.validate_config(self.config)
            await self._initialize()
            self._initialized = True

    @abstractmethod
    async def _initialize(self):
        """Provider-specific initialization logic."""
        pass

    async def cleanup(self):
        """Cleanup provider resources."""
        if self._initialized:
            await self._cleanup()
            self._initialized = False

    @abstractmethod
    async def _cleanup(self):
        """Provider-specific cleanup logic."""
        pass

    @abstractmethod
    def get_provider_type(self) -> str:
        """Get the provider type identifier."""
        pass

    @abstractmethod
    def validate_config(self, config: dict):
        """Validate provider configuration."""
        pass

    async def translate(
        self, text: str, source_lang: str, target_lang: str, **kwargs
    ) -> str:
        """Translate text from source language to target language."""
        if not self._initialized:
            raise ProviderError("Provider not initialized")

        for attempt in range(self.retry_count):
            try:
                await self.rate_limiter.acquire()
                return await self._translate(text, source_lang, target_lang, **kwargs)
            except RateLimitError:
                if attempt == self.retry_count - 1:
                    raise
                await asyncio.sleep(self.retry_delay)

    @abstractmethod
    async def _translate(
        self, text: str, source_lang: str, target_lang: str, **kwargs
    ) -> str:
        """Provider-specific translation implementation."""
        pass
