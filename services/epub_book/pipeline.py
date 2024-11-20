import asyncio
from pathlib import Path
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
import html
import re
from bs4 import BeautifulSoup
from ebooklib import epub
import ebooklib
from rich.progress import Progress, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
from rich.console import Console

from utils.logging import get_logger
from .translator import Translator, TranslationConfig, TranslationProvider

logger = get_logger(__name__)
console = Console()

class PipelineConfig(BaseModel):
    """Configuration for the EPUB translation pipeline."""
    max_concurrent_requests: int
    output_dir: Path
    translation_provider: str = "OPENAI"
    api_key: Optional[str] = None
    model: Optional[str] = None
    source_lang: str = "en"
    target_lang: str = "zh"
    max_chars: int = 1200
    separator: str = "\n"

class EPUBHandler:
    """Handles EPUB file operations and structure."""
    
    def __init__(self, config: PipelineConfig):
        self.config = config

    def prepare_output_file(self, epub_path: Path) -> Path:
        """Prepare the output file path and copy the original EPUB."""
        output_path = self.config.output_dir / epub_path.name
        shutil.copy(str(epub_path), str(output_path))
        return output_path

    @staticmethod
    def extract_chapters(book: epub.EpubBook) -> List[epub.EpubItem]:
        """Extract all document chapters from the EPUB book."""
        return [item for item in book.get_items() if item.get_type() == ebooklib.ITEM_DOCUMENT]

    @staticmethod
    def get_toc_item(book: epub.EpubBook, toc_file_name: str) -> Optional[epub.EpubItem]:
        """Get the table of contents item from the EPUB book."""
        return next(
            (item for item in book.get_items() if toc_file_name in item.get_name().lower()),
            None
        )


class ContentProcessor:
    """Processes and translates EPUB content."""

    # Pattern to match HTML entities and tags
    HTML_ENTITY_PATTERN = re.compile(r'&[a-zA-Z]+;|&#[0-9]+;|&lt;[^&]+&gt;')
    
    def __init__(self, config: PipelineConfig, translator: Translator):
        self.config = config
        self.translator = translator
        self.semaphore = asyncio.Semaphore(config.max_concurrent_requests)

    def _preserve_entities(self, text: str) -> Tuple[str, List[str]]:
        """
        Replace HTML entities with placeholders and return mapping.
        Returns:
            Tuple of (text with placeholders, list of original entities)
        """
        entities = []
        
        def replace(match):
            entity = match.group(0)
            placeholder = f"__ENTITY_{len(entities)}__"
            entities.append(entity)
            return placeholder
        
        processed_text = self.HTML_ENTITY_PATTERN.sub(replace, text)
        return processed_text, entities

    def _restore_entities(self, text: str, entities: List[str]) -> str:
        """Restore HTML entities from placeholders."""
        for i, entity in enumerate(entities):
            text = text.replace(f"__ENTITY_{i}__", entity)
        return text

    def extract_elements(self, soup: BeautifulSoup) -> Tuple[List[str], List[BeautifulSoup], List[List[str]]]:
        """Extract translatable elements from BeautifulSoup object."""
        contents, elements, all_entities = [], [], []
        for tag in self.config.translate_tags:
            for element in soup.find_all(tag):
                content = "".join(str(child) for child in element.contents)
                # Preserve HTML entities before translation
                processed_content, entities = self._preserve_entities(content)
                contents.append(processed_content)
                elements.append(element)
                all_entities.append(entities)
        return contents, elements, all_entities

    async def translate_contents(self, contents: List[str], all_entities: List[List[str]]) -> List[str]:
        """Translate a list of content strings."""
        async def translate_with_retry(content: str, entities: List[str], max_retries: int = 3) -> str:
            for attempt in range(max_retries):
                try:
                    async with self.semaphore:
                        translated = await self.translator.translate(content)
                        # Restore HTML entities after translation
                        return self._restore_entities(translated, entities)
                except Exception as e:
                    if attempt == max_retries - 1:
                        logger.error(f"Failed to translate after {max_retries} attempts: {e}")
                        return self._restore_entities(content, entities)
                    await asyncio.sleep(1 * (attempt + 1))

        tasks = [translate_with_retry(content, entities) 
                for content, entities in zip(contents, all_entities)]
        return await asyncio.gather(*tasks)

    @staticmethod
    def update_elements(elements: List[BeautifulSoup], contents: List[str]) -> None:
        """Update BeautifulSoup elements with translated content."""
        for element, content in zip(elements, contents):
            element.clear()
            element.append(BeautifulSoup(content, 'html.parser'))


