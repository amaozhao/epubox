"""OpenAI translation provider."""

from typing import Optional

from openai import AsyncOpenAI

from ..errors import ConfigurationError
from .base import TranslationProvider


class OpenAIProvider(TranslationProvider):
    """OpenAI translation provider implementation."""

    def __init__(self, config: dict, **kwargs):
        super().__init__(config, **kwargs)
        self.api_key = config.get("api_key")
        self.model = config.get("model", "gpt-3.5-turbo")
        self.client: Optional[AsyncOpenAI] = None

    def get_provider_type(self) -> str:
        """Get provider type identifier."""
        return "openai"

    def validate_config(self, config: dict) -> bool:
        """Validate provider configuration."""
        if not config.get("api_key"):
            raise ConfigurationError("OpenAI API key is required")
        return True

    async def _initialize(self):
        """Initialize OpenAI client."""
        self.client = AsyncOpenAI(api_key=self.api_key)

    async def _cleanup(self):
        """Cleanup OpenAI client resources."""
        if self.client:
            await self.client.close()
            self.client = None

    async def _translate(
        self, text: str, source_lang: str, target_lang: str, **kwargs
    ) -> str:
        """Translate text using OpenAI API."""
        if not self.client:
            raise ConfigurationError("OpenAI client not initialized")

        system_prompt = f"You are a professional translator. Translate the following text from {source_lang} to {target_lang}. Provide only the translated text without any explanations or additional content."

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            temperature=0.3,  # Lower temperature for more consistent translations
            **kwargs,
        )

        return response.choices[0].message.content.strip()
