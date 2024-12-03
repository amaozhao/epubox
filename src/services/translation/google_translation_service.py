"""Google翻译服务实现"""

import asyncio
import logging
from typing import Optional

import translators as ts

from src.infrastructure.config import settings

from .translation_service import TranslationError, TranslationService


class GoogleTranslationError(TranslationError):
    """Google翻译服务特定错误"""

    pass


class RateLimitError(GoogleTranslationError):
    """速率限制错误"""

    pass


class GoogleTranslationService(TranslationService):
    """Google翻译服务实现"""

    def __init__(
        self,
        max_retries: int = 5,
        min_wait: int = 1,
        max_wait: int = 60,
        timeout: int = 30,
        is_testing: bool = False,
    ):
        """
        初始化Google翻译服务

        Args:
            max_retries: 最大重试次数
            min_wait: 最小等待时间（秒）
            max_wait: 最大等待时间（秒）
            timeout: 超时时间（秒）
            is_testing: 是否处于测试模式
        """
        self.max_retries = max_retries
        self.min_wait = min_wait
        self.max_wait = max_wait
        self.timeout = timeout
        self.is_testing = is_testing

        # Google Translate API 的字符限制是5000
        self.max_text_length = 5000

    async def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        """
        翻译文本

        Args:
            text: 要翻译的文本
            source_lang: 源语言代码 (如 'zh', 'en')
            target_lang: 目标语言代码

        Returns:
            翻译后的文本

        Raises:
            GoogleTranslationError: 翻译失败
        """
        if not text.strip():
            return text

        try:
            # 使用 translators 库的 translate_text 方法
            translated = ts.translate_text(
                text,
                translator="google",
                from_language=source_lang,
                to_language=target_lang,
                timeout=self.timeout,
            )

            if not translated or not translated.strip():
                raise GoogleTranslationError("Empty translation result")

            return translated

        except Exception as e:
            error_msg = str(e).lower()

            # 处理速率限制错误
            if any(
                msg in error_msg
                for msg in [
                    "rate limit",
                    "too many requests",
                    "resource exhausted",
                    "quota exceeded",
                    "please wait",
                ]
            ):
                raise RateLimitError(str(e))

            raise GoogleTranslationError(f"Translation failed: {e}")

    def get_token_limit(self) -> int:
        """获取token限制"""
        return self.max_text_length
