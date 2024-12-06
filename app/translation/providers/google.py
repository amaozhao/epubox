"""Google Cloud Translation provider."""

from typing import Optional

from google.cloud import translate_v2
from google.oauth2 import service_account

from ..errors import ConfigurationError
from .base import TranslationProvider


class GoogleProvider(TranslationProvider):
    """Google Cloud Translation provider implementation."""

    def __init__(self, config: dict, **kwargs):
        super().__init__(config, **kwargs)
        self.credentials_info = config.get("credentials")
        self.project_id = config.get("project_id")
        self.client: Optional[translate_v2.Client] = None

    def get_provider_type(self) -> str:
        """Get provider type identifier."""
        return "google"

    def validate_config(self, config: dict) -> bool:
        """Validate provider configuration."""
        if not config.get("credentials"):
            raise ConfigurationError("Google Cloud credentials are required")
        if not config.get("project_id"):
            raise ConfigurationError("Google Cloud project ID is required")
        return True

    async def _initialize(self):
        """Initialize Google Cloud Translation client."""
        credentials = service_account.Credentials.from_service_account_info(
            self.credentials_info
        )
        self.client = translate_v2.Client(
            credentials=credentials, project=self.project_id
        )

    async def _cleanup(self):
        """Cleanup Google Cloud Translation client resources."""
        if self.client:
            self.client = None

    async def _translate(
        self, text: str, source_lang: str, target_lang: str, **kwargs
    ) -> str:
        """Translate text using Google Cloud Translation API."""
        if not self.client:
            raise ConfigurationError("Google Cloud Translation client not initialized")

        # Google Cloud Translation API uses different language code format
        source_lang = source_lang.split("-")[0]  # Convert 'en-US' to 'en'
        target_lang = target_lang.split("-")[0]

        result = self.client.translate(
            text, target_language=target_lang, source_language=source_lang, **kwargs
        )

        return result["translatedText"]
