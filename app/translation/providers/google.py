"""Google Translation provider using free API."""

import re
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.logging import get_logger
from app.db.models import TranslationProvider as TranslationProviderModel
from app.translation.errors import ConfigurationError, TranslationError
from app.translation.providers.base import TranslationProvider, log_retry_attempt

logger = get_logger(__name__)


class GoogleProvider(TranslationProvider):
    """Google Translation provider implementation using free API."""

    def __init__(self, provider_model: TranslationProviderModel):
        super().__init__(provider_model)
        self.api_url = "https://translate.google.com/translate_a/single"
        self.params = {
            "client": "it",
            "dt": ["qca", "t", "rmt", "bd", "rms", "sos", "md", "gt", "ld", "ss", "ex"],
            "otf": "2",
            "dj": "1",
            "hl": "en",
            "ie": "UTF-8",
            "oe": "UTF-8",
            "sl": "auto",
        }
        self.headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "GoogleTranslate/6.29.59279 (iPhone; iOS 15.4; en; iPhone14,2)",
        }
        self.client: Optional[httpx.AsyncClient] = None

    def get_provider_type(self) -> str:
        """Get provider type identifier."""
        return "google"

    async def _initialize(self):
        """Initialize httpx client."""
        self.client = httpx.AsyncClient(headers=self.headers)

    async def _cleanup(self):
        """Cleanup httpx client resources."""
        if self.client:
            await self.client.aclose()
            self.client = None

    def validate_config(self, config: dict) -> bool:
        """Validate provider configuration."""
        return True  # No config needed for free API

    async def _translate(
        self, text: str, source_lang: str, target_lang: str, **kwargs
    ) -> str:
        """Translate text using Google Translate API."""
        if not self.client:
            raise ConfigurationError("HTTP client not initialized")

        # Google Translate API 使用不同的语言代码格式
        target_lang = target_lang.split("-")[0]  # 将 'en-US' 转换为 'en'

        # 设置目标语言
        params = {**self.params, "tl": target_lang}
        data = {"q": text}

        try:
            response = await self.client.post(
                self.api_url,
                params=params,
                data=data,
                timeout=10.0,
            )
            response.raise_for_status()
            result = response.json()

            translated_text = result["target"]

            # 清理文本
            translated_text = re.sub(r"\n{3,}", "\n\n", translated_text)
            translated_text = translated_text.replace("您", "你")
            translated_text = translated_text.replace("覆盖", "封面")
            translated_text = translated_text.replace("法学硕士", "LLM")

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
        wait=wait_exponential(multiplier=1, min=4, max=10),
        before_sleep=log_retry_attempt,
    )
    async def translate(
        self, text: str, source_lang: str, target_lang: str, **kwargs
    ) -> str:
        """Translate text with retry mechanism."""
        return await self._translate(text, source_lang, target_lang, **kwargs)
