"""Caiyun translation provider."""

import asyncio
import json
import re
import sre_compile
from typing import Optional

import httpx

from app.core.logging import get_logger
from app.db.models import LimitType
from app.db.models import TranslationProvider as TranslationProviderModel
from app.translation.errors import ConfigurationError, TranslationError
from app.translation.providers.base import TranslationProvider, log_retry_attempt

logger = get_logger(__name__)

CAIYUN_API = "https://api.interpreter.caiyunai.com/v1/translator"


class CaiyunProvider(TranslationProvider):
    """Caiyun translation provider implementation."""

    def __init__(self, provider_model: TranslationProviderModel):
        super().__init__(provider_model)
        if provider_model.limit_type != LimitType.CHARS:
            raise ValueError("Caiyun provider must use character-based limits")

        self.client: Optional[httpx.AsyncClient] = None
        self.api_key = provider_model.config.get("api_key")
        if not self.api_key:
            raise ConfigurationError("API key is required for Caiyun provider")

    def get_provider_type(self) -> str:
        """Get provider type identifier."""
        return "caiyun"

    def validate_config(self, config: dict) -> bool:
        """Validate provider configuration."""
        return bool(config.get("api_key"))

    async def _initialize(self):
        """Initialize httpx client."""
        if not self.client:
            headers = {
                "content-type": "application/json",
                "x-authorization": f"token {self.api_key}",
            }
            self.client = httpx.AsyncClient(headers=headers)

    async def _cleanup(self):
        """Cleanup httpx client resources."""
        if self.client:
            await self.client.aclose()
            self.client = None

    def _get_trans_type(self, source_lang: str, target_lang: str) -> str:
        """Get translation type based on source and target languages."""
        if target_lang.lower() == "zh":
            return "auto2zh"
        elif target_lang.lower() == "en":
            return "auto2en"
        elif target_lang.lower() == "ja":
            return "auto2ja"
        else:
            raise TranslationError(f"Unsupported target language: {target_lang}")

    async def _translate(
        self, text: str, source_lang: str, target_lang: str, **kwargs
    ) -> str:
        """Translate text using Caiyun API."""
        if not self.client:
            raise ConfigurationError("HTTP client not initialized")

        # 检查目标语言是否支持
        trans_type = self._get_trans_type(source_lang, target_lang)

        # 准备请求数据
        payload = {
            "source": text,
            "trans_type": trans_type,
            "request_id": "epubox",
            "detect": True,
        }

        try:
            response = await self.client.post(
                url=CAIYUN_API,
                json=payload,
                timeout=30.0,
            )
            response.raise_for_status()

            result = response.json()

            if "target" not in result:
                logger.error(
                    "Invalid response format from Caiyun",
                    response=result,
                )
                raise TranslationError(f"Invalid response format from Caiyun: {result}")

            translated_text = result["target"]

            # 还原占位符的格式
            translated_text = re.sub(r"‹(\d+)›", r"{\1}", translated_text)

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

        except Exception as e:
            logger.error(
                "Translation failed",
                error=str(e),
                text_preview=text[:100] + "..." if len(text) > 100 else text,
            )
            raise TranslationError(f"Translation failed: {str(e)}")
