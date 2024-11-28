"""Google Translate-based translator implementation."""

import asyncio
import logging
import re
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, TypeVar
from urllib.parse import quote

import httpx
from tenacity import (
    RetryError,
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.logging import get_logger

from .base import BaseTranslator

# Get logger for translation service
logger = get_logger("app.services.translation.google")

# Type variable for generic return type
T = TypeVar("T")


class GoogleTranslationError(Exception):
    """Custom exception for Google Translate errors."""

    pass


class GoogleTranslator(BaseTranslator):
    """Translator using Google Translate API."""

    # Common retry configuration
    RETRY_CONFIG = {
        "stop": stop_after_attempt(3),
        "wait": wait_exponential(multiplier=1, min=4, max=10),
        "retry": retry_if_exception_type((httpx.RequestError, asyncio.TimeoutError)),
        "before_sleep": before_sleep_log(logger, logging.DEBUG),
        "reraise": False,
    }

    # Batch processing configuration
    BATCH_CHUNK_SIZE = 50
    MAX_CONCURRENT_REQUESTS = 10

    # Supported languages with their names
    SUPPORTED_LANGUAGES = [
        {"code": "en", "name": "English"},
        {"code": "es", "name": "Spanish"},
        {"code": "fr", "name": "French"},
        {"code": "de", "name": "German"},
        {"code": "it", "name": "Italian"},
        {"code": "pt", "name": "Portuguese"},
        {"code": "ru", "name": "Russian"},
        {"code": "zh", "name": "Chinese"},
        {"code": "ja", "name": "Japanese"},
        {"code": "ko", "name": "Korean"},
        {"code": "ar", "name": "Arabic"},
        {"code": "hi", "name": "Hindi"},
        {"code": "auto", "name": "Auto-detect"},
    ]

    def __init__(self, api_key: str, source_lang: str, target_lang: str):
        """Initialize Google translator.

        Args:
            api_key: Not used for Google Translate
            source_lang: Source language code
            target_lang: Target language code
        """
        super().__init__(api_key, source_lang, target_lang)
        self.api_url = "https://translate.google.com/translate_a/single"
        self.headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "GoogleTranslate/6.29.59279 (iPhone; iOS 15.4; en; iPhone14,2)",
        }
        self.params = {
            "client": "it",
            "dt": ["qca", "t", "rmt", "bd", "rms", "sos", "md", "gt", "ld", "ss", "ex"],
            "otf": "2",
            "dj": "1",
            "hl": "en",
            "ie": "UTF-8",
            "oe": "UTF-8",
            "sl": source_lang if source_lang != "auto" else "auto",
            "tl": target_lang,
        }
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            headers=self.headers,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
        )
        logger.info(
            "Initialized Google Translator",
            source_lang=source_lang,
            target_lang=target_lang,
        )

    async def _make_request(
        self, text: str, process_response: Callable[[Dict[str, Any]], T]
    ) -> T:
        """Make a request to Google Translate API with retry logic.

        Args:
            text: Text to process
            process_response: Function to process the JSON response

        Returns:
            T: Processed response of type T

        Raises:
            GoogleTranslationError: If request fails after all retries
        """

        @retry(**self.RETRY_CONFIG)
        async def _request():
            try:
                data = {"q": text}
                response = await self._client.post(
                    self.api_url, params=self.params, data=data
                )

                if response.status_code == 200:
                    return process_response(response.json())
                else:
                    raise GoogleTranslationError(
                        f"Request failed with status code: {response.status_code}"
                    )
            except Exception as e:
                logger.error(
                    "Request failed",
                    error=str(e),
                    text_length=len(text),
                    source_lang=self.source_lang,
                    target_lang=self.target_lang,
                )
                raise

        return await _request()

    def _process_translation(self, json_response: Dict) -> str:
        """Process translation response.

        Args:
            json_response: Response from Google Translate API

        Returns:
            str: Translated text
        """
        translated = "".join(
            [sentence.get("trans", "") for sentence in json_response["sentences"]]
        )
        return re.sub(r"\n{3,}", "\n\n", translated)

    def _process_detection(self, json_response: Dict) -> str:
        """Process language detection response.

        Args:
            json_response: Response from Google Translate API

        Returns:
            str: Detected language code
        """
        if "src" in json_response:
            return json_response["src"]
        raise GoogleTranslationError("Language detection failed: 'src' not in response")

    async def translate_text(self, text: str) -> str:
        """Translate text using Google Translate.

        Args:
            text: Text to translate

        Returns:
            str: Translated text
        """
        try:
            return await self._make_request(text, self._process_translation)
        except (RetryError, GoogleTranslationError) as e:
            logger.error(
                "Translation failed after retries",
                error=str(e),
                text_length=len(text),
                source_lang=self.source_lang,
                target_lang=self.target_lang,
            )
            # Apply fallback replacements
            return (
                text.replace("您", "你")
                .replace("覆盖", "封面")
                .replace("法学硕士", " LLM ")
            )

    async def translate_batch(self, texts: List[str]) -> List[str]:
        """Translate multiple texts using Google Translate.

        Args:
            texts: List of texts to translate

        Returns:
            List[str]: List of translated texts
        """
        logger.info(
            "Starting batch translation",
            batch_size=len(texts),
            source_lang=self.source_lang,
            target_lang=self.target_lang,
        )

        # Process texts in chunks to avoid overwhelming the service
        results = []
        for i in range(0, len(texts), self.BATCH_CHUNK_SIZE):
            chunk = texts[i : i + self.BATCH_CHUNK_SIZE]
            # Create semaphore to limit concurrent requests
            sem = asyncio.Semaphore(self.MAX_CONCURRENT_REQUESTS)

            async def translate_with_semaphore(text):
                async with sem:
                    return await self.translate_text(text)

            chunk_tasks = [translate_with_semaphore(text) for text in chunk]
            chunk_results = await asyncio.gather(*chunk_tasks)
            results.extend(chunk_results)

        return results

    async def detect_language(self, text: str) -> str:
        """Detect language using Google Translate.

        Args:
            text: Text to analyze

        Returns:
            str: Detected language code
        """
        try:
            return await self._make_request(text, self._process_detection)
        except (RetryError, GoogleTranslationError) as e:
            logger.error(
                "Language detection failed", error=str(e), text_length=len(text)
            )
            return "en"  # Default fallback

    def get_supported_languages(self) -> Dict[str, List[Dict[str, str]]]:
        """Get list of supported languages.

        Returns:
            Dict[str, List[Dict[str, str]]]: Dictionary containing source and target languages
        """
        # Remove auto-detect from target languages
        target_languages = [
            lang for lang in self.SUPPORTED_LANGUAGES if lang["code"] != "auto"
        ]
        return {
            "source_languages": self.SUPPORTED_LANGUAGES,
            "target_languages": target_languages,
        }

    async def close(self):
        """Close the httpx client."""
        await self._client.aclose()
