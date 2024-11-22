from typing import Dict, List, Optional
import aiohttp
from aiohttp import ClientError

from .base import (
    BaseTranslationAdapter,
    TranslationRequest,
    TranslationResponse,
    TranslationError,
    UnsupportedLanguageError,
    TranslationQuotaExceededError,
    TranslationAPIError,
    InvalidRequestError
)

class DeepLTranslationAdapter(BaseTranslationAdapter):
    """DeepL Translation API adapter."""
    
    API_URL = "https://api-free.deepl.com/v2"  # Free tier API URL
    SUPPORTED_LANGUAGES = {
        "bg": "Bulgarian",
        "cs": "Czech",
        "da": "Danish",
        "de": "German",
        "el": "Greek",
        "en": "English",
        "es": "Spanish",
        "et": "Estonian",
        "fi": "Finnish",
        "fr": "French",
        "hu": "Hungarian",
        "id": "Indonesian",
        "it": "Italian",
        "ja": "Japanese",
        "lt": "Lithuanian",
        "lv": "Latvian",
        "nl": "Dutch",
        "pl": "Polish",
        "pt": "Portuguese",
        "ro": "Romanian",
        "ru": "Russian",
        "sk": "Slovak",
        "sl": "Slovenian",
        "sv": "Swedish",
        "tr": "Turkish",
        "uk": "Ukrainian",
        "zh-CN": "Chinese (Simplified)"
    }

    def __init__(self, api_key: str, **kwargs):
        super().__init__(api_key, **kwargs)
        self.is_pro = kwargs.get("is_pro", False)
        if self.is_pro:
            self.API_URL = "https://api.deepl.com/v2"

    async def _make_request(self, endpoint: str, params: Dict) -> Dict:
        """Make an HTTP request to the DeepL API."""
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"DeepL-Auth-Key {self.api_key}",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            try:
                async with session.post(
                    f"{self.API_URL}/{endpoint}",
                    headers=headers,
                    data=params
                ) as response:
                    if response.status == 429:
                        raise TranslationQuotaExceededError(
                            "Translation quota exceeded",
                            service="deepl"
                        )
                    elif response.status == 456:
                        raise TranslationQuotaExceededError(
                            "Character limit exceeded",
                            service="deepl"
                        )
                    elif response.status != 200:
                        raise TranslationAPIError(
                            f"API request failed with status {response.status}",
                            service="deepl",
                            details={"status": response.status}
                        )
                    return await response.json()
            except ClientError as e:
                raise TranslationAPIError(
                    "API request failed",
                    service="deepl",
                    details={"error": str(e)}
                )

    async def translate_text(self, request: TranslationRequest) -> TranslationResponse:
        """Translate a single piece of text using DeepL."""
        try:
            # Validate languages
            if not await self.validate_languages(request.source_language, request.target_language):
                raise UnsupportedLanguageError(
                    f"Unsupported language pair: {request.source_language} -> {request.target_language}",
                    service="deepl"
                )

            # Prepare request parameters
            params = {
                "text": request.text,
                "source_lang": request.source_language.upper(),
                "target_lang": request.target_language.upper()
            }
            if request.context:
                params.update(request.context)

            # Make API request
            result = await self._make_request("translate", params)

            return TranslationResponse(
                translated_text=result["translations"][0]["text"],
                source_language=request.source_language,
                target_language=request.target_language,
                service_metadata={
                    "detected_source_language": result["translations"][0].get("detected_source_language", "").lower()
                }
            )

        except TranslationError:
            raise
        except Exception as e:
            raise TranslationAPIError(
                "Translation failed",
                service="deepl",
                details={"error": str(e)}
            )

    async def translate_batch(self, requests: List[TranslationRequest]) -> List[TranslationResponse]:
        """Translate multiple texts in batch using DeepL."""
        try:
            # Group requests by language pair for efficiency
            grouped_requests = {}
            for req in requests:
                key = (req.source_language, req.target_language)
                if key not in grouped_requests:
                    grouped_requests[key] = []
                grouped_requests[key].append(req)

            responses = []
            for (source_lang, target_lang), reqs in grouped_requests.items():
                # Validate languages
                if not await self.validate_languages(source_lang, target_lang):
                    raise UnsupportedLanguageError(
                        f"Unsupported language pair: {source_lang} -> {target_lang}",
                        service="deepl"
                    )

                # Prepare request parameters
                params = {
                    "text": [req.text for req in reqs],
                    "source_lang": source_lang.upper(),
                    "target_lang": target_lang.upper()
                }

                # Make API request
                result = await self._make_request("translate", params)

                # Create responses
                for req, translation in zip(reqs, result["translations"]):
                    responses.append(TranslationResponse(
                        translated_text=translation["text"],
                        source_language=source_lang,
                        target_language=target_lang,
                        service_metadata={
                            "detected_source_language": translation.get("detected_source_language", "").lower()
                        }
                    ))

            return responses

        except TranslationError:
            raise
        except Exception as e:
            raise TranslationAPIError(
                "Batch translation failed",
                service="deepl",
                details={"error": str(e)}
            )

    async def detect_language(self, text: str) -> str:
        """Detect the language of a text using DeepL."""
        # DeepL doesn't have a dedicated language detection endpoint
        # We'll use the translation endpoint with a dummy target language
        try:
            params = {
                "text": text,
                "target_lang": "EN"  # Use English as dummy target
            }
            result = await self._make_request("translate", params)
            return result["translations"][0]["detected_source_language"].lower()
        except Exception as e:
            raise TranslationAPIError(
                "Language detection failed",
                service="deepl",
                details={"error": str(e)}
            )

    def get_supported_languages(self) -> List[str]:
        """Get list of supported language codes."""
        return list(self.SUPPORTED_LANGUAGES.keys())

    async def get_translation_cost(self, text: str, source_lang: str, target_lang: str) -> float:
        """Calculate the cost of translation.
        
        DeepL API pricing (Pro):
        - €20 per million characters for Pro
        - Free tier has 500,000 characters per month limit
        """
        char_count = len(text)
        if self.is_pro:
            cost_per_million = 20.0  # EUR
            return (char_count / 1_000_000) * cost_per_million
        return 0.0  # Free tier

    async def validate_languages(self, source_lang: str, target_lang: str) -> bool:
        """Validate if the language pair is supported."""
        return (
            source_lang.lower() in self.SUPPORTED_LANGUAGES and
            target_lang.lower() in self.SUPPORTED_LANGUAGES
        )
