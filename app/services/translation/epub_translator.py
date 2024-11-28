"""EPUB translation service."""

import asyncio
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

import ebooklib
from bs4 import BeautifulSoup
from ebooklib import epub
from lxml import etree

from app.core.config import settings
from app.core.logging import get_logger
from app.models.translation_chunk import TranslationChunk
from app.models.translation_project import TranslationProject
from app.services.epub import HTMLSplitter

from .base import BaseTranslator
from .factory import create_translator

logger = get_logger(__name__)


class EPUBTranslator:
    """Service for translating EPUB files while preserving formatting."""

    def __init__(
        self,
        translator: BaseTranslator,
        source_lang: str,
        target_lang: str,
        preserve_formatting: bool = True,
        project_id: Optional[int] = None,
    ):
        """Initialize EPUB translator."""
        self.translator = translator
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.preserve_formatting = preserve_formatting
        self.html_splitter = HTMLSplitter()
        self.validation_errors = []
        self.project_id = project_id or 0  # Use 0 as default for testing

    async def translate_epub(
        self,
        input_path: str,
        output_path: str,
        progress_callback: Optional[callable] = None,
        temp_storage: Optional[Dict] = None,
    ) -> Dict:
        """Translate an EPUB file with validation.

        Args:
            input_path: Path to input EPUB file
            output_path: Path to save translated EPUB
            progress_callback: Optional callback for progress updates
            temp_storage: Optional temporary storage for recovery

        Returns:
            Dict containing translation statistics
        """
        # Pre-translation validation
        await self._validate_input_epub(input_path)
        if any(error["level"] == "critical" for error in self.validation_errors):
            raise ValueError(
                next(
                    error["message"]
                    for error in self.validation_errors
                    if error["level"] == "critical"
                )
            )

        # Initialize or restore progress
        stats = (
            temp_storage.get("stats", {})
            if temp_storage
            else {
                "total_items": 0,
                "translated_items": 0,
                "total_words": 0,
                "translated_words": 0,
                "skipped_items": 0,
                "progress": 0,
                "current_item": None,
                "validation_errors": [],
            }
        )

        # Read input EPUB
        book = epub.read_epub(input_path)

        # Count total items and words first
        if not temp_storage:
            for item in book.get_items():
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    stats["total_items"] += 1
                    content = item.get_content().decode("utf-8")
                    soup = BeautifulSoup(content, "html.parser")
                    stats["total_words"] += len(soup.get_text().split())

        # Process each document
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                try:
                    # Skip already translated items if resuming
                    if temp_storage and item.get_name() in temp_storage.get(
                        "translated_items", []
                    ):
                        stats["translated_items"] += 1
                        content = item.get_content().decode("utf-8")
                        soup = BeautifulSoup(content, "html.parser")
                        stats["translated_words"] += len(soup.get_text().split())
                        continue

                    # Update current item
                    stats["current_item"] = item.get_name()
                    print(f"\nProcessing item: {item.get_name()}")

                    # Get HTML content
                    content = item.get_content().decode("utf-8")
                    soup = BeautifulSoup(content, "html.parser")
                    current_words = len(soup.get_text().split())

                    # Pre-translation content validation
                    print("Before validation clear:", self.validation_errors)
                    self.validation_errors.clear()
                    await self._validate_content(content)
                    print("After validation:", self.validation_errors)

                    # Add validation errors to stats but only skip on errors
                    if self.validation_errors:
                        stats["validation_errors"].extend(self.validation_errors)
                        print("Stats validation errors:", stats["validation_errors"])
                        if any(
                            error["level"] == "error"
                            for error in self.validation_errors
                        ):
                            stats["skipped_items"] += 1
                            print("Skipping due to validation errors")
                            continue

                    # Translate content
                    translated_content = await self._translate_html_content(
                        content, source_file=item.get_name()
                    )

                    # Post-translation validation
                    self.validation_errors.clear()
                    await self._validate_translation(content, translated_content)

                    # Add validation errors to stats but only skip on errors
                    if self.validation_errors:
                        stats["validation_errors"].extend(self.validation_errors)
                        if any(
                            error["level"] == "error"
                            for error in self.validation_errors
                        ):
                            stats["skipped_items"] += 1
                            continue

                    # Update item content
                    item.set_content(translated_content.encode("utf-8"))
                    stats["translated_items"] += 1
                    stats["translated_words"] += current_words

                    if temp_storage:
                        temp_storage["translated_items"].append(item.get_name())

                except Exception as e:
                    logger.error(f"Failed to translate item {item.get_name()}: {e}")
                    stats["skipped_items"] += 1
                    stats["validation_errors"].append(
                        {
                            "level": "error",
                            "message": f"Failed to translate {item.get_name()}: {str(e)}",
                            "type": "translation_error",
                        }
                    )
                    if temp_storage:
                        temp_storage["failed_items"].append(
                            {"item": item.get_name(), "error": str(e)}
                        )

                finally:
                    # Calculate progress based on total items
                    if stats["total_items"] > 0:
                        processed_items = stats["translated_items"]
                        stats["progress"] = min(
                            100, int((processed_items / stats["total_items"]) * 100)
                        )
                    if progress_callback:
                        progress_callback(stats)

        # Final validation
        self.validation_errors.clear()
        await self._validate_output_epub(book)
        if self.validation_errors:
            stats["validation_errors"].extend(self.validation_errors)

        # Save translated EPUB
        epub.write_epub(output_path, book)

        return stats

    async def _validate_input_epub(self, input_path: str) -> None:
        """Validate input EPUB file before translation.

        Args:
            input_path: Path to input EPUB file

        The validation results are stored in self.validation_errors
        """
        self.validation_errors.clear()  # Clear previous errors

        try:
            if not os.path.exists(input_path):
                self.validation_errors.append(
                    {
                        "level": "critical",
                        "message": f"Input file not found: {input_path}",
                        "type": "file_error",
                    }
                )
                return

            # Verify EPUB can be opened and parsed
            try:
                book = epub.read_epub(input_path)
            except Exception as e:
                self.validation_errors.append(
                    {
                        "level": "critical",
                        "message": f"Failed to read EPUB file: {str(e)}",
                        "type": "epub_error",
                    }
                )
                return

            if not book.get_items():
                self.validation_errors.append(
                    {
                        "level": "critical",
                        "message": "EPUB file is empty or corrupted",
                        "type": "epub_error",
                    }
                )
                return

            # Check content types
            for item in book.get_items():
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    try:
                        content = item.get_content().decode("utf-8")
                        BeautifulSoup(content, "html.parser")
                    except Exception as e:
                        self.validation_errors.append(
                            {
                                "level": "warning",
                                "message": f"Invalid HTML in {item.get_name()}: {str(e)}",
                                "type": "content_error",
                            }
                        )

        except Exception as e:
            self.validation_errors.append(
                {
                    "level": "critical",
                    "message": f"Failed to validate input EPUB: {str(e)}",
                    "type": "validation_error",
                }
            )

    async def _validate_content(self, content: str) -> None:
        """Validate HTML content before translation.

        Args:
            content: HTML content to validate
        """
        try:
            # First try parsing with html.parser
            soup = BeautifulSoup(content, "html.parser")

            # Check for required HTML structure
            if not soup.find("body"):
                self.validation_errors.append(
                    {
                        "level": "warning",
                        "message": "Missing body tag in HTML content",
                        "type": "structure_error",
                    }
                )

            # Check for script tags (potential security issue)
            if soup.find("script"):
                self.validation_errors.append(
                    {
                        "level": "warning",
                        "message": "Script tags found in content",
                        "type": "security_warning",
                    }
                )

            # Check if the structure is preserved
            original_content = content.replace(" ", "").replace("\n", "")
            parsed_content = str(soup).replace(" ", "").replace("\n", "")

            # If the parsed content is significantly different, we lost structure
            if len(parsed_content) != len(original_content):
                self.validation_errors.append(
                    {
                        "level": "warning",
                        "message": "HTML structure was auto-fixed during parsing",
                        "type": "content_warning",
                    }
                )

        except Exception as e:
            print(f"Validation exception: {e}")
            self.validation_errors.append(
                {
                    "level": "error",
                    "message": f"Content validation failed: {str(e)}",
                    "type": "validation_error",
                }
            )

    async def _validate_translation(self, original: str, translated: str) -> None:
        """Validate translation output.

        Args:
            original: Original HTML content
            translated: Translated HTML content
        """
        try:
            # Parse both contents
            original_soup = BeautifulSoup(original, "html.parser")
            translated_soup = BeautifulSoup(translated, "html.parser")

            # Compare structure
            if len(original_soup.find_all()) != len(translated_soup.find_all()):
                self.validation_errors.append(
                    {
                        "level": "error",
                        "message": "HTML structure mismatch between original and translation",
                        "type": "structure_error",
                    }
                )

            # Check for preserved formatting markers
            original_markers = re.findall(r"__[A-Z]+_\d+__", original)
            translated_markers = re.findall(r"__[A-Z]+_\d+__", translated)
            if set(original_markers) != set(translated_markers):
                self.validation_errors.append(
                    {
                        "level": "error",
                        "message": "Formatting markers not preserved in translation",
                        "type": "formatting_error",
                    }
                )

            # Check for preserved elements
            for tag in ["img", "a", "code", "pre"]:
                orig_count = len(original_soup.find_all(tag))
                trans_count = len(translated_soup.find_all(tag))
                if orig_count != trans_count:
                    self.validation_errors.append(
                        {
                            "level": "warning",
                            "message": f"Mismatch in {tag} elements: original={orig_count}, translated={trans_count}",
                            "type": "element_error",
                        }
                    )

        except Exception as e:
            self.validation_errors.append(
                {
                    "level": "error",
                    "message": f"Translation validation failed: {str(e)}",
                    "type": "validation_error",
                }
            )

    async def _validate_output_epub(self, book: epub.EpubBook) -> None:
        """Validate complete translated EPUB.

        Args:
            book: Translated EPUB book object
        """
        try:
            # Check spine integrity
            if not book.spine:
                self.validation_errors.append(
                    {
                        "level": "critical",
                        "message": "Missing spine in output EPUB",
                        "type": "structure_error",
                    }
                )

            # Check TOC integrity
            if not book.toc:
                self.validation_errors.append(
                    {
                        "level": "warning",
                        "message": "Missing table of contents in output EPUB",
                        "type": "structure_error",
                    }
                )

            # Check for missing resources
            resource_errors = []
            for item in book.get_items():
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    content = item.get_content().decode("utf-8")
                    soup = BeautifulSoup(content, "html.parser")

                    # Check for empty content
                    if not soup.get_text().strip():
                        self.validation_errors.append(
                            {
                                "level": "warning",
                                "message": f"Empty content in {item.get_name()}",
                                "type": "content_error",
                            }
                        )

                    # Check images
                    for img in soup.find_all("img"):
                        src = img.get("src", "")
                        if src and not any(
                            i.file_name == src for i in book.get_items()
                        ):
                            resource_errors.append(f"Missing image: {src}")

                    # Check stylesheets
                    for link in soup.find_all("link", rel="stylesheet"):
                        href = link.get("href", "")
                        if href and not any(
                            i.file_name == href for i in book.get_items()
                        ):
                            resource_errors.append(f"Missing stylesheet: {href}")

            if resource_errors:
                self.validation_errors.append(
                    {
                        "level": "warning",
                        "message": "Missing resources in output EPUB",
                        "type": "resource_error",
                        "details": resource_errors,
                    }
                )

        except Exception as e:
            self.validation_errors.append(
                {
                    "level": "critical",
                    "message": f"Output EPUB validation failed: {str(e)}",
                    "type": "validation_error",
                }
            )

    async def _translate_html_content(
        self, html_content: str, source_file: Optional[str] = None
    ) -> str:
        """Translate HTML content while preserving formatting.

        Args:
            html_content: HTML content to translate
            source_file: Optional source file name for tracking

        Returns:
            Translated HTML content
        """
        if self.preserve_formatting:
            # Split content into translatable parts
            parts = self.html_splitter.split_content(
                html_content,
                source_file=source_file or "test.xhtml",  # Use default for testing
                project_id=self.project_id,
            )

            # Extract translatable text
            texts_to_translate = []
            for part in parts:
                if part["type"] == "translatable":
                    texts_to_translate.append(part["content"])

            # Calculate total words for progress tracking
            total_words = sum(len(text.split()) for text in texts_to_translate)
            translated_words = 0

            # Translate texts in batches
            translated_texts = []
            batch_size = 10  # Adjust based on provider limits

            for i in range(0, len(texts_to_translate), batch_size):
                batch = texts_to_translate[i : i + batch_size]
                batch_translations = await self.translator.translate_batch(batch)

                # Validate batch translations
                if len(batch_translations) != len(batch):
                    raise ValueError(
                        f"Translation returned {len(batch_translations)} items but expected {len(batch)}"
                    )

                translated_texts.extend(batch_translations)

                # Update progress
                translated_words += sum(len(text.split()) for text in batch)
                progress = int((translated_words / total_words) * 100)

                logger.debug(
                    "Batch translation progress",
                    batch_progress=progress,
                    translated_words=translated_words,
                    total_words=total_words,
                )

            # Update parts with translations
            text_index = 0
            for part in parts:
                if part["type"] == "translatable":
                    part["content"] = translated_texts[text_index]
                    text_index += 1

            # Reassemble HTML
            return self.html_splitter.reassemble_content(parts, html_content)
        else:
            # Simple translation without preserving formatting
            soup = BeautifulSoup(html_content, "html.parser")
            text = soup.get_text()
            translated_text = await self.translator.translate_text(text)
            return f"<html><body>{translated_text}</body></html>"

    @classmethod
    async def create(
        cls,
        source_lang: str,
        target_lang: str,
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
        preserve_formatting: bool = True,
    ) -> "EPUBTranslator":
        """Create an EPUB translator instance.

        Args:
            source_lang: Source language code
            target_lang: Target language code
            provider: Translation provider to use
            api_key: Provider API key
            preserve_formatting: Whether to preserve HTML formatting

        Returns:
            EPUBTranslator instance
        """
        # Create translator instance
        translator = await create_translator(
            provider or "mistral",
            api_key or settings.MISTRAL_API_KEY,
            source_lang,
            target_lang,
        )

        return cls(translator, source_lang, target_lang, preserve_formatting)
