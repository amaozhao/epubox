"""Mistral-based translator implementation."""

import asyncio
from typing import Any, Callable, Dict, List, Optional, TypeVar

from mistralai import Mistral
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .base import BaseTranslator

T = TypeVar("T")


class MistralTranslationError(Exception):
    """Custom exception for Mistral translation errors."""

    pass


class MistralTranslator(BaseTranslator):
    """Translator using Mistral's API."""

    RETRY_CONFIG = {
        "stop": stop_after_attempt(3),
        "wait": wait_exponential(multiplier=1, min=4, max=10),
        "retry": retry_if_exception_type((Exception, asyncio.TimeoutError)),
        "reraise": True,
    }
    BATCH_CHUNK_SIZE = 20
    MAX_CONCURRENT_REQUESTS = 5

    def __init__(
        self,
        api_key: str,
        source_lang: str,
        target_lang: str,
        model: str = "mistral-large-latest",
    ):
        """Initialize Mistral translator.

        Args:
            api_key: Mistral API key
            source_lang: Source language code
            target_lang: Target language code
            model: Mistral model to use (default: mistral-large-latest)
        """
        super().__init__(api_key, source_lang, target_lang)
        self.client = Mistral(api_key=api_key)
        self.model = model
        self._semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_REQUESTS)

    async def _make_request(
        self, text: str, process_response: Callable[[Any], T], task: str = "translate"
    ) -> T:
        """Make a request to Mistral API with retry logic.

        Args:
            text: Text to process
            process_response: Function to process the response
            task: Task type ("translate" or "detect")

        Returns:
            T: Processed response of type T

        Raises:
            MistralTranslationError: If request fails after all retries
        """

        @retry(**self.RETRY_CONFIG)
        async def _request():
            try:
                async with self._semaphore:
                    response = await self.client.chat.complete_async(
                        model=self.model,
                        messages=self._prepare_messages(text, task),
                        temperature=0.1,
                        max_tokens=1000,
                    )
                    return process_response(response)
            except asyncio.TimeoutError as e:
                raise MistralTranslationError(f"TimeoutError: {str(e)}")
            except Exception as e:
                raise MistralTranslationError(f"{e.__class__.__name__}: {str(e)}")

        return await _request()

    def _prepare_messages(self, text: str, task: str = "translate") -> List[Dict]:
        """Prepare messages for Mistral API request.

        Args:
            text: Text to process
            task: Task type ("translate" or "detect")

        Returns:
            List[Dict]: Prepared messages
        """
        if task == "translate":
            system_prompt = (
                f"You are a professional translator. Translate the following text "
                f"from {self.source_lang} to {self.target_lang}. "
                f"Only return the translated text, nothing else."
            )
        else:
            system_prompt = (
                "You are a language detection expert. "
                "Analyze the following text and return only the ISO 639-1 "
                "language code (2 letters). Return only the code, nothing else."
            )

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ]

    async def translate_text(self, text: str) -> str:
        """Translate text using Mistral.

        Args:
            text: Text to translate

        Returns:
            str: Translated text
        """

        def process_response(response):
            return response.choices[0].message["content"].strip()

        return await self._make_request(text, process_response, task="translate")

    async def translate_batch(self, texts: List[str]) -> List[str]:
        """Translate multiple texts using Mistral.

        Args:
            texts: List of texts to translate

        Returns:
            List[str]: List of translated texts
        """
        results = [""] * len(texts)
        chunks = [
            texts[i : i + self.BATCH_CHUNK_SIZE]
            for i in range(0, len(texts), self.BATCH_CHUNK_SIZE)
        ]

        for chunk_idx, chunk in enumerate(chunks):
            tasks = []
            start_idx = chunk_idx * self.BATCH_CHUNK_SIZE

            for i, text in enumerate(chunk):

                async def translate_text(text=text, i=i):
                    try:
                        result = await self.translate_text(text)
                        return i, result
                    except Exception:
                        return i, ""

                tasks.append(translate_text())

            chunk_results = await asyncio.gather(*tasks)
            for i, result in chunk_results:
                if i < len(chunk):
                    results[start_idx + i] = result

        return results

    async def detect_language(self, text: str) -> str:
        """Detect language of text using Mistral.

        Args:
            text: Text to detect language for

        Returns:
            str: ISO 639-1 language code
        """

        def process_response(response):
            return response.choices[0].message["content"].strip()

        return await self._make_request(text, process_response, task="detect")

    def get_supported_languages(self) -> List[str]:
        """Get list of supported languages.

        Returns:
            List[str]: List of supported language codes
        """
        return [
            "en",
            "es",
            "fr",
            "de",
            "it",
            "pt",
            "nl",
            "pl",
            "ru",
            "ja",
            "ko",
            "zh",
            "ar",
            "hi",
            "bn",
            "pa",
            "te",
            "ta",
            "ur",
            "fa",
            "tr",
            "vi",
            "th",
            "id",
            "ms",
            "fil",
            "cs",
            "da",
            "el",
            "fi",
            "hu",
            "no",
            "ro",
            "sk",
            "sv",
            "uk",
            "bg",
            "hr",
            "lt",
            "lv",
            "et",
            "sl",
        ]

    async def close(self):
        """Close any open resources."""
        pass
