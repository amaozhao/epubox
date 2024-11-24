from typing import List, Dict, Any, Optional, Tuple
from abc import ABC, abstractmethod
import asyncio
from dataclasses import dataclass
from bs4 import BeautifulSoup, NavigableString

from .html_processor import HTMLProcessor, TextFragment
from .base import TranslationRequest, TranslationResponse, BaseTranslationAdapter

@dataclass
class TranslationProgress:
    chapter_id: str
    total_fragments: int
    completed_fragments: int
    translated_fragments: Dict[str, str]  # fragment path -> translated text

class AdapterTranslationService:
    """Adapter to convert BaseTranslationAdapter to TranslationService interface."""
    
    def __init__(self, adapter: BaseTranslationAdapter):
        self.adapter = adapter
        
    async def translate_text(self, request: TranslationRequest) -> TranslationResponse:
        """Translate a single text fragment."""
        return await self.adapter.translate_text(request)
        
    async def translate_batch(self, requests: List[TranslationRequest]) -> List[TranslationResponse]:
        """Translate a batch of text fragments."""
        return await self.adapter.translate_batch(requests)

    async def detect_language(self, text: str) -> str:
        """Detect the language of a text."""
        return await self.adapter.detect_language(text)

    def get_supported_languages(self) -> List[str]:
        """Get list of supported language codes."""
        return self.adapter.get_supported_languages()

    async def get_translation_cost(self, text: str, source_lang: str, target_lang: str) -> float:
        """Calculate the cost of translation."""
        return await self.adapter.get_translation_cost(text, source_lang, target_lang)

    async def validate_languages(self, source_lang: str, target_lang: str) -> bool:
        """Validate if the language pair is supported."""
        return await self.adapter.validate_languages(source_lang, target_lang)

class TranslationService(ABC):
    @abstractmethod
    async def translate_text(self, request: TranslationRequest) -> TranslationResponse:
        """Translate a single text fragment."""
        pass

    @abstractmethod
    async def translate_batch(self, requests: List[TranslationRequest]) -> List[TranslationResponse]:
        """Translate a batch of text fragments."""
        pass

class SemanticTranslationService:
    def __init__(self, translation_adapter: BaseTranslationAdapter, batch_size: int = 10):
        """
        Initialize semantic translation service.
        
        Args:
            translation_adapter: Implementation of BaseTranslationAdapter
            batch_size: Number of text fragments to translate in one batch
        """
        self.adapter = translation_adapter
        self.translation_service = AdapterTranslationService(translation_adapter)
        self.html_processor = HTMLProcessor()
        self.batch_size = batch_size
        self.progress: Dict[str, TranslationProgress] = {}
        self.total_word_count = 0
        self.total_character_count = 0
        self.total_translation_cost = 0.0
        self.total_errors = 0

    def _create_progress(self, chapter_id: str, fragments: List[TextFragment]) -> TranslationProgress:
        """Create a new progress tracker for a chapter."""
        return TranslationProgress(
            chapter_id=chapter_id,
            total_fragments=len(fragments),
            completed_fragments=0,
            translated_fragments={}
        )

    def get_progress(self, chapter_id: str) -> Optional[TranslationProgress]:
        """Get translation progress for a chapter."""
        return self.progress.get(chapter_id)

    def clear_progress(self, chapter_id: str) -> None:
        """Clear translation progress for a chapter."""
        if chapter_id in self.progress:
            del self.progress[chapter_id]

    async def validate_languages(self, source_language: str, target_language: str) -> bool:
        """Validate language pair is supported."""
        return await self.translation_service.validate_languages(source_language, target_language)

    async def detect_language(self, text: str) -> str:
        """Detect language of text."""
        return await self.translation_service.detect_language(text)

    def get_supported_languages(self) -> List[str]:
        """Get list of supported languages."""
        return self.translation_service.get_supported_languages()

    async def get_translation_cost(self, text: str, source_lang: str, target_lang: str) -> float:
        """Calculate the cost of translation."""
        return await self.translation_service.get_translation_cost(text, source_lang, target_lang)

    async def translate_text_legacy(self, text: str, source_lang: str, target_lang: str) -> str:
        """Forward legacy translation method to adapter."""
        return await self.adapter.translate_text_legacy(text, source_lang, target_lang)

    async def translate_text(self, request: TranslationRequest) -> TranslationResponse:
        """Translate a single text fragment."""
        response = await self.translation_service.translate_text(request)
        
        # Update statistics
        self.total_word_count += len(request.text.split())
        self.total_character_count += len(request.text)
        cost = await self.get_translation_cost(request.text, request.source_language, request.target_language)
        self.total_translation_cost += cost if cost else 0
        
        return response

    async def translate_batch(self, requests: List[TranslationRequest]) -> List[TranslationResponse]:
        """Translate a batch of text fragments."""
        responses = await self.translation_service.translate_batch(requests)
        
        # Update statistics
        for request, response in zip(requests, responses):
            self.total_word_count += len(request.text.split())
            self.total_character_count += len(request.text)
            cost = await self.get_translation_cost(request.text, request.source_language, request.target_language)
            self.total_translation_cost += cost if cost else 0
        
        return responses

    async def translate_html(self, chapter_id: str, html_content: str, source_lang: str, target_lang: str, resume: bool = False) -> str:
        """
        Translate HTML content while preserving HTML structure.
        """
        # Parse HTML and extract text fragments
        soup = self.html_processor.parse_html(html_content)
        fragments = self.html_processor.extract_text_fragments(soup)
        
        # Create or get progress tracker
        if not resume or chapter_id not in self.progress:
            self.progress[chapter_id] = self._create_progress(chapter_id, fragments)
        progress = self.progress[chapter_id]
        
        # Process fragments in batches
        batch = []
        translations = []
        for fragment in fragments:
            # Skip if already translated
            if fragment.path in progress.translated_fragments:
                translations.append((fragment, progress.translated_fragments[fragment.path]))
                continue
                
            # Add to batch
            batch.append(fragment)
            
            # Process batch if full
            if len(batch) >= self.batch_size:
                batch_translations = await self._process_batch(batch, source_lang, target_lang, progress)
                translations.extend(batch_translations)
                batch = []
        
        # Process remaining fragments
        if batch:
            batch_translations = await self._process_batch(batch, source_lang, target_lang, progress)
            translations.extend(batch_translations)
        
        # Update HTML with translations
        return self.html_processor.rebuild_html(soup, translations)

    async def _process_batch(self, batch: List[TextFragment], source_lang: str, target_lang: str, progress: TranslationProgress) -> List[Tuple[TextFragment, str]]:
        """Process a batch of text fragments."""
        translations = []
        requests = [
            TranslationRequest(
                text=fragment.text,
                source_language=source_lang,
                target_language=target_lang
            )
            for fragment in batch
        ]
        
        try:
            responses = await self.translate_batch(requests)
            
            # Update progress
            for fragment, response in zip(batch, responses):
                if response and response.translated_text:
                    progress.translated_fragments[fragment.path] = response.translated_text
                    progress.completed_fragments += 1
                    translations.append((fragment, response.translated_text))
                
        except Exception as e:
            self.total_errors += 1
            raise e
            
        return translations
