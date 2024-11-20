from enum import Enum
import asyncio
import re
from bs4 import BeautifulSoup, Tag, NavigableString
from typing import Dict, List, Tuple, Optional
import uuid
import logging

import httpx
from mistralai import Mistral
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.core.config import settings
from utils.logging import get_logger

logger = get_logger(__name__)


class TranslationProvider(str, Enum):
    """Available translation providers."""
    GOOGLE = "google"
    OPENAI = "openai"
    MOCK = "mock"
    MISTRAL = "mistral"


class TranslationConfig(BaseModel):
    """Configuration for translation."""
    provider: TranslationProvider
    api_key: Optional[str] = None
    model: Optional[str] = None
    source_lang: str = "en"
    target_lang: str = "zh"
    max_chars: int = 2000
    separator: str = '\n'
    temperature: float = 0.3
    top_p: float = 0.9
    retry_attempts: int = 3
    retry_delay: int = 1
    preserve_tags: bool = True


    def get_api_key(self) -> str:
        """Get API key from settings if not provided."""
        if self.api_key:
            return self.api_key
            
        if self.provider == TranslationProvider.OPENAI:
            return settings.OPENAI_API_KEY
        elif self.provider == TranslationProvider.MISTRAL:
            return settings.MISTRAL_API_KEY
        elif self.provider == TranslationProvider.GOOGLE:
            return settings.GOOGLE_API_KEY
        return ""

    def get_model(self) -> str:
        """Get model from settings if not provided."""
        if self.model:
            return self.model
            
        if self.provider == TranslationProvider.OPENAI:
            return settings.OPENAI_MODEL
        elif self.provider == TranslationProvider.MISTRAL:
            return settings.MISTRAL_MODEL
        return ""


class TranslationService:
    """Base class for translation services."""

    def __init__(self, config: TranslationConfig):
        self.config = config
        self._request_lock = asyncio.Lock()
        self._last_request_time = 0
        self.min_request_interval = 0.5
        self.max_batch_tokens = 4000
        self.batch_separator = "|||"

    async def _wait_for_rate_limit(self):
        """Wait for rate limit to reset."""
        async with self._request_lock:
            current_time = asyncio.get_event_loop().time()
            time_since_last_request = current_time - self._last_request_time
            if time_since_last_request < self.min_request_interval:
                await asyncio.sleep(self.min_request_interval - time_since_last_request)
            self._last_request_time = asyncio.get_event_loop().time()

    def _estimate_tokens(self, text: str) -> int:
        """Estimate number of tokens in text."""
        # Simple estimation: 1 token ≈ 4 characters
        return len(text) // 4 + 1

    def _create_batches(self, texts: list[str]) -> list[list[str]]:
        """Create batches of texts that fit within token limits."""
        current_batch = []
        current_tokens = 0
        batches = []
        
        for text in texts:
            text_tokens = self._estimate_tokens(text)
            # Account for system prompt and some overhead
            if current_tokens + text_tokens > self.max_batch_tokens:
                if current_batch:  # If we have a batch, add it to batches
                    batches.append(current_batch)
                    current_batch = [text]
                    current_tokens = text_tokens
                else:  # Single text is too large, process it alone
                    batches.append([text])
            else:
                current_batch.append(text)
                current_tokens += text_tokens
        
        if current_batch:
            batches.append(current_batch)
        
        return batches

    async def translate(self, text: str) -> str:
        """Translate text from source language to target language."""
        return (await self.translate_batch([text]))[0]

    async def translate_batch(self, texts: list[str]) -> list[str]:
        """Translate multiple texts efficiently in batches."""
        if not texts:
            return []

        all_translations = []
        batches = self._create_batches(texts)
        
        for batch_idx, batch in enumerate(batches):
            try:
                await self._wait_for_rate_limit()
                batch_translations = await self._translate_batch(batch)
                
                if len(batch_translations) != len(batch):
                    logger.error("batch_translation_mismatch", 
                               expected=len(batch), 
                               received=len(batch_translations))
                    batch_translations = batch
                
                all_translations.extend(batch_translations)
                
            except Exception as e:
                logger.error("batch_translation_error", 
                           error=str(e), 
                           batch_index=batch_idx,
                           exc_info=True)
                # Return original texts for this batch as fallback
                all_translations.extend(batch)
        
        return all_translations

    async def _translate_batch(self, batch: list[str]) -> list[str]:
        """Implement this method in each provider to handle batch translation."""
        raise NotImplementedError


