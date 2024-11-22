import os
from typing import List, Dict, Optional, Tuple
from bs4 import BeautifulSoup
import ebooklib
from ebooklib import epub
import aiofiles
import hashlib
from datetime import datetime

from app.core.config import settings
from app.services.translation.base import BaseTranslationAdapter, TranslationRequest

class EPUBProcessor:
    def __init__(self, translation_service: BaseTranslationAdapter):
        self.translation_service = translation_service
        os.makedirs(settings.UPLOAD_DIR, exist_ok=True)

    async def save_uploaded_file(self, file_content: bytes, original_filename: str) -> str:
        """Save uploaded EPUB file and return the saved path."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_hash = hashlib.md5(file_content).hexdigest()[:10]
        filename = f"{timestamp}_{file_hash}_{original_filename}"
        file_path = os.path.join(settings.UPLOAD_DIR, filename)
        
        async with aiofiles.open(file_path, 'wb') as f:
            await f.write(file_content)
        return file_path

    async def translate_epub(
        self,
        file_path: str,
        source_language: str,
        target_language: str,
        progress_callback: Optional[callable] = None
    ) -> str:
        """Process EPUB file and return path to translated file."""
        # Load EPUB
        book = epub.read_epub(file_path)
        
        # Create new EPUB for translation
        translated_book = epub.EpubBook()
        
        # Copy metadata
        identifier = book.get_metadata('DC', 'identifier')[0][0] if book.get_metadata('DC', 'identifier') else 'unknown'
        title = book.get_metadata('DC', 'title')[0][0] if book.get_metadata('DC', 'title') else 'Untitled'
        translated_book.set_identifier(identifier)
        translated_book.set_title(f"{title} (Translated)")
        translated_book.set_language(target_language)
        
        # Process items
        items = list(book.get_items())
        total_items = len(items)
        processed_items = 0
        spine = []
        
        for item in items:
            if isinstance(item, epub.EpubHtml):
                # Process HTML content
                soup = BeautifulSoup(item.content, 'html.parser')
                text_nodes = []
                
                # Extract text nodes
                for text in soup.stripped_strings:
                    if text.strip():
                        text_nodes.append(text)
                
                if text_nodes:
                    # Create translation requests
                    requests = [
                        TranslationRequest(
                            text=text,
                            source_language=source_language,
                            target_language=target_language
                        )
                        for text in text_nodes
                    ]
                    
                    # Translate text nodes in batch
                    responses = await self.translation_service.translate_batch(requests)
                    translated_texts = [response.translated_text for response in responses]
                    
                    # Replace text nodes with translations
                    for original, translated in zip(text_nodes, translated_texts):
                        for elem in soup.find_all(string=original):
                            elem.replace_with(translated)
                
                # Create new item with translated content
                new_item = epub.EpubHtml(
                    title=item.get_name(),
                    file_name=item.get_name(),
                    content=str(soup)
                )
                translated_book.add_item(new_item)
                spine.append(new_item)
            else:
                # Copy non-HTML items as is
                translated_book.add_item(item)
            
            processed_items += 1
            if progress_callback:
                progress = (processed_items / total_items) * 100
                await progress_callback(progress)
        
        # Set spine and create translated file
        translated_book.spine = spine
        
        # Generate output filename
        output_filename = os.path.splitext(os.path.basename(file_path))[0]
        output_filename = f"{output_filename}_translated.epub"
        output_path = os.path.join(settings.UPLOAD_DIR, output_filename)
        
        # Save translated EPUB
        epub.write_epub(output_path, translated_book)
        
        return output_path

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
