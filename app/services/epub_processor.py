import os
from typing import List, Dict, Optional, Tuple, Any, Callable, Awaitable
from dataclasses import dataclass
from datetime import datetime
import json
import logging
from bs4 import BeautifulSoup
import ebooklib
from ebooklib import epub
import aiofiles

from app.core.config import settings
from app.services.translation.base import (
    BaseTranslationAdapter,
    TranslationRequest,
    TranslationResponse,
)
import asyncio

@dataclass
class TranslationError:
    """Record of a translation error."""
    error_type: str
    message: str
    timestamp: datetime
    fragment: str
    retry_count: int = 0

@dataclass
class ChapterProgress:
    """Progress information for a single chapter."""
    chapter_id: str
    title: str = ""
    total_fragments: int = 0
    completed_fragments: int = 0
    word_count: int = 0
    character_count: int = 0
    translation_cost: float = 0.0
    is_completed: bool = False
    start_time: datetime = None
    end_time: datetime = None
    errors: List[TranslationError] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []
        if self.start_time is None:
            self.start_time = datetime.now()

    @property
    def duration(self) -> float:
        """Calculate duration in seconds."""
        if not self.end_time:
            return 0
        return (self.end_time - self.start_time).total_seconds()

    @property
    def completion_percentage(self) -> float:
        """Calculate completion percentage."""
        if self.total_fragments == 0:
            return 100.0
        return (self.completed_fragments / self.total_fragments) * 100

    def add_error(self, error_type: str, message: str, fragment: str = "", retry_count: int = 0):
        """Add an error to the chapter's error list."""
        error = TranslationError(
            error_type=error_type,
            message=message,
            timestamp=datetime.now(),
            fragment=fragment,
            retry_count=retry_count
        )
        self.errors.append(error)

