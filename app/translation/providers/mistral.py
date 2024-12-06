"""Mistral translation provider."""

import asyncio
import time
from datetime import datetime, timedelta
from typing import Optional

import tiktoken
from bs4 import BeautifulSoup
from mistralai import Mistral, models
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..errors import ConfigurationError, RateLimitError, TranslationError
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
        self.max_retries = self.config.get("max_retries", 3)
        self.retry_delay = self.config.get("retry_delay", 5)  # seconds
        self.client = Mistral(api_key=self.api_key)
        self.tokenizer = self._initialize_tokenizer()

        # 限流状态
        self._rate_limit_lock = asyncio.Lock()
        self._last_error_time = None
        self._rate_limit_reset = None

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

    async def check_rate_limit(self):
        """Check if we're currently rate limited by Mistral API."""
        async with self._rate_limit_lock:
            now = datetime.now()

            # 如果之前遇到过限流
            if self._rate_limit_reset and now < self._rate_limit_reset:
                wait_minutes = (self._rate_limit_reset - now).seconds // 60
                raise RateLimitError(
                    f"Rate limit exceeded. Please try again in {wait_minutes} minutes"
                )

    async def _handle_rate_limit(self):
        """Handle rate limit error by setting a reset time."""
        async with self._rate_limit_lock:
            self._last_error_time = datetime.now()
            # Mistral API 的限流是按小时计算的
            self._rate_limit_reset = self._last_error_time + timedelta(hours=1)

    async def _make_request(self, messages: list, **kwargs) -> str:
        """Make API request with retry logic."""
        await self.check_rate_limit()
        retries = 0
        last_error = None

        while retries <= self.max_retries:
            try:
                print(
                    f"\nMaking API request (attempt {retries + 1}/{self.max_retries + 1}):"
                )
                print(f"Model: {self.model}")

                response = await self.client.chat.complete_async(
                    model=self.model,
                    messages=messages,
                    **kwargs,
                )

                print(f"API Response: {response}")
                return response.choices[0].message.content.strip()

            except models.SDKError as e:
                print(f"\nSDK Error occurred:")
                print(f"Status code: {e.status_code}")
                print(f"Error message: {e.message}")

                if "rate limit exceeded" in str(e).lower():
                    await self._handle_rate_limit()
                    retries += 1
                    if retries <= self.max_retries:
                        wait_time = self.retry_delay * (2 ** (retries - 1))
                        print(
                            f"Rate limit hit, waiting {wait_time} seconds before retry {retries}/{self.max_retries}"
                        )
                        await asyncio.sleep(wait_time)
                        last_error = e
                        continue
                    raise RateLimitError(str(e))
                raise TranslationError(f"Mistral API error: {e.message}")

            except models.HTTPValidationError as e:
                raise TranslationError(f"Invalid request: {str(e)}")

            except Exception as e:
                print(f"\nUnexpected error: {str(e)}")
                print(f"Error type: {type(e)}")
                raise TranslationError(f"Translation failed: {str(e)}")

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
            # 构建提示，要求保留HTML标签和占位符
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

            translated = await self._make_request(messages, **kwargs)

            print(f"\nDebug - Translation result:")
            print(f"Original text: {text}")
            print(f"Translated text: {translated}")

            return translated.strip()

        except RateLimitError:
            # 直接向上传递RateLimitError
            raise
        except Exception as e:
            raise TranslationError(f"Translation failed: {str(e)}")
