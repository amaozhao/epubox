"""Groq translation provider."""

import asyncio

import tiktoken
from groq import AsyncGroq
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.logging import get_logger
from app.db.models import LimitType
from app.db.models import TranslationProvider as TranslationProviderModel
from app.translation.errors import ConfigurationError, TranslationError
from app.translation.providers.base import TranslationProvider, log_retry_attempt

logger = get_logger(__name__)


class GroqProvider(TranslationProvider):
    """Groq translation provider implementation."""

    # 类级别的信号量，限制并发为30（每分钟30个请求）
    _semaphore = asyncio.Semaphore(30)
    # 类级别的上次请求时间记录
    _last_request_time = 0
    # 每分钟token限制
    _tokens_per_minute = 15000
    # 当前分钟已使用的token数
    _current_minute_tokens = 0
    # 上次重置token计数的时间
    _last_token_reset_time = 0

    def __init__(self, provider_model: TranslationProviderModel):
        super().__init__(provider_model)
        if provider_model.limit_type != LimitType.TOKENS:
            raise ValueError("Groq provider must use token-based limits")

        self.api_key = self.config.get("api_key")
        self.model = self.provider_model.model or "llama3-8b-8192"
        self.client = AsyncGroq(api_key=self.api_key)
        self.tokenizer = tiktoken.get_encoding("cl100k_base")

    def get_provider_type(self) -> str:
        """Get provider type identifier."""
        return "groq"

    def validate_config(self, config: dict) -> bool:
        """Validate provider configuration."""
        if not config.get("api_key"):
            raise ConfigurationError("Groq API key is required")
        return True

    def _count_tokens(self, text: str) -> int:
        """Count tokens in text using tiktoken."""
        if not self.tokenizer:
            raise ConfigurationError("Tokenizer not initialized")
        return len(self.tokenizer.encode(text))

    async def _translate(
        self, text: str, source_lang: str, target_lang: str, **kwargs
    ) -> str:
        """Translate text using Groq API."""
        if not self.client:
            raise ConfigurationError("Groq client not initialized")

        token_count = self._count_tokens(text)
        if token_count > self.provider_model.limit_value:
            raise TranslationError(
                f"Text length ({token_count} tokens) exceeds maximum allowed ({self.provider_model.limit_value} tokens)"
            )

        # 构建提示内容
        prompt = (
            f"You are a translator. Translate the following text from {source_lang} to {target_lang}.\n"
            "Important rules:\n"
            "1. Preserve all HTML tags exactly as they appear\n"
            "2. Do not add any formatting or markup\n"
            "3. Do not add any explanations or notes\n"
            "4. Keep all proper names in their original form\n"
            "5. Return only the translated text\n\n"
            f"Text to translate:\n{text}"
        )

        messages = [
            {"role": "system", "content": "You are a helpful translation assistant."},
            {"role": "user", "content": prompt},
        ]

        try:
            response = await self.client.chat.completions.create(  # type: ignore
                messages=messages,  # type: ignore
                model=self.model,
                temperature=0.1,
                max_tokens=self.provider_model.limit_value,
                **kwargs,
            )
            result = response.choices[0].message.content.strip()

            # 记录翻译结果
            logger.info(
                "Translation response",
                request_length=token_count,
                response_length=len(result),
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
        stop=stop_after_attempt(10),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        before_sleep=log_retry_attempt,
    )
    async def translate(
        self, text: str, source_lang: str, target_lang: str, **kwargs
    ) -> str:
        """Translate text with rate limiting and concurrency control."""
        async with self._semaphore:  # 使用信号量控制并发
            current_time = asyncio.get_event_loop().time()

            # 检查是否需要重置当前分钟的token计数
            if current_time - self._last_token_reset_time >= 60:
                self.__class__._current_minute_tokens = 0
                self.__class__._last_token_reset_time = current_time

            # 计算当前请求的token数
            token_count = self._count_tokens(text)

            # 检查是否超过每分钟token限制
            if self._current_minute_tokens + token_count > self._tokens_per_minute:
                wait_time = 60 - (current_time - self._last_token_reset_time)
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                self.__class__._current_minute_tokens = 0
                self.__class__._last_token_reset_time = asyncio.get_event_loop().time()

            # 更新token使用计数
            self.__class__._current_minute_tokens += token_count

            # 确保距离上次请求至少有2秒（考虑到每分钟30个请求的限制）
            time_since_last_request = current_time - self._last_request_time
            if time_since_last_request < 2:
                await asyncio.sleep(2 - time_since_last_request)

            self.__class__._last_request_time = asyncio.get_event_loop().time()

            try:
                return await self._translate(text, source_lang, target_lang, **kwargs)
            except Exception as e:
                raise TranslationError(f"Groq Translation failed: {str(e)}")
