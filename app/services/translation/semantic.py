from typing import List, Dict, Any, Optional
from abc import ABC, abstractmethod
import asyncio
from dataclasses import dataclass

from .html_processor import HTMLProcessor, TextFragment

@dataclass
class TranslationProgress:
    chapter_id: str
    total_fragments: int
    completed_fragments: int
    translated_fragments: Dict[str, str]  # fragment path -> translated text

class TranslationService(ABC):
    @abstractmethod
    async def translate_text(self, text: str, source_lang: str, target_lang: str) -> str:
        """Translate a single text fragment."""
        pass

    @abstractmethod
    async def translate_batch(self, texts: List[str], source_language: str, target_language: str) -> List[str]:
        """Translate a batch of text fragments."""
        pass

class SemanticTranslationService:
    def __init__(self, translation_service: TranslationService, batch_size: int = 10):
        """
        Initialize semantic translation service.
        
        Args:
            translation_service: Implementation of TranslationService
            batch_size: Number of text fragments to translate in one batch
        """
        self.translation_service = translation_service
        self.html_processor = HTMLProcessor()
        self.batch_size = batch_size
        self.progress: Dict[str, TranslationProgress] = {}

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

    async def translate_html(self, 
                           chapter_id: str,
                           html_content: str,
                           source_lang: str,
                           target_lang: str,
                           resume: bool = False) -> str:
        """
        Translate HTML content while preserving structure.
        
        Args:
            chapter_id: Unique identifier for the chapter
            html_content: Raw HTML content to translate
            source_lang: Source language code
            target_lang: Target language code
            resume: Whether to resume from previous progress
            
        Returns:
            Translated HTML content
        """
        # Parse HTML and extract fragments
        soup, fragments = self.html_processor.process_html(html_content)
        
        # Get or create progress tracker
        progress = self.get_progress(chapter_id) if resume else None
        if not progress:
            progress = self._create_progress(chapter_id, fragments)
            self.progress[chapter_id] = progress
        
        # Prepare fragments for translation
        remaining_fragments = []
        translated_pairs = []
        for fragment in fragments:
            # Skip empty or whitespace-only fragments
            if not fragment.text or fragment.text.isspace():
                translated_pairs.append((fragment, fragment.text))
                continue
                
            if fragment.path not in progress.translated_fragments:
                remaining_fragments.append(fragment)
        
        # Translate in batches
        for i in range(0, len(remaining_fragments), self.batch_size):
            batch = remaining_fragments[i:i + self.batch_size]
            texts = [f.text for f in batch]
            
            try:
                translated_texts = await self.translation_service.translate_batch(
                    texts=texts,
                    source_language=source_lang,
                    target_language=target_lang
                )
                
                # Update progress and store translations
                for fragment, translated_text in zip(batch, translated_texts):
                    progress.translated_fragments[fragment.path] = translated_text
                    translated_pairs.append((fragment, translated_text))
                    progress.completed_fragments += 1
            except Exception as e:
                print(f"Error translating batch: {e}")
                raise
        
        # Add previously translated fragments
        for fragment in fragments:
            if fragment.path in progress.translated_fragments:
                translated_pairs.append((fragment, progress.translated_fragments[fragment.path]))
            elif not fragment.text or fragment.text.isspace():
                translated_pairs.append((fragment, fragment.text))
        
        # Sort translated pairs by original document order
        translated_pairs.sort(key=lambda x: fragments.index(x[0]))
        
        # Rebuild HTML with translations
        translated_html = self.html_processor.rebuild_html(soup, translated_pairs)
        return translated_html

    def clear_progress(self, chapter_id: str):
        """Clear translation progress for a chapter."""
        if chapter_id in self.progress:
            del self.progress[chapter_id]
