"""OpenAI翻译服务实现"""

import asyncio
import logging
import random
from typing import Optional

from openai import AsyncOpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .translation_service import TranslationError, TranslationService


class OpenAITranslationError(TranslationError):
    """OpenAI翻译服务特定错误"""

    pass


class OpenAITranslationService(TranslationService):
    """OpenAI翻译服务实现"""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-3.5-turbo",
        max_retries: int = 3,
        min_wait: float = 1,
        max_wait: float = 10,
        timeout: float = 30,
    ):
        """
        初始化OpenAI翻译服务

        Args:
            api_key: OpenAI API密钥
            model: 使用的模型名称
            max_retries: 最大重试次数
            min_wait: 重试最小等待时间（秒）
            max_wait: 重试最大等待时间（秒）
            timeout: 翻译请求超时时间（秒）
        """
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.max_retries = max_retries
        self.min_wait = min_wait
        self.max_wait = max_wait
        self.timeout = timeout
        self._token_limit = 4096  # GPT-3.5的上下文窗口大小

    @retry(
        retry=retry_if_exception_type((OpenAITranslationError, asyncio.TimeoutError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
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
            OpenAITranslationError: 翻译失败
        """
        if not text.strip():
            return text

        try:
            # 添加随机延迟避免请求过于频繁
            await asyncio.sleep(random.uniform(0.1, 0.5))

            # 设置超时
            async with asyncio.timeout(self.timeout):
                # 构建系统提示和用户提示
                system_prompt = (
                    f"You are a professional translator. Translate the following text "
                    f"from {source_lang} to {target_lang}. Preserve the original "
                    f"formatting and structure. Only output the translated text, "
                    f"without any explanations or notes."
                )

                # 调用OpenAI API
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": text},
                    ],
                    temperature=0.3,  # 降低随机性以保持翻译准确
                    timeout=self.timeout,
                )

                translated = response.choices[0].message.content.strip()
                if not translated:
                    raise OpenAITranslationError("Empty translation result")

                return translated

        except asyncio.TimeoutError:
            logging.error(
                f"Translation timeout: source_lang={source_lang}, "
                f"target_lang={target_lang}, text={text[:100]}..."
            )
            raise OpenAITranslationError("Translation request timed out")

        except Exception as e:
            logging.error(
                f"Translation failed: {str(e)}, source_lang={source_lang}, "
                f"target_lang={target_lang}, text={text[:100]}..."
            )
            raise OpenAITranslationError(f"Translation failed: {str(e)}")

    def get_token_limit(self) -> int:
        """获取翻译服务的token限制"""
        return self._token_limit
