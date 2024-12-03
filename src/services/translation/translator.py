"""Translation service interface and implementations."""

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx
import openai
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class TranslationProvider(str, Enum):
    """Translation service providers."""

    OPENAI = "openai"
    GOOGLE = "google"
    DEEPL = "deepl"
    MISTRAL = "mistral"


class TranslationError(Exception):
    """Base exception for translation errors."""

    pass


class TranslationService(ABC):
    """Abstract base class for translation services."""

    @abstractmethod
    async def translate_batch(
        self, texts: List[str], source_lang: str, target_lang: str
    ) -> List[str]:
        """Translate a batch of texts."""
        pass


class OpenAITranslator(TranslationService):
    """OpenAI-based translation service."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-3.5-turbo",
        temperature: float = 0.3,
        max_tokens: Optional[int] = None,
        max_retries: int = 5,
        min_seconds: int = 4,
        max_seconds: int = 10,
    ):
        """Initialize OpenAI translator."""
        try:
            self.api_key = api_key
            self.model = model
            self.temperature = temperature
            self.max_tokens = max_tokens
            self.max_retries = max_retries
            self.min_seconds = min_seconds
            self.max_seconds = max_seconds
            logging.info(f"Initialized OpenAI translator with model: {model}")
        except Exception as e:
            raise TranslationError(f"Failed to initialize OpenAI translator: {str(e)}")

    @retry(
        retry=retry_if_exception_type((openai.RateLimitError, openai.APIError)),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        reraise=True,
    )
    async def _translate_batch(self, batch_text: str, system_prompt: str) -> str:
        """Translate a single batch of text with retry logic."""
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self.api_key)
        response = await client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": batch_text},
            ],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        return response.choices[0].message.content.strip()

    async def translate_batch(
        self, texts: List[str], source_lang: str, target_lang: str
    ) -> List[str]:
        """Translate texts using OpenAI API with rate limiting."""
        try:
            # 构建提示词
            system_prompt = (
                f"You are a professional translator. "
                f"Translate the following text from {source_lang} to {target_lang}. "
                f"Maintain the original meaning and style. "
                f"Return only the translated text, without any explanations."
            )

            # 批量翻译，每次最多处理 10 个文本
            batch_size = 10
            results = []

            for i in range(0, len(texts), batch_size):
                batch = texts[i : i + batch_size]
                batch_text = "\n---\n".join(batch)

                # Add delay between batches to avoid rate limits
                if i > 0:
                    await asyncio.sleep(1)

                translated = await self._translate_batch(batch_text, system_prompt)
                translated_texts = translated.split("\n---\n")
                results.extend(translated_texts)

            return results

        except Exception as e:
            raise TranslationError(f"OpenAI translation failed: {str(e)}")


class GoogleTranslator(TranslationService):
    """Google Cloud Translation service."""

    def __init__(self, api_key: str, project_id: str):
        """Initialize Google translator."""
        try:
            self.api_key = api_key
            self.project_id = project_id
            self.base_url = "https://translation.googleapis.com/v3"
            logging.info(f"Initialized Google translator with project ID: {project_id}")
        except Exception as e:
            raise TranslationError(f"Failed to initialize Google translator: {str(e)}")

    async def translate_batch(
        self, texts: List[str], source_lang: str, target_lang: str
    ) -> List[str]:
        """Translate texts using Google Cloud Translation API."""
        try:
            url = f"{self.base_url}/projects/{self.project_id}/locations/global:translateText"

            # 准备请求数据
            data = {
                "contents": texts,
                "sourceLanguageCode": source_lang,
                "targetLanguageCode": target_lang,
                "mimeType": "text/plain",
            }

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=data, headers=headers)
                response.raise_for_status()

                result = response.json()
                return [
                    translation["translatedText"]
                    for translation in result["translations"]
                ]

        except Exception as e:
            raise TranslationError(f"Google translation failed: {str(e)}")


class MistralTranslator(TranslationService):
    """Mistral AI-based translation service."""

    def __init__(
        self,
        api_key: str,
        model: str = "mistral-medium",
        temperature: float = 0.3,
        max_tokens: Optional[int] = None,
        max_retries: int = 5,
        min_seconds: int = 4,
        max_seconds: int = 10,
    ):
        """Initialize Mistral translator."""
        try:
            self.api_key = api_key
            self.model = model
            self.temperature = temperature
            self.max_tokens = max_tokens
            self.max_retries = max_retries
            self.min_seconds = min_seconds
            self.max_seconds = max_seconds
            self.base_url = "https://api.mistral.ai/v1"
            logging.info(f"Initialized Mistral translator with model: {model}")
        except Exception as e:
            raise TranslationError(f"Failed to initialize Mistral translator: {str(e)}")

    @retry(
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        reraise=True,
    )
    async def _translate_batch(self, batch_text: str, system_prompt: str) -> str:
        """Translate a single batch of text with retry logic."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        data = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": batch_text},
            ],
            "temperature": self.temperature,
        }
        if self.max_tokens:
            data["max_tokens"] = self.max_tokens

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/chat/completions", headers=headers, json=data
            )
            response.raise_for_status()
            result = response.json()
            return result["choices"][0]["message"]["content"].strip()

    async def translate_batch(
        self, texts: List[str], source_lang: str, target_lang: str
    ) -> List[str]:
        """Translate texts using Mistral AI API with rate limiting."""
        try:
            system_prompt = (
                f"You are a professional translator. "
                f"Translate the following text from {source_lang} to {target_lang}. "
                f"Maintain the original meaning and style. "
                f"Return only the translated text, without any explanations."
            )

            # Process in smaller batches to avoid rate limits
            batch_size = 5  # Smaller batch size for Mistral
            results = []

            for i in range(0, len(texts), batch_size):
                batch = texts[i : i + batch_size]
                batch_text = "\n---\n".join(batch)

                # Add delay between batches
                if i > 0:
                    await asyncio.sleep(2)  # Longer delay for Mistral

                translated = await self._translate_batch(batch_text, system_prompt)
                translated_texts = translated.split("\n---\n")
                results.extend(translated_texts)

            return results

        except Exception as e:
            raise TranslationError(f"Mistral translation failed: {str(e)}")


