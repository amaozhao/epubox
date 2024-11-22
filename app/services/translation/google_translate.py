import re
import asyncio
import random
from typing import Dict, List, Optional, Union
import httpx
from rich import print

from .base import (
    BaseTranslationAdapter,
    TranslationRequest,
    TranslationResponse,
    TranslationError,
    TranslationAPIError,
    UnsupportedLanguageError,
    TranslationQuotaExceededError
)

class GoogleTranslateAdapter(BaseTranslationAdapter):
    """
    Google translate adapter using free Google Translate API
    """
    def __init__(self, api_key: str = "", **kwargs):
        super().__init__(api_key, **kwargs)
        self.api_url = "https://translate.google.com/translate_a/single?client=it&dt=qca&dt=t&dt=rmt&dt=bd&dt=rms&dt=sos&dt=md&dt=gt&dt=ld&dt=ss&dt=ex&otf=2&dj=1&hl=en&ie=UTF-8&oe=UTF-8&sl=auto"
        self.headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "GoogleTranslate/6.29.59279 (iPhone; iOS 15.4; en; iPhone14,2)",
        }
        self._supported_languages = {
            "en": "English",
            "zh-CN": "Chinese (Simplified)",
            "es": "Spanish",
            "fr": "French",
            "de": "German",
            "ja": "Japanese",
            "ko": "Korean",
            "ru": "Russian"
        }
        self._client = None

    async def __aenter__(self):
        """Async context manager entry."""
        if not self._client:
            self._client = httpx.AsyncClient()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def translate_text(self, request: TranslationRequest) -> TranslationResponse:
        """Translate a single piece of text using Google Translate API."""
        if not request.text:
            return TranslationResponse(
                translated_text="",
                source_language=request.source_language,
                target_language=request.target_language
            )

        # Validate languages
        if not await self.validate_languages(request.source_language, request.target_language):
            raise UnsupportedLanguageError(
                f"Language pair not supported: {request.source_language} -> {request.target_language}",
                "google_translate"
            )

        url = f"{self.api_url}&tl={request.target_language}"
        
        try:
            translated_text = await self._retry_translate(request.text, url)
            
            return TranslationResponse(
                translated_text=translated_text,
                source_language=request.source_language,
                target_language=request.target_language,
                confidence=1.0,
                service_metadata={"provider": "google_translate_free"}
            )
        except (TranslationQuotaExceededError, UnsupportedLanguageError):
            # Let specific errors propagate up
            raise
        except Exception as e:
            raise TranslationAPIError(
                f"Translation failed: {str(e)}",
                "google_translate",
                {"error": str(e)}
            )

    async def translate_batch(self, requests: List[TranslationRequest]) -> List[TranslationResponse]:
        """Translate multiple texts using Google Translate API."""
        if not requests:
            return []

        results = []
        for request in requests:
            result = await self.translate_text(request)
            results.append(result)
        
        return results

    async def _retry_translate(self, text: str, url: str, max_retries: int = 3, base_timeout: float = 3.0) -> str:
        """
        Retry translation with exponential backoff.
        
        Args:
            text: Text to translate
            url: Translation API URL
            max_retries: Maximum number of retry attempts
            base_timeout: Base request timeout in seconds
            
        Returns:
            Translated text
            
        Raises:
            TranslationAPIError: If all retries fail or other API errors occur
            TranslationQuotaExceededError: If API quota is exceeded
        """
        last_error = None
        
        for attempt in range(max_retries):
            # Calculate exponential backoff with jitter
            backoff = min(300, (2 ** attempt) + random.uniform(0, 0.1))
            timeout = base_timeout * (1.5 ** attempt)  # Exponential timeout increase
            
            try:
                if not self._client:
                    self._client = httpx.AsyncClient()
                
                response = await self._client.post(
                    url,
                    headers=self.headers,
                    data=f"q={httpx.utils.quote(text)}",
                    timeout=timeout
                )
                
                if response.status_code == 200:
                    try:
                        result = response.json()
                        return "".join(
                            [sentence.get("trans", "") for sentence in result.get("sentences", [])]
                        )
                    except (ValueError, KeyError) as e:
                        raise TranslationAPIError(
                            "Invalid response format from translation API",
                            "google_translate",
                            {"error": str(e), "response": response.text}
                        )
                        
                elif response.status_code == 429:
                    raise TranslationQuotaExceededError(
                        "Translation quota exceeded",
                        "google_translate",
                        {"retry_after": response.headers.get("Retry-After")}
                    )
                elif response.status_code >= 500:
                    # Server errors are retryable
                    last_error = TranslationAPIError(
                        f"Server error: {response.status_code}",
                        "google_translate",
                        {"status_code": response.status_code, "response": response.text}
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(backoff)
                        continue
                    raise last_error
                else:
                    # Client errors are not retryable
                    raise TranslationAPIError(
                        f"Translation API error: {response.status_code}",
                        "google_translate",
                        {"status_code": response.status_code, "response": response.text}
                    )
                    
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError) as e:
                last_error = TranslationAPIError(
                    f"Network error during translation",
                    "google_translate",
                    {"error": str(e), "error_type": e.__class__.__name__, "attempt": attempt + 1}
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(backoff)
                    continue
                raise last_error
            except TranslationQuotaExceededError:
                # Let quota errors propagate up
                raise
            except Exception as e:
                # Unexpected errors are not retryable
                raise TranslationAPIError(
                    f"Unexpected error during translation: {str(e)}",
                    "google_translate",
                    {"error": str(e), "error_type": e.__class__.__name__}
                )

        # If we get here, all retries failed
        raise last_error or TranslationAPIError(
            f"Translation failed after {max_retries} retries",
            "google_translate",
            {"max_retries": max_retries}
        )

    async def detect_language(self, text: str) -> str:
        """Detect the language of a text."""
        # Google Translate API automatically detects the language
        # For now, return 'auto' as we're using the free API
        return "auto"

    def get_supported_languages(self) -> List[str]:
        """Get list of supported language codes."""
        return list(self._supported_languages.keys())

    async def get_translation_cost(self, text: str, source_lang: str, target_lang: str) -> float:
        """Calculate the cost of translation."""
        # Free API, so cost is always 0
        return 0.0

    async def _validate_language_pair(self, source_lang: str, target_lang: str) -> bool:
        """Validate if the language pair is supported."""
        return await self.validate_languages(source_lang, target_lang)

    async def validate_languages(self, source_lang: str, target_lang: str) -> bool:
        """Validate that both source and target languages are supported."""
        # Allow "auto" as a valid source language
        if source_lang == "auto":
            return target_lang in self._supported_languages
        return source_lang in self._supported_languages and target_lang in self._supported_languages