class GoogleTranslationService(TranslationService):
    """Google Translation implementation"""
    
    def __init__(self, config: TranslationConfig):
        super().__init__(config)
        self.api_url = "https://translate.google.com/translate_a/single?client=it&dt=qca&dt=t&dt=rmt&dt=bd&dt=rms&dt=sos&dt=md&dt=gt&dt=ld&dt=ss&dt=ex&otf=2&dj=1&hl=en&ie=UTF-8&oe=UTF-8"
        self.headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "GoogleTranslate/6.29.59279 (iPhone; iOS 15.4; en; iPhone14,2)",
        }
        self.session = httpx.AsyncClient()
    
    async def translate(self, text: str) -> str:
        """Translate text using Google Translate API"""
        return await self._retry_translate(text)
    
    async def _retry_translate(self, text: str, timeout: Optional[int] = None) -> str:
        """
        Retry translating with timeout support.
        Args:
            text: The text to translate
            timeout: Optional timeout override
        Returns:
            Translated text or the original if failed
        """
        url = f"{self.api_url}&sl={self.config.source_lang}&tl={self.config.target_lang}"
        attempts = 0
        timeout = timeout or self.config.retry_attempts

        while attempts < timeout:
            attempts += 1
            try:
                r = await self.session.post(
                    url,
                    headers=self.headers,
                    data=f"q={urllib.parse.quote_plus(text)}",
                    timeout=3,
                )
                if r.status_code == 200:
                    t_text = "".join(
                        [
                            sentence.get("trans", "")
                            for sentence in r.json()["sentences"]
                        ],
                    )
                    return t_text
            except httpx.RequestError as e:
                logger.error(f"Google Translate request failed (attempt {attempts}/{timeout}): {e}")
                if attempts < timeout:
                    await asyncio.sleep(self.config.retry_delay * attempts)
        
        # If all retries failed, apply common replacements
        text = text.replace("您", "你")
        text = text.replace("覆盖", "封面")
        text = text.replace("法学硕士", "LLM")
        return text


class OpenAITranslationService(TranslationService):
    """OpenAI translation service implementation."""

    def __init__(self, config: TranslationConfig):
        super().__init__(config)
        self.client = AsyncOpenAI(api_key=config.get_api_key())
        self.model = config.get_model()

    async def _translate_batch(self, batch: list[str]) -> list[str]:
        """Translate multiple texts in a batch."""
        if not batch:
            return []
            
        # Handle None values before batch creation
        sanitized_batch = [text if text is not None else "" for text in batch]
        
        # Split into smaller batches based on token limit
        batches = self._create_batches(sanitized_batch)
        all_results = []
        
        for sub_batch in batches:
            # Wait for rate limit for each sub-batch
            await self._wait_for_rate_limit()
            
            results = []
            for text in sub_batch:
                if text == "":  # Was None originally
                    results.append("[MOCK ERROR] None input")
                elif self.config.preserve_tags and ("<" in text and ">" in text):
                    # Preserve HTML tags in mock translation
                    results.append(f"[MOCK HTML] {text}")
                else:
                    results.append(f"[MOCK] {text}")
            
            all_results.extend(results)
                
        return all_results


class MistralTranslationService(TranslationService):
    """Mistral translation service implementation with batch processing and HTML handling."""

    def __init__(self, config: TranslationConfig):
        super().__init__(config)
        self.client = Mistral(api_key=config.get_api_key())
        self.model = config.get_model() or "mistral-large-latest"
        # Regular expressions for HTML processing
        self.tag_pattern = re.compile(r'<[^>]+>')
        self.attr_pattern = re.compile(r'\s+(\w+)=["\'](.*?)["\']')

    def _split_at_html_boundary(self, text: str, max_tokens: int) -> List[str]:
        """Split text at HTML tag boundaries while respecting token limits."""
        soup = BeautifulSoup(text, 'html.parser')
        chunks = []
        current_chunk = []
        current_tokens = 0
        open_tags = []

        def estimate_tokens(text: str) -> int:
            return len(text) // 4 + 1

        def close_chunk():
            # Close all open tags in reverse order
            closing_tags = [f"</{tag}>" for tag in reversed(open_tags)]
            chunk_text = ''.join(current_chunk) + ''.join(closing_tags)
            chunks.append(chunk_text)
            # Start new chunk with all currently open tags
            opening_tags = [f"<{tag}>" for tag in open_tags]
            current_chunk.clear()
            current_chunk.extend(opening_tags)
            return estimate_tokens(''.join(opening_tags))

        for element in soup.descendants:
            if isinstance(element, NavigableString) and str(element).strip():
                text = str(element)
                text_tokens = estimate_tokens(text)
                
                # If adding this text would exceed the limit, close the current chunk
                if current_tokens + text_tokens > max_tokens and current_chunk:
                    current_tokens = close_chunk()
                
                current_chunk.append(text)
                current_tokens += text_tokens
            elif isinstance(element, Tag):
                if element.name not in ['br', 'img', 'hr']:  # Non-self-closing tags
                    if element.parent and element.parent.name not in open_tags:
                        open_tags.append(element.name)

        # Add the final chunk if there is one
        if current_chunk:
            closing_tags = [f"</{tag}>" for tag in reversed(open_tags)]
            chunks.append(''.join(current_chunk) + ''.join(closing_tags))

        return chunks

    async def _translate_batch(self, batch: list[str]) -> list[str]:
        """Translate multiple texts in a batch with HTML handling."""
        if not batch:
            return []

        all_translations = []
        
        for text in batch:
            try:
                # Split the text at HTML boundaries
                chunks = self._split_at_html_boundary(text, self.max_batch_tokens)
                chunk_translations = []
                
                for chunk in chunks:
                    await self._wait_for_rate_limit()
                    
                    system_prompt = (
                        f"Translate from {self.config.source_lang} to {self.config.target_lang}. "
                        f"Preserve all HTML tags exactly as they appear. "
                        f"Only output the translation, no explanations."
                    )

                    messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": chunk}
                    ]

                    chat_response = self.client.chat.complete(
                        model=self.model,
                        messages=messages,
                        temperature=self.config.temperature,
                        top_p=self.config.top_p
                    )
                    
                    translation = chat_response.choices[0].message.content.strip()
                    chunk_translations.append(translation)
                
                # Combine all chunk translations
                full_translation = ''.join(chunk_translations)
                all_translations.append(full_translation)
                
            except Exception as e:
                logger.error("translation_error", 
                           error=str(e),
                           text_length=len(text),
                           exc_info=True)
                # Return original text as fallback
                all_translations.append(text)
        
        return all_translations


