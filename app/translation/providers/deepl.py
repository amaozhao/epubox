"""DeepL translation provider."""

import asyncio
import json
import random
import time
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.logging import get_logger
from app.db.models import LimitType
from app.db.models import TranslationProvider as TranslationProviderModel
from app.translation.errors import ConfigurationError, TranslationError
from app.translation.providers.base import TranslationProvider, log_retry_attempt

logger = get_logger(__name__)

DEEPL_API = "https://www2.deepl.com/jsonrpc"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "x-app-os-name": "iOS",
    "x-app-os-version": "16.3.0",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "x-app-device": "iPhone13,2",
    "User-Agent": "DeepL-iOS/2.9.1 iOS 16.3.0 (iPhone13,2)",
    "x-app-build": "510265",
    "x-app-version": "2.9.1",
    "Connection": "keep-alive",
}


class DeepLProvider(TranslationProvider):
    """DeepL translation provider implementation."""

    # 类级别的信号量，限制并发为1
    _semaphore = asyncio.Semaphore(1)
    # 类级别的上次请求时间记录
    _last_request_time = 0

    def __init__(self, provider_model: TranslationProviderModel):
        super().__init__(provider_model)
        if provider_model.limit_type != LimitType.CHARS:
            raise ValueError("DeepL provider must use character-based limits")

        self.client: Optional[httpx.AsyncClient] = None

    def get_provider_type(self) -> str:
        """Get provider type identifier."""
        return "deepl"

    def validate_config(self, config: dict) -> bool:
        """Validate provider configuration."""
        return True

    @staticmethod
    def _get_i_count(text: str) -> int:
        """Get count of 'i' characters in text."""
        return text.count("i")

    @staticmethod
    def _get_random_number() -> int:
        """Generate random number for request ID."""
        random.seed(time.time())
        num = random.randint(8300000, 8399998)
        return num * 1000

    @staticmethod
    def _get_timestamp(i_count: int) -> int:
        """Generate timestamp based on i_count."""
        ts = int(time.time() * 1000)
        if i_count == 0:
            return ts
        i_count += 1
        return ts - ts % i_count + i_count

    async def _initialize(self):
        """Initialize httpx client."""
        if not self.client:
            self.client = httpx.AsyncClient(headers=HEADERS)

    async def _cleanup(self):
        """Cleanup httpx client resources."""
        if self.client:
            await self.client.aclose()
            self.client = None

    async def _translate(
        self, text: str, source_lang: str, target_lang: str, **kwargs
    ) -> str:
        """Translate text using DeepL API."""
        if not self.client:
            raise ConfigurationError("HTTP client not initialized")

        # 准备请求数据
        i_count = self._get_i_count(text)
        request_id = self._get_random_number()

        post_data = {
            "jsonrpc": "2.0",
            "method": "LMT_handle_texts",
            "id": request_id,
            "params": {
                "texts": [{"text": text, "requestAlternatives": 0}],
                "splitting": "newlines",
                "lang": {
                    "source_lang_user_selected": source_lang,
                    "target_lang": target_lang,
                },
                "timestamp": self._get_timestamp(i_count),
                "commonJobParams": {
                    "wasSpoken": False,
                    "transcribe_as": "",
                },
            },
        }

        # 特殊处理 JSON 字符串
        post_data_str = json.dumps(post_data, ensure_ascii=False)
        if (request_id + 5) % 29 == 0 or (request_id + 3) % 13 == 0:
            post_data_str = post_data_str.replace('"method":"', '"method" : "', -1)
        else:
            post_data_str = post_data_str.replace('"method":"', '"method": "', -1)

        try:
            response = await self.client.post(
                url=DEEPL_API,
                content=post_data_str,
                timeout=30.0,
            )
            response.raise_for_status()

            if response.status_code == 429:
                raise TranslationError(
                    "Too many requests, your IP has been blocked by DeepL temporarily"
                )

            result = response.json()
            try:
                translated_text = result["result"]["texts"][0]["text"]
            except (KeyError, IndexError) as e:
                logger.error(
                    "Invalid response format from DeepL",
                    error=str(e),
                    response=result,
                )
                raise TranslationError(f"Invalid response format from DeepL: {result}")

            logger.info(
                "Translation successful",
                text_preview=text[:100] + "..." if len(text) > 100 else text,
                result_preview=(
                    translated_text[:100] + "..."
                    if len(translated_text) > 100
                    else translated_text
                ),
            )

            return translated_text

        except httpx.HTTPError as e:
            logger.error(
                "Translation request failed",
                error=str(e),
                text_preview=text[:100] + "..." if len(text) > 100 else text,
            )
            raise TranslationError(f"Translation failed: {str(e)}")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=10, max=30),
        before_sleep=log_retry_attempt,
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

            self.__class__._last_request_time = asyncio.get_event_loop().time()

            try:
                return await self._translate(text, source_lang, target_lang, **kwargs)
            except Exception as e:
                raise TranslationError(f"DeepL Translation failed: {str(e)}")
