"""EPUB parsing module."""

import os
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union

import ebooklib
from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from ebooklib import epub

from app.core.logging import services_logger as logger
from app.models.translation_chunk import TranslationChunk
from app.models.translation_project import TranslationProject
from app.services.epub.splitter import HTMLSplitter

logger = logger.bind(service="paeser")


class EPUBParser:
    """Enhanced EPUB parser with advanced content analysis and chunk generation."""

    def __init__(self, epub_path: str):
        """Initialize the EPUB parser with a path to an EPUB file."""
        self.epub_path = epub_path
        self.book = None
        self._splitter = HTMLSplitter()
        self.metadata = {}
        self.chapters = []
        self.toc_items = []
        self._load_epub()

    def _load_epub(self):
        """Load and parse the EPUB file."""
        try:
            self.book = epub.read_epub(self.epub_path)
            self._extract_metadata()
            self._process_chapters()
            self._extract_toc()
        except Exception as e:
            logger.error(f"Failed to load EPUB file {self.epub_path}: {e}")
            raise

    def _extract_metadata(self):
        """Extract metadata from the EPUB file."""
        try:
            self.metadata = {}

            # Extract standard metadata fields
            metadata_fields = {
                "title": ("DC", "title"),
                "language": ("DC", "language"),
                "creator": ("DC", "creator"),
                "identifier": ("DC", "identifier"),
                "publisher": ("DC", "publisher"),
                "date": ("DC", "date"),
                "rights": ("DC", "rights"),
                "description": ("DC", "description"),
                "coverage": ("DC", "coverage"),
                "contributor": ("DC", "contributor"),
            }

            for field, (namespace, name) in metadata_fields.items():
                try:
                    value = self.book.get_metadata(namespace, name)
                    if value and len(value) > 0:
                        # Handle tuple or direct value
                        first_value = value[0]
                        if isinstance(first_value, tuple) and len(first_value) > 0:
                            self.metadata[field] = first_value[0]
                        else:
                            self.metadata[field] = first_value
                except Exception as e:
                    logger.error(f"Failed to extract metadata field {field}: {e}")
        except Exception as e:
            logger.error(f"Failed to extract metadata: {e}")
            raise

    def _process_chapters(self):
        """Process and analyze all chapters in the EPUB file."""
        try:
            for item in self.book.get_items():
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    soup = BeautifulSoup(item.get_content(), "lxml")

                    # Basic structure analysis without logging HTML content
                    structure = {
                        "headings": len(
                            soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
                        ),
                        "paragraphs": len(soup.find_all("p")),
                        "lists": len(soup.find_all(["ul", "ol"])),
                        "tables": len(soup.find_all("table")),
                        "images": len(soup.find_all("img")),
                        "links": len(soup.find_all("a")),
                    }

                    content_type = self._determine_content_type(structure)
                    word_count = len(soup.get_text(strip=True).split())

                    # Extract image information
                    images = []
                    for img in soup.find_all("img"):
                        image_info = {
                            "src": img.get("src", ""),
                            "alt": img.get("alt", ""),
                            "title": img.get("title", ""),
                        }
                        images.append(image_info)

                    chapter = {
                        "file_name": item.file_name,
                        "content_type": content_type,
                        "word_count": word_count,
                        "structure": structure,
                        "content": str(soup),
                        "images": images,
                    }
                    self.chapters.append(chapter)

                    logger.debug(
                        f"Processed chapter {item.file_name}: {content_type}, {word_count} words"
                    )
        except Exception as e:
            logger.error(f"Failed to process chapters: {e}")
            raise

    def _process_toc_item(self, item: Union[epub.Link, epub.Section, tuple]) -> Dict:
        """Process a single TOC item."""
        if isinstance(item, tuple):
            section, children = item
            result = {
                "title": section.title,
                "level": 1,  # Default level
                "href": section.href if hasattr(section, "href") else None,
                "children": [
                    self._process_toc_item(child)
                    for child in children
                    if child is not None
                ],
            }
            return result

        result = {
            "title": item.title,
            "level": 1,  # Default level
            "href": item.href if hasattr(item, "href") else None,
        }

        if isinstance(item, epub.Section):
            result["children"] = [
                self._process_toc_item(child)
                for child in item.links
                if child is not None
            ]

        return result

    def _extract_toc(self):
        """Extract table of contents from the EPUB file."""
        try:
            self.toc_items = [self._process_toc_item(item) for item in self.book.toc]
        except Exception as e:
            logger.error(f"Failed to extract TOC: {e}")
            raise

    def _analyze_chapter(self, item: epub.EpubItem) -> Dict:
        """Analyze a single chapter's content and structure."""
        try:
            soup = BeautifulSoup(item.get_content(), "lxml")

            # Basic structure analysis
            structure = {
                "headings": len(soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])),
                "paragraphs": len(soup.find_all("p")),
                "lists": len(soup.find_all(["ul", "ol"])),
                "tables": len(soup.find_all("table")),
                "images": len(soup.find_all("img")),
                "links": len(soup.find_all("a")),
            }

            # Extract image information
            images = []
            for img in soup.find_all("img"):
                image_info = {
                    "src": img.get("src", ""),
                    "alt": img.get("alt", ""),
                    "title": img.get("title", ""),
                }
                images.append(image_info)

            # Determine content type based on structure
            content_type = self._determine_content_type(structure)

            # Calculate word count
            word_count = len(soup.get_text(strip=True).split())

            return {
                "file_name": item.file_name,
                "content_type": content_type,
                "word_count": word_count,
                "structure": structure,
                "content": str(soup),
                "images": images,
            }
        except Exception as e:
            logger.error(f"Error analyzing chapter {item.file_name}: {e}")
            return None

    def _determine_content_type(self, structure: Dict) -> str:
        """Determine the type of content based on structure analysis."""
        if structure["images"] > 0 and structure["paragraphs"] == 0:
            return "image"
        elif structure["images"] > structure["paragraphs"]:
            return "image-heavy"
        elif structure["tables"] > structure["paragraphs"]:
            return "table-heavy"
        else:
            return "text"

    def generate_translation_chunks(
        self, project: TranslationProject
    ) -> List[TranslationChunk]:
        """Generate translation chunks for a translation project."""
        chunks = []
        sequence_number = 1

        # Process TOC items first to ensure we have at least one item
        for item in self.toc_items:
            title = item.get("title", "")
            chunk = TranslationChunk(
                project_id=project.id,
                sequence_number=sequence_number,
                original_content=title,
                content_type="toc",
                context=f"toc_{item.get('level', 1)}",
                word_count=len(title.split()),
            )
            chunks.append(chunk)
            sequence_number += 1

        # If no TOC items, add a default chunk to satisfy validation
        if not chunks:
            title = self.metadata.get("title", "Untitled")
            chunk = TranslationChunk(
                project_id=project.id,
                sequence_number=sequence_number,
                original_content=title,
                content_type="metadata",
                context="title",
                word_count=len(title.split()),
            )
            chunks.append(chunk)
            sequence_number += 1

        # Process chapters
        for chapter in self.chapters:
            if not chapter.get("content_type") == "text":
                continue

            soup = BeautifulSoup(chapter.get("content", ""), "lxml")
            splitter = self._splitter
            chapter_chunks = splitter.split_content(
                str(soup), chapter.get("file_name", ""), project.id
            )

            for chunk_dict in chapter_chunks:
                # Map content types from splitter to expected types
                content_type = "text"  # Default type
                if chunk_dict["content_type"] == "translatable":
                    if chunk_dict.get("parent_tag") in self._splitter.INLINE_TAGS:
                        content_type = "inline"
                    else:
                        content_type = "text"
                elif chunk_dict["content_type"] == "untranslatable":
                    continue  # Skip untranslatable content

                chunk = TranslationChunk(
                    project_id=project.id,
                    sequence_number=sequence_number,
                    original_content=chunk_dict["original_content"],
                    content_type=content_type,
                    context=chunk_dict["selector"],
                    word_count=chunk_dict["word_count"],
                )
                chunks.append(chunk)
                sequence_number += 1

            logger.debug(
                f"Generated {len(chapter_chunks)} chunks for {chapter.get('file_name')}"
            )

        return chunks

    def get_chapter_by_href(self, href: str) -> Optional[Dict]:
        """Get chapter information by href."""
        for chapter in self.chapters:
            if chapter["file_name"] == href:
                return chapter
        return None

    def get_resource_by_href(self, href: str) -> Optional[epub.EpubItem]:
        """Get resource (image, etc.) by href."""
        for item in self.book.get_items():
            if item.file_name == href:
                return item
        return None
