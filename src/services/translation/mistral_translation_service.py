"""Mistral翻译服务实现"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from mistralai import Mistral
from mistralai.models import UserMessage
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.infrastructure.config import settings

from .translation_service import TranslationError, TranslationService


@dataclass
class TokenBucket:
    """令牌桶速率限制器"""

    capacity: int  # 桶的容量（最大token数）
    rate: float  # 每秒补充的token数
    tokens: float = field(init=False)  # 当前可用的token数
    last_update: float = field(init=False)  # 上次更新时间

    def __post_init__(self):
        self.tokens = self.capacity
        self.last_update = time.time()

    def consume(self, tokens: int) -> bool:
        """
        尝试消费指定数量的token

        Args:
            tokens: 需要消费的token数

        Returns:
            是否成功消费
        """
        now = time.time()
        # 计算从上次更新到现在新增的token
        elapsed = now - self.last_update
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_update = now

        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    async def acquire(self, tokens: int):
        """
        等待直到可以获取指定数量的token

        Args:
            tokens: 需要的token数
        """
        while not self.consume(tokens):
            # 计算需要等待的时间
            needed = tokens - self.tokens
            wait_time = needed / self.rate
            await asyncio.sleep(wait_time)


class MistralTranslationError(TranslationError):
    """Mistral翻译服务特定错误"""

    pass


class RateLimitError(MistralTranslationError):
    """速率限制错误"""

    pass


class MistralTranslationService(TranslationService):
    """Mistral翻译服务实现"""

    # 速率限制错误关键词
    RATE_LIMIT_KEYWORDS = [
        "rate limit",
        "too many requests",
        "resource_exhausted",
        "quota exceeded",
        "try again",
        "please wait",
        "exceeded",
    ]

    def __init__(
        self,
        model: str = "mistral-tiny",
        max_retries: int = 5,
        min_wait: int = 1,
        max_wait: int = 60,
        timeout: int = 30,
        is_testing: bool = False,
    ):
        """
        初始化Mistral翻译服务

        Args:
            model: 使用的模型名称
            max_retries: 最大重试次数
            min_wait: 最小等待时间（秒）
            max_wait: 最大等待时间（秒）
            timeout: 超时时间（秒）
            is_testing: 是否处于测试模式
        """
        self.model = model
        self.max_retries = max_retries
        self.min_wait = min_wait
        self.max_wait = max_wait
        self.timeout = timeout
        self.is_testing = is_testing
        self.client = Mistral(api_key=settings.MISTRAL_API_KEY)

        # Mistral的上下文窗口大小是4096 tokens
        self._token_limit = 4096

        # 计算单个文本块的最大大小
        # 1. 提示词大约占用50个tokens
        # 2. 预留一半空间给翻译结果（因为某些语言可能会导致文本膨胀）
        # 3. 保守估计：1个中文字符约等于1.5个token
        prompt_tokens = 50
        self.max_text_length = int((self._token_limit - prompt_tokens) / 3)

        # 初始化速率限制器
        # 1. RPS限制：每秒1个请求
        self._request_limiter = asyncio.Semaphore(1)
        # 2. Token限制：每分钟500,000 tokens
        self._token_bucket = TokenBucket(
            capacity=500_000,  # 最大容量500k tokens
            rate=500_000 / 60,  # 每秒补充的token数（约8333 tokens/s）
        )

    def _is_rate_limit_error(self, error_msg: str) -> bool:
        """检查是否为速率限制错误"""
        error_msg = error_msg.lower()
        return any(keyword in error_msg for keyword in self.RATE_LIMIT_KEYWORDS)

    def _estimate_tokens(self, text: str) -> int:
        """
        估算文本的token数量

        Args:
            text: 输入文本

        Returns:
            估算的token数量
        """
        # 保守估计：
        # - 英文：每个单词约1.3个token
        # - 中文：每个字符约1.5个token
        # 这里我们使用较保守的1.5作为系数
        return int(len(text) * 1.5)

    def get_token_limit(self) -> int:
        """
        获取翻译服务的token限制

        Returns:
            单次翻译请求的最大token数
        """
        return self._token_limit

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
            MistralTranslationError: 翻译失败
        """
        # 估算token数量（包括输入和预期的输出）
        estimated_tokens = self._estimate_tokens(text) * 2  # 输入和输出

        # 等待token bucket有足够的token
        await self._token_bucket.acquire(estimated_tokens)

        # 使用信号量控制RPS
        async with self._request_limiter:
            try:
                # 构建提示词
                prompt = (
                    f"Translate the following text from {source_lang} to {target_lang}. "
                    f"Only return the translated text, no explanations: {text}"
                )

                # 使用 complete_async 方法
                chat_response = await self.client.chat.complete_async(
                    model=self.model,
                    messages=[UserMessage(content=prompt)],
                )

                # 强制等待1秒以遵守RPS限制
                await asyncio.sleep(1)

                # 提取翻译结果
                if not chat_response or not chat_response.choices:
                    raise MistralTranslationError("No translation result")

                translated_text = chat_response.choices[0].message.content.strip()
                if not translated_text:
                    raise MistralTranslationError("Empty translation result")

                return translated_text

            except Exception as e:
                error_msg = str(e)
                if self._is_rate_limit_error(error_msg):
                    raise RateLimitError(f"Rate limit exceeded: {error_msg}")
                raise MistralTranslationError(f"Translation failed: {error_msg}")
