"""EPUB writing module."""

import asyncio
import os
import shutil
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Union

import ebooklib
from bs4 import BeautifulSoup, Tag
from ebooklib import epub

from app.core.logging import services_logger as logger
from app.models.translation_chunk import TranslationChunk
from app.models.translation_project import TranslationProject

logger = logger.bind(service="writer")


class EPUBWriter:
    """EPUB writer for creating translated documents."""

    def __init__(self, translation_project: TranslationProject):
        """Initialize the EPUB writer with a translation project."""
        self.translation_project = translation_project
        self.source_epub = epub.read_epub(translation_project.source_epub_path)
        self.target_epub = epub.EpubBook()
        self._temp_files = []
        self._resources = {}
        self._initialize_target_epub()
        self._initialize_resource_tracking()

    def _initialize_resource_tracking(self):
        """Initialize resource tracking."""
        self._resources = {
            "images": [],
            "styles": [],
            "fonts": [],
            "scripts": [],
        }

        # Track resources from source EPUB
        for item in self.source_epub.get_items():
            media_type = item.media_type
            if media_type:
                if media_type.startswith("image/"):
                    self._resources["images"].append(item)
                elif media_type == "text/css":
                    self._resources["styles"].append(item)
                elif media_type in ["application/x-font-ttf", "application/x-font-otf"]:
                    self._resources["fonts"].append(item)
                elif media_type in ["application/javascript", "text/javascript"]:
                    self._resources["scripts"].append(item)

    async def write_translated_epub(self, output_path: str) -> str:
        """Write the translated EPUB to the specified path with resource management.

        Args:
            output_path: Path where the translated EPUB should be saved

        Returns:
            Path to the written EPUB file
        """
        temp_dir = None
        try:
            # Create temporary directory for processing
            temp_dir = os.path.join(
                os.path.dirname(output_path), f".temp_{uuid.uuid4()}"
            )
            os.makedirs(temp_dir, exist_ok=True)
            self._temp_files.append(temp_dir)

            # Process each document with resource handling
            for item in self.source_epub.get_items():
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    # Process HTML document
                    translated_item = await self._process_document(item, temp_dir)
                    self.target_epub.add_item(translated_item)
                else:
                    # Handle resources
                    processed_item = await self._process_resource(item, temp_dir)
                    if processed_item:
                        self.target_epub.add_item(processed_item)

            # Update TOC with resource references
            self.update_toc()

            # Write the EPUB file
            epub.write_epub(output_path, self.target_epub)
            logger.info(f"Successfully wrote translated EPUB to {output_path}")

            return output_path

        except Exception as e:
            logger.error(f"Error writing translated EPUB: {e}")
            raise

        finally:
            # Clean up resources
            await self.cleanup()

    async def _process_resource(
        self, item: epub.EpubItem, temp_dir: str
    ) -> Optional[epub.EpubItem]:
        """Process a resource item (image, style, font, etc.).

        Args:
            item: Resource item to process
            temp_dir: Temporary directory for processing

        Returns:
            Processed resource item or None if should be skipped
        """
        try:
            # Skip if resource is not tracked
            if not self._is_tracked_resource(item):
                return None

            # Create temporary file for resource
            temp_path = os.path.join(temp_dir, item.file_name)
            os.makedirs(os.path.dirname(temp_path), exist_ok=True)
            self._temp_files.append(temp_path)

            # Write resource content to temporary file
            with open(temp_path, "wb") as f:
                f.write(item.get_content())

            # Create new resource item
            new_item = epub.EpubItem(
                uid=item.id,
                file_name=item.file_name,
                media_type=item.media_type,
                content=item.get_content(),
            )

            return new_item

        except Exception as e:
            logger.error(f"Error processing resource {item.file_name}: {e}")
            return None

    def _is_tracked_resource(self, item: epub.EpubItem) -> bool:
        """Check if a resource is being tracked."""
        if not item.media_type:
            return False

        return any(item in resources for resources in self._resources.values())

    async def cleanup(self):
        """Clean up temporary files and resources."""
        for path in self._temp_files:
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                elif os.path.isfile(path):
                    os.remove(path)
            except Exception as e:
                logger.error(f"Error cleaning up {path}: {e}")

        self._temp_files.clear()
        self._resources.clear()

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit with cleanup."""
        await self.cleanup()

    def _initialize_target_epub(self):
        """Initialize target EPUB with metadata and structure from source."""
        # Copy metadata
        for key, value in self.source_epub.metadata.items():
            if key != "language":  # Don't copy language, will set target language
                setattr(self.target_epub.metadata, key, value)

        # Set target language
        self.target_epub.set_language(self.translation_project.target_language)

        # Copy spine
        self.target_epub.spine = self.source_epub.spine

        # Copy guide
        self.target_epub.guide = self.source_epub.guide

        logger.debug("Initialized target EPUB with metadata and structure")

    async def _process_document(
        self, item: epub.EpubItem, temp_dir: str
    ) -> epub.EpubItem:
        """Process a single document, replacing content with translations."""
        try:
            # Get chunks for this document
            chunks = TranslationChunk.query.filter_by(
                translation_project_id=self.translation_project.id,
                source_file=item.file_name,
            ).all()

            if not chunks:
                logger.warning(f"No translation chunks found for {item.file_name}")
                return item

            # Create new document with same properties
            new_item = epub.EpubItem(
                uid=item.id,
                file_name=item.file_name,
                media_type=item.media_type,
                content=item.get_content(),
            )

            # Parse content
            soup = BeautifulSoup(new_item.get_content(), "html.parser")

            # Replace translated chunks
            for chunk in chunks:
                if chunk.status == "completed":
                    self._replace_chunk_content(soup, chunk)

            # Update content
            new_item.content = str(soup).encode()

            return new_item

        except Exception as e:
            logger.error(f"Error processing document {item.file_name}: {e}")
            raise

    def _replace_chunk_content(self, soup: BeautifulSoup, chunk: TranslationChunk):
        """Replace content of a specific chunk in the document."""
        try:
            # Find the element using the chunk's selector
            element = soup.select_one(chunk.selector)
            if not element:
                logger.warning(f"Could not find element for selector {chunk.selector}")
                return

            if isinstance(element, Tag):
                # Replace content while preserving HTML structure
                new_content = BeautifulSoup(chunk.translated_content, "html.parser")
                element.clear()
                element.append(new_content)
            else:
                # Direct text replacement
                element.string = chunk.translated_content

        except Exception as e:
            logger.error(f"Error replacing chunk content: {e}")
            raise

    def update_toc(self):
        """Update table of contents with translated titles."""
        try:
            # Get translated TOC items
            toc_chunks = TranslationChunk.query.filter_by(
                translation_project_id=self.translation_project.id, content_type="toc"
            ).all()

            if not toc_chunks:
                logger.warning("No translated TOC items found")
                return

            # Create translation lookup
            translations = {
                chunk.source_content: chunk.translated_content
                for chunk in toc_chunks
                if chunk.status == "completed"
            }

            # Update TOC
            new_toc = []
            for toc_item in self.source_epub.toc:
                if isinstance(toc_item, epub.Link):
                    title = translations.get(toc_item.title, toc_item.title)
                    new_toc.append(epub.Link(toc_item.href, title, toc_item.uid))
                else:
                    # Handle nested TOC items
                    new_toc.append(toc_item)

            self.target_epub.toc = new_toc
            logger.debug("Updated table of contents with translations")

        except Exception as e:
            logger.error(f"Error updating TOC: {e}")
            raise