@dataclass
class TranslationProgress:
    """Overall translation progress information."""
    total_chapters: int = 0
    completed_chapters: int = 0
    total_word_count: int = 0
    total_character_count: int = 0
    total_translation_cost: float = 0.0
    start_time: datetime = None
    end_time: datetime = None
    chapters_progress: Dict[str, ChapterProgress] = None
    current_chapter: ChapterProgress = None

    def __post_init__(self):
        if self.chapters_progress is None:
            self.chapters_progress = {}
        if self.start_time is None:
            self.start_time = datetime.now()

    @property
    def duration(self) -> float:
        """Calculate duration in seconds."""
        if not self.end_time:
            return (datetime.now() - self.start_time).total_seconds()
        return (self.end_time - self.start_time).total_seconds()

    @property
    def completion_percentage(self) -> float:
        """Calculate completion percentage."""
        if self.total_chapters == 0:
            return 100.0  # 空书视为已完成
        return (self.completed_chapters / self.total_chapters) * 100

    @property
    def total_errors(self) -> int:
        """Get total number of errors across all chapters."""
        return sum(len(chapter.errors) for chapter in self.chapters_progress.values())

    def to_dict(self) -> dict:
        """Convert progress to dictionary for serialization."""
        return {
            'total_chapters': self.total_chapters,
            'completed_chapters': self.completed_chapters,
            'total_word_count': self.total_word_count,
            'total_character_count': self.total_character_count,
            'total_translation_cost': self.total_translation_cost,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'chapters_progress': {
                chapter_id: {
                    'chapter_id': chapter.chapter_id,
                    'title': chapter.title,
                    'total_fragments': chapter.total_fragments,
                    'completed_fragments': chapter.completed_fragments,
                    'word_count': chapter.word_count,
                    'character_count': chapter.character_count,
                    'translation_cost': chapter.translation_cost,
                    'is_completed': chapter.is_completed,
                    'start_time': chapter.start_time.isoformat() if chapter.start_time else None,
                    'end_time': chapter.end_time.isoformat() if chapter.end_time else None,
                    'errors': [
                        {
                            'error_type': error.error_type,
                            'message': error.message,
                            'timestamp': error.timestamp.isoformat(),
                            'fragment': error.fragment,
                            'retry_count': error.retry_count
                        }
                        for error in chapter.errors
                    ]
                }
                for chapter_id, chapter in self.chapters_progress.items()
            }
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'TranslationProgress':
        """Create progress from dictionary."""
        progress = cls()
        progress.total_chapters = data.get('total_chapters', 0)
        progress.completed_chapters = data.get('completed_chapters', 0)
        progress.total_word_count = data.get('total_word_count', 0)
        progress.total_character_count = data.get('total_character_count', 0)
        progress.total_translation_cost = data.get('total_translation_cost', 0.0)
        progress.start_time = datetime.fromisoformat(data['start_time']) if data.get('start_time') else None
        progress.end_time = datetime.fromisoformat(data['end_time']) if data.get('end_time') else None

        for chapter_id, chapter_data in data.get('chapters_progress', {}).items():
            chapter = ChapterProgress(
                chapter_id=chapter_data['chapter_id'],
                title=chapter_data.get('title', ''),
                total_fragments=chapter_data.get('total_fragments', 0),
                completed_fragments=chapter_data.get('completed_fragments', 0),
                word_count=chapter_data.get('word_count', 0),
                character_count=chapter_data.get('character_count', 0),
                translation_cost=chapter_data.get('translation_cost', 0.0),
                is_completed=chapter_data.get('is_completed', False),
                start_time=datetime.fromisoformat(chapter_data['start_time']) if chapter_data.get('start_time') else None,
                end_time=datetime.fromisoformat(chapter_data['end_time']) if chapter_data.get('end_time') else None
            )

            for error_data in chapter_data.get('errors', []):
                chapter.add_error(
                    error_type=error_data['error_type'],
                    message=error_data['message'],
                    fragment=error_data.get('fragment', ''),
                    retry_count=error_data.get('retry_count', 0)
                )

            progress.chapters_progress[chapter_id] = chapter

        return progress

class EPUBProcessor:
    """EPUB file processor for translation."""
    
    MAX_RETRIES = 3  # 最大重试次数
    RETRY_DELAY = 1  # 重试延迟（秒）

    def __init__(self, translation_service: BaseTranslationAdapter):
        self.translation_service = translation_service
        self.progress: Optional[TranslationProgress] = None
        os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
        self.logger = logging.getLogger(__name__)

    async def save_uploaded_file(self, file_content: bytes, original_filename: str) -> str:
        """Save uploaded EPUB file and return the saved path."""
        file_path = os.path.join(settings.UPLOAD_DIR, original_filename)
        async with aiofiles.open(file_path, 'wb') as f:
            await f.write(file_content)
        return file_path

    def _get_progress_path(self, file_path: str) -> str:
        """Get path for progress file."""
        return f"{file_path}.progress.json"

    async def _save_progress(self, epub_path: str, progress: TranslationProgress) -> None:
        """Save translation progress to a file."""
        progress_path = self._get_progress_path(epub_path)
        async with aiofiles.open(progress_path, 'w') as f:
            await f.write(json.dumps(progress.to_dict()))

    async def _load_progress(self, file_path: str) -> Optional[TranslationProgress]:
        """Load translation progress from file."""
        progress_path = self._get_progress_path(file_path)
        try:
            async with aiofiles.open(progress_path, 'r') as f:
                data = json.loads(await f.read())
                return TranslationProgress.from_dict(data)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _clear_progress(self, epub_path: str) -> None:
        """Clear translation progress file."""
        progress_path = self._get_progress_path(epub_path)
        try:
            os.remove(progress_path)
        except FileNotFoundError:
            pass

    async def _translate_with_retry(
        self,
        text: str,
        src_lang: str,
        dest_lang: str,
        retry_count: int = 3,
        base_delay: float = 1.0,
    ) -> Tuple[Optional[str], Optional[TranslationError], Optional[float]]:
        """
        Translate text with retry mechanism.
        
        Args:
            text: Text to translate
            src_lang: Source language
            dest_lang: Target language
            retry_count: Number of retries
            base_delay: Base delay for exponential backoff
            
        Returns:
            Tuple of (translated text, error, translation cost)
        """
        if not text.strip():
            return text, None, 0.0

        last_error = None
        for attempt in range(retry_count):
            try:
                delay = base_delay * (2 ** attempt)  # Exponential backoff
                if attempt > 0:
                    await asyncio.sleep(delay)
                
                request = TranslationRequest(
                    text=text,
                    source_language=src_lang,
                    target_language=dest_lang
                )
                response = await self.translation_service.translate_text(request)
                cost = await self.translation_service.get_translation_cost(text, src_lang, dest_lang)
                
                if response and response.translated_text:
                    return response.translated_text, None, cost
                
            except Exception as e:
                error = TranslationError(
                    error_type="translation_error",
                    message=str(e),
                    timestamp=datetime.now(),
                    fragment=text,
                    retry_count=attempt + 1,
                )
                logging.error(f"Translation error (attempt {attempt + 1}/{retry_count}): {str(e)}")
                last_error = error
                
                # 添加详细的日志记录
                logging.debug(f"Progress object exists: {hasattr(self, 'progress')}")
                if hasattr(self, 'progress'):
                    logging.debug(f"Progress is not None: {self.progress is not None}")
                    logging.debug(f"Current chapter exists: {self.progress.current_chapter is not None}")
                    if self.progress and self.progress.current_chapter:
                        logging.debug(f"Current chapter ID: {self.progress.current_chapter.chapter_id}")
                        logging.debug(f"Current chapter errors before: {len(self.progress.current_chapter.errors)}")
                
                # 修改错误记录逻辑
                if hasattr(self, 'progress') and self.progress and self.progress.current_chapter:
                    self.progress.current_chapter.add_error(
                        error_type="translation_error",
                        message=str(e),
                        fragment=text,
                        retry_count=attempt + 1
                    )
                    logging.debug(f"Current chapter errors after: {len(self.progress.current_chapter.errors)}")
                    logging.debug(f"Total errors across all chapters: {self.progress.total_errors}")
                
                if attempt == retry_count - 1:  # Last attempt
                    return None, error, 0.0
                
        return None, last_error, 0.0  # Should never reach here

    async def _process_chapter(
        self,
        chapter_id: str,
        content: str,
        source_language: str,
        target_language: str,
    ) -> Tuple[str, List[TranslationError]]:
        """
        Process a single chapter for translation.
        
        Args:
            chapter_id: Chapter ID
            content: Chapter content
            source_language: Source language
            target_language: Target language
            
        Returns:
            Tuple of (translated content, list of errors)
        """
        errors = []
        soup = BeautifulSoup(content, 'html.parser')
        text_nodes = soup.find_all(text=True)
        
        self.logger.debug(f"Processing chapter {chapter_id} with {len(text_nodes)} text nodes")
        
        # Initialize chapter progress
        if chapter_id not in self.progress.chapters_progress:
            self.progress.chapters_progress[chapter_id] = ChapterProgress(
                chapter_id=chapter_id,
                title="",
                total_fragments=len(text_nodes),
                completed_fragments=0,
                word_count=0,
                character_count=0,
                translation_cost=0.0,
                is_completed=False,
                start_time=datetime.now()
            )
        chapter = self.progress.chapters_progress[chapter_id]
        self.progress.current_chapter = chapter
        
        # HTML structural tags that should be skipped
        structural_tags = {
            'html', 'head', 'body', 'article', 'section', 'nav', 'aside',
            'header', 'footer', 'main', 'figure', 'figcaption'
        }
        
        # Process each text node
        for node in text_nodes:
            # Log node information
            text = node.string.strip() if node.string else ""
            parent_name = node.parent.name if node.parent else "None"
            self.logger.debug(f"Processing node: text='{text}', parent={parent_name}")

            # Skip translation for special nodes
            if node.parent and (
                node.parent.name in ['script', 'style', 'code', 'sup'] or
                node.parent.name in structural_tags
            ):
                self.logger.debug(f"Skipping node due to parent tag: {parent_name}")
                chapter.completed_fragments += 1
                continue

            # Skip XML declarations and DOCTYPE
            if (text.startswith('<?xml') or
                text.startswith('<!DOCTYPE') or
                'xml version' in text or
                (node.parent and node.parent.name == 'xml')):
                self.logger.debug(f"Skipping XML/DOCTYPE node: {text}")
                chapter.completed_fragments += 1
                continue

            # Count this fragment regardless of whether it has content
            chapter.completed_fragments += 1
            
            if text:
                self.logger.debug(f"Translating text: '{text}'")
                translated_text, error, cost = await self._translate_with_retry(
                    text,
                    source_language,
                    target_language
                )
                
                if error:
                    self.logger.error(f"Translation error: {error.message} for text: '{text}'")
                    errors.append(error)
                    # 确保错误被添加到章节中
                    chapter.add_error(
                        error_type=error.error_type,
                        message=error.message,
                        fragment=error.fragment,
                        retry_count=error.retry_count
                    )
                
                if translated_text:
                    self.logger.debug(f"Translated text: '{text}' -> '{translated_text}'")
                    node.replace_with(translated_text)
                    # Update statistics
                    chapter.word_count += len(text.split())
                    chapter.character_count += len(text)
                    chapter.translation_cost += cost
                    
                    # Update overall statistics
                    self.progress.total_word_count += len(text.split())
                    self.progress.total_character_count += len(text)
                    self.progress.total_translation_cost += cost
        
        # Update chapter completion status
        if not chapter.is_completed and chapter.completed_fragments >= chapter.total_fragments:
            chapter.is_completed = True
            chapter.end_time = datetime.now()
            self.progress.completed_chapters += 1

        self.logger.debug(f"Finished processing chapter {chapter_id}")
        return str(soup), errors

    @staticmethod
    def validate_epub(file_path: str) -> Tuple[bool, Optional[str]]:
        """Validate EPUB file and return (is_valid, error_message)."""
        try:
            book = epub.read_epub(file_path)
            if not book.get_items():
                return False, "EPUB file is empty"
            return True, None
        except Exception as e:
            return False, f"Invalid EPUB file: {str(e)}"

    async def translate_epub(
        self,
        epub_path: str,
        source_language: str,
        target_language: str,
        progress_callback: Optional[Callable[[TranslationProgress], Awaitable[None]]] = None,
        resume: bool = False
    ) -> str:
        """
        Translate an EPUB file.
        
        Args:
            epub_path: Path to EPUB file
            source_language: Source language code
            target_language: Target language code
            progress_callback: Optional callback for progress updates
            resume: If True, attempt to resume from previous progress
            
        Returns:
            Path to translated EPUB file
        """
        # Validate language pair
        if not await self.translation_service.validate_languages(source_language, target_language):
            raise ValueError(f"Unsupported language pair: {source_language} -> {target_language}")

        # Validate EPUB file
        is_valid, error_message = self.validate_epub(epub_path)
        if not is_valid:
            raise ValueError(f"Invalid EPUB file: {error_message}")

        # Load or create progress
        if resume:
            saved_progress = await self._load_progress(epub_path)
            if saved_progress:
                self.progress = saved_progress
                self.logger.info(f"Resuming translation from {self.progress.completed_chapters}/{self.progress.total_chapters} chapters")

        if not self.progress:
            book = epub.read_epub(epub_path)
            doc_items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))
            self.progress = TranslationProgress(
                total_chapters=len(doc_items),
                completed_chapters=0,
                total_word_count=0,
                total_character_count=0,
                total_translation_cost=0.0,
                start_time=datetime.now()
            )

        try:
            # Create translated EPUB
            book = epub.read_epub(epub_path)
            translated_book = epub.EpubBook()

            # Copy metadata
            translated_book.metadata = {}
            for ns_name, values in book.metadata.items():
                translated_book.metadata[ns_name] = {}
                for name, items in values.items():
                    if ns_name == 'http://purl.org/dc/elements/1.1/':
                        if name == 'language':
                            # Update language metadata
                            translated_book.metadata[ns_name][name] = [(target_language, {})]
                        elif name == 'title':
                            # Translate title
                            original_title = items[0][0]
                            translated_title, _, _ = await self._translate_with_retry(
                                original_title,
                                source_language,
                                target_language
                            )
                            if translated_title:
                                translated_book.metadata[ns_name][name] = [(translated_title, items[0][1])]
                            else:
                                translated_book.metadata[ns_name][name] = items
                        else:
                            translated_book.metadata[ns_name][name] = items
                    else:
                        translated_book.metadata[ns_name][name] = items

            # Copy spine
            spine = []

            # Process each item
            for item in book.get_items():
                if isinstance(item, epub.EpubHtml):
                    chapter_id = item.id

                    # Skip if already translated
                    if chapter_id in self.progress.chapters_progress:
                        chapter = self.progress.chapters_progress[chapter_id]
                        if chapter.is_completed:
                            translated_book.add_item(item)
                            spine.append(item)
                            continue

                    # Get content and check if it's empty
                    content = item.get_content()
                    if isinstance(content, bytes):
                        content = content.decode('utf-8')
                    if not content or not content.strip():
                        self.logger.warning(f"Empty content in chapter {chapter_id}, skipping translation")
                        
                        # Initialize progress for empty chapter
                        if chapter_id not in self.progress.chapters_progress:
                            self.progress.chapters_progress[chapter_id] = ChapterProgress(
                                chapter_id=chapter_id,
                                title="",
                                total_fragments=0,
                                completed_fragments=0,
                                word_count=0,
                                character_count=0,
                                translation_cost=0.0,
                                is_completed=True,
                                start_time=datetime.now(),
                                end_time=datetime.now()
                            )
                            self.progress.completed_chapters += 1
                            
                        translated_book.add_item(item)
                        spine.append(item)
                        continue

                    # Process chapter
                    translated_content, errors = await self._process_chapter(
                        chapter_id,
                        content,
                        source_language,
                        target_language
                    )
                    
                    # Update item with translated content
                    if isinstance(translated_content, str):
                        translated_content = translated_content.encode('utf-8')
                    item.set_content(translated_content)

                    translated_book.add_item(item)
                    spine.append(item)

                    # Save progress
                    await self._save_progress(epub_path, self.progress)

                    # Notify progress
                    if progress_callback:
                        await progress_callback(self.progress)

                else:
                    # Copy non-HTML items as is
                    translated_book.add_item(item)
                    if isinstance(item, epub.EpubHtml):
                        spine.append(item)

            # Set spine
            translated_book.spine = spine

            # Save translated EPUB
            translated_path = self._get_translated_path(epub_path)
            epub.write_epub(translated_path, translated_book)

            # Update completion status and save final progress
            if self.progress.completed_chapters == self.progress.total_chapters:
                self.progress.end_time = datetime.now()
                await self._save_progress(epub_path, self.progress)
                if progress_callback:
                    await progress_callback(self.progress)

            return translated_path

        except Exception as e:
            self.logger.error(f"Translation error: {str(e)}")
            raise

    def _get_translated_path(self, epub_path: str) -> str:
        """Get path for translated EPUB file."""
        return f"{epub_path}.translated.epub"
