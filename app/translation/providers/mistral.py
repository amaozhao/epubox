"""Mistral translation provider."""

import asyncio
import html

import tiktoken
from mistralai import Mistral, models
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.logging import get_logger
from app.db.models import LimitType
from app.db.models import TranslationProvider as TranslationProviderModel
from app.translation.errors import ConfigurationError, TranslationError
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
        self.model = self.provider_model.model or "mistral-large-latest"
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
            logger.warning(
                "Translation request exceeds limit",
                request_length=token_count,
                limit=self.provider_model.limit_value,
            )

        # 构建提示内容
        prompt = (
            f"Translate the following HTML from {source_lang} to {target_lang}.\n\n"
            "Rules:\n"
            "1. Keep ALL HTML tags unchanged\n"
            "2. Only translate text between tags\n"
            "3. DO NOT translate or modify these special markers:\n"
            "   - Placeholder markers: {1}, {2}, {3}, etc.\n"
            "   - Inline tag markers: ‹1›, ‹/1›, ‹2›, ‹/2›, etc.\n"
            "   - Skip tag placeholders: †1†, †2†, etc.\n"
            "4. Return ONLY the translated HTML\n\n"
            f"HTML:\n{text}"
        )

        messages = [
            models.SystemMessage(
                content=(
                    "You are an HTML-aware translation assistant. Your primary responsibility is to "
                    "translate text while perfectly preserving all HTML tags and their structure. "
                    "Never modify HTML tags or their attributes. Always maintain the exact count and "
                    "position of tags."
                )
            ),
            models.UserMessage(content=prompt),
        ]

        response = await self.client.chat.complete_async(
            model=self.model,
            messages=messages,
            temperature=0.1,
            **kwargs,
        )
        result = response.choices[0].message.content.strip()  # type: ignore

        # 记录翻译结果
        logger.info(
            "Translation response",
            # request_text=text,
            request_length=token_count,
            response_length=len(result),
            response=result[:400],
        )

        return result

    @retry(
        stop=stop_after_attempt(10),
        wait=wait_exponential(multiplier=2, min=10, max=30),
        before_sleep=log_retry_attempt,
        # retry=retry_if_exception_type(models.SDKError),
    )
    async def translate(
        self, text: str, source_lang: str, target_lang: str, **kwargs
    ) -> str:
        """Translate text with rate limiting and concurrency control."""
        async with self._semaphore:  # 使用信号量控制并发
            # 确保距离上次请求至少有1秒
            current_time = asyncio.get_event_loop().time()
            time_since_last_request = current_time - self._last_request_time
            if time_since_last_request < 3:
                await asyncio.sleep(3 - time_since_last_request)

            self.__class__._last_request_time = asyncio.get_event_loop().time()

            try:
                return await self._translate(text, source_lang, target_lang, **kwargs)
            except Exception as e:
                raise TranslationError(f"Mistral Translation failed: {str(e)}")