class DeepLTranslator(TranslationService):
    """DeepL-based translation service."""

    def __init__(self, api_key: str, is_pro: bool = False):
        """Initialize DeepL translator."""
        try:
            self.api_key = api_key
            self.is_pro = is_pro
            self.base_url = "https://api.deepl.com/v2/translate"
            logging.info(f"Initialized DeepL translator")
        except Exception as e:
            raise TranslationError(f"Failed to initialize DeepL translator: {str(e)}")

    async def translate_batch(
        self, texts: List[str], source_lang: str, target_lang: str
    ) -> List[str]:
        """Translate texts using DeepL API."""
        try:
            headers = {
                "Authorization": f"DeepL-Auth-Key {self.api_key}",
                "Content-Type": "application/json",
            }

            data = {
                "text": texts,
                "source_lang": source_lang.upper(),
                "target_lang": target_lang.upper(),
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(self.base_url, json=data, headers=headers)
                response.raise_for_status()

                result = response.json()
                return [translation["text"] for translation in result["translations"]]

        except Exception as e:
            raise TranslationError(f"DeepL translation failed: {str(e)}")


def create_translator(
    provider: TranslationProvider, api_key: str, **kwargs
) -> TranslationService:
    """Create a translator instance based on provider."""
    try:
        if provider == TranslationProvider.OPENAI:
            return OpenAITranslator(
                api_key=api_key,
                model=kwargs.get("model", "gpt-3.5-turbo"),
                temperature=kwargs.get("temperature", 0.3),
                max_tokens=kwargs.get("max_tokens"),
            )
        elif provider == TranslationProvider.GOOGLE:
            return GoogleTranslator(api_key=api_key, project_id=kwargs["project_id"])
        elif provider == TranslationProvider.MISTRAL:
            return MistralTranslator(
                api_key=api_key,
                model=kwargs.get("model", "mistral-medium"),
                temperature=kwargs.get("temperature", 0.3),
                max_tokens=kwargs.get("max_tokens"),
            )
        elif provider == TranslationProvider.DEEPL:
            return DeepLTranslator(api_key=api_key, is_pro=kwargs.get("is_pro", False))
        else:
            raise TranslationError(f"Unsupported translation provider: {provider}")
    except Exception as e:
        raise TranslationError(f"Failed to create translator: {str(e)}")