class TranslationPipeline:
    """Main pipeline for translating EPUB books."""

    def __init__(self, config: PipelineConfig, progress_manager: ProgressManager, translator: Translator):
        self.config = config
        self.progress_manager = progress_manager
        self.epub_handler = EPUBHandler(config)
        self.content_processor = ContentProcessor(config, translator)
        self.total_texts = 0
        self.translated_texts = 0

    def _extract_text_from_html(self, html_content: str) -> str:
        """Extract text content from HTML."""
        soup = BeautifulSoup(html_content, 'html.parser')
        return soup.get_text()

    def _get_translatable_items(self, book: epub.EpubBook) -> List[Dict[str, Any]]:
        """Get all translatable items from the book."""
        items = []
        
        # Process spine items (main content)
        for item in book.spine:
            if isinstance(item, epub.Link):
                item = book.get_item_with_id(item.id)
            if isinstance(item, epub.EpubHtml):
                items.append({
                    'type': 'content',
                    'item': item,
                    'content': item.content.decode('utf-8') if item.content else ''
                })
        
        # Process navigation
        if book.toc:
            items.append({
                'type': 'toc',
                'item': book.toc,
                'content': str(book.toc)
            })
        
        return items

    async def process_epub(self, epub_path: Path) -> None:
        """Process an EPUB file, translating its content chapter by chapter."""
        try:
            output_path = self.epub_handler.prepare_output_file(epub_path)
            book = epub.read_epub(str(output_path))

            items = self._get_translatable_items(book)
            
            # Count total texts for progress tracking
            self.total_texts = sum(len(re.findall(r'<p[^>]*>.*?</p>', item['content'], re.DOTALL)) 
                                  for item in items if item['type'] == 'content')
            
            logger.info("starting_book_translation", 
                       book_title=book.title, 
                       total_texts=self.total_texts,
                       source_lang=self.config.source_lang,
                       target_lang=self.config.target_lang)
            
            with Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TimeRemainingColumn(),
                console=console,
                transient=True
            ) as progress:
                translation_task = progress.add_task(
                    f"[cyan]Translating {book.title}...",
                    total=self.total_texts
                )
                
                # Process each item
                for item in items:
                    if item['type'] == 'content':
                        item['content'] = await self._translate_content(
                            item['content'],
                            progress,
                            translation_task
                        )
                        if isinstance(item['item'], epub.EpubHtml):
                            item['item'].content = item['content'].encode('utf-8')
                    elif item['type'] == 'toc':
                        # Handle table of contents translation
                        pass
            
            # Save the translated book
            final_output_path = output_path.with_name(output_path.stem + '_translated.epub')
            epub.write_epub(str(final_output_path), book)
            
            logger.info("book_translation_complete", 
                       book_title=book.title,
                       output_path=str(final_output_path))
            
        except Exception as e:
            logger.error(f"Error processing EPUB {epub_path}: {e}")
            raise

    async def _translate_content(self, content: str, progress: Progress, task_id: int) -> str:
        """Translate HTML content while updating progress."""
        soup = BeautifulSoup(content, 'html.parser')
        paragraphs = soup.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
        
        for p in paragraphs:
            if p.string and p.string.strip():
                translated_text = await self.content_processor.translate_contents([str(p.string)], [[]])[0]
                p.string = translated_text
                progress.advance(task_id)
        
        return str(soup)

    async def _translate_batch(self, texts: List[str], progress: Progress, task_id: int) -> List[str]:
        """Translate a batch of texts while updating progress."""
        translations = await self.content_processor.translate_contents(texts, [[] for _ in texts])
        progress.advance(task_id, len(texts))
        return translations


async def main():
    """Example usage of the translation pipeline."""
    # Create output directory in the current working directory
    output_dir = Path("translated_books")
    output_dir.mkdir(exist_ok=True)
    
    config = PipelineConfig(
        max_concurrent_requests=5,
        output_dir=output_dir,
        translation_provider="GOOGLE",
        source_lang="en",  # English source
        target_lang="zh",  # Chinese target
        max_chars=1200,
        separator="###"
    )
    
    # Create translation config from pipeline config
    translation_config = TranslationConfig(
        provider=config.translation_provider,
        source_lang=config.source_lang,
        target_lang=config.target_lang,
        max_chars=config.max_chars,
        separator=config.separator,
    )
    
    progress_manager = ProgressManager()
    translator = Translator(translation_config)
    pipeline = TranslationPipeline(config, progress_manager, translator)

    # Use the smaller EPUB file for testing
    epub_path = Path("AI-Explained.epub")
    if not epub_path.exists():
        print(f"EPUB file not found: {epub_path}")
        print("Please make sure the EPUB file exists in the current directory")
        return
    
    print(f"Starting translation of {epub_path}")
    print(f"Output will be saved to: {output_dir / epub_path.name}")
    
    try:
        await pipeline.process_epub(epub_path)
        print(f"Translation completed successfully! Output saved to: {output_dir / epub_path.name}")
    except Exception as e:
        print(f"Error during translation: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
