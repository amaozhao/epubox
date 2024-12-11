"""Mistral translation provider."""

import asyncio
from typing import Dict

import tiktoken
from mistralai import Mistral, models
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from app.core.logging import get_logger
from app.translation.errors import ConfigurationError, TranslationError
from app.translation.models import LimitType
from app.translation.models import TranslationProvider as TranslationProviderModel
from app.translation.providers.base import TranslationProvider, log_retry_attempt

logger = get_logger(__name__)


class MistralProvider(TranslationProvider):
    """Mistral translation provider implementation."""

    # 类级别的信号量，限制并发为1
    _semaphore = asyncio.Semaphore(1)
    # 类级别的上次请求时间记录
    _last_request_time = 0

    def __init__(self, provider_model: TranslationProviderModel):
        super().__init__(provider_model)
        if provider_model.limit_type != LimitType.TOKENS:
            raise ValueError("Mistral provider must use token-based limits")

        self.api_key = self.config.get("api_key")
        self.model = self.config.get("model", "mistral-large-latest")
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

        token_count = self._count_tokens(text)
        if token_count > self.provider_model.limit_value:
            raise TranslationError(
                f"Text length ({token_count} tokens) exceeds maximum allowed ({self.provider_model.limit_value} tokens)"
            )

        # 构建提示内容
        prompt = f"Translate from {source_lang} to {target_lang}. Keep HTML tags and names in original English. Return only the translation.\n\n{text}"

        messages = [models.UserMessage(content=prompt)]

        try:
            response = await self.client.chat.complete_async(
                model=self.model,
                messages=messages,
                temperature=0.7,
                **kwargs,
            )
            result = response.choices[0].message.content.strip()

            # 记录翻译结果
            logger.info(
                "Translation response",
                response_text=result,
                response_length=len(result),
                request_text=text,
                request_length=len(text),
                request_preview=text[:200] + "..." if len(text) > 200 else text,
            )

            return result
        except Exception as e:
            logger.error(
                "Translation request failed",
                error=str(e),
                text_preview=text[:100] + "..." if len(text) > 100 else text,
            )
            raise TranslationError(f"Translation failed: {str(e)}")

    @retry(
        wait=wait_fixed(2),  # 固定等待2秒
        stop=stop_after_attempt(3),  # 最多重试3次
        before_sleep=log_retry_attempt,
        retry=retry_if_exception_type(TranslationError),
    )
    async def translate(
        self, text: str, source_lang: str, target_lang: str, **kwargs
    ) -> str:
        """Translate text with rate limiting and concurrency control."""
        async with self._semaphore:  # 使用信号量控制并发
            # 确保距离上次请求至少有1秒
            current_time = asyncio.get_event_loop().time()
            time_since_last_request = current_time - self._last_request_time
            if time_since_last_request < 1:
                await asyncio.sleep(1 - time_since_last_request)

            try:
                result = await super().translate(
                    text, source_lang, target_lang, **kwargs
                )
                self.__class__._last_request_time = asyncio.get_event_loop().time()
                return result
            except Exception as e:
                self.__class__._last_request_time = asyncio.get_event_loop().time()
                raise e