class MockTranslationService(TranslationService):
    """Mock translation service for testing."""
    
    def __init__(self, config: TranslationConfig):
        super().__init__(config)
        # Set a smaller max_batch_tokens for testing
        self.max_batch_tokens = 500  # Even smaller for testing
        self.min_request_interval = 0.1  # Smaller interval for faster tests

    def _estimate_tokens(self, text: str) -> int:
        """Override token estimation for testing."""
        if text is None:
            return 0
        # Make token estimation more aggressive for testing
        return len(text) // 2 + 1  # 1 token ≈ 2 characters for testing

    async def _translate_batch(self, batch: list[str]) -> list[str]:
        """Translate multiple texts in a batch."""
        if not batch:
            return []
            
        # Handle None values before batch creation
        sanitized_batch = [text if text is not None else "" for text in batch]
        
        # Split into smaller batches based on token limit
        batches = self._create_batches(sanitized_batch)
        all_results = []
        
        for sub_batch in batches:
            # Wait for rate limit for each sub-batch
            await self._wait_for_rate_limit()
            
            results = []
            for text in sub_batch:
                if text == "":  # Was None originally
                    results.append("[MOCK ERROR] None input")
                elif self.config.preserve_tags and ("<" in text and ">" in text):
                    # Preserve HTML tags in mock translation
                    results.append(f"[MOCK HTML] {text}")
                else:
                    results.append(f"[MOCK] {text}")
            
            all_results.extend(results)
                
        return all_results


class TranslationServiceFactory:
    """Factory for creating translation services."""

    @staticmethod
    def create_service(config: TranslationConfig) -> TranslationService:
        """Create a translation service based on the provider."""
        if config.provider == TranslationProvider.GOOGLE:
            return GoogleTranslationService(config)
        elif config.provider == TranslationProvider.OPENAI:
            return OpenAITranslationService(config)
        elif config.provider == TranslationProvider.MISTRAL:
            return MistralTranslationService(config)
        elif config.provider == TranslationProvider.MOCK:
            return MockTranslationService(config)
        else:
            raise ValueError(f"Unsupported translation provider: {config.provider}")


class Translator:
    """Main translator class."""

    def __init__(self, config: TranslationConfig):
        self.config = config
        self.service = TranslationServiceFactory.create_service(config)

    async def translate(self, text: str) -> str:
        """Translate text using the configured service."""
        if not text.strip():
            return text

        chunks = self._split_text(text)
        translated_chunks = []

        for chunk in chunks:
            translated = await self.service.translate(chunk)
            translated_chunks.append(translated)

        return self.config.separator.join(translated_chunks)

    def _split_text(self, text: str) -> list[str]:
        """Split text into chunks based on max_chars."""
        if len(text) <= self.config.max_chars:
            return [text]

        chunks = []
        current_chunk = []
        current_length = 0

        for word in text.split():
            word_length = len(word) + 1  # +1 for space
            if current_length + word_length > self.config.max_chars:
                chunks.append(" ".join(current_chunk))
                current_chunk = [word]
                current_length = word_length
            else:
                current_chunk.append(word)
                current_length += word_length

        if current_chunk:
            chunks.append(" ".join(current_chunk))

        return chunks
