from typing import Dict, List, Optional
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, NavigableString, Comment
import os
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)


class EPUBParser:
    def __init__(self, epub_path: str):
        """Initialize the EPUB parser with a path to an EPUB file."""
        self.epub_path = epub_path
        try:
            self.book = epub.read_epub(epub_path)
            self.chapters = self._process_chapters()
            self.metadata = self._extract_metadata()
        except Exception as e:
            logger.error(f"Error reading EPUB file: {e}")
            raise

    def _process_chapters(self) -> List[Dict]:
        """Process all chapters in the EPUB file."""
        chapters = []
        for item in self.book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                logger.debug(f"Processing document: {item.get_name()}")
                chapter = self._process_single_chapter(item)
                if chapter:
                    chapters.append(chapter)

        logger.debug(f"Found {len(chapters)} chapters in EPUB")
        if not chapters:
            logger.warning("No chapters found in EPUB file")

        return chapters

    def _process_single_chapter(self, item) -> Dict:
        """Process a single chapter item.

        Analyzes the chapter content and identifies its type and characteristics:
        - Cover pages: Usually contain a single image
        - Image-only pages: Pages with only images and no meaningful text
        - Content pages: Pages with actual text content

        Returns:
            Dict containing chapter information including content type and text status
        """
        content = item.get_content().decode("utf-8")
        soup = BeautifulSoup(content, "lxml")

        # Extract text content, excluding script and style tags
        for tag in soup(["script", "style"]):
            tag.decompose()

        # Find all images
        images = soup.find_all("img")

        # Find all text nodes (excluding whitespace)
        text_nodes = [
            text.strip()
            for text in soup.find_all(text=True)
            if isinstance(text, NavigableString)
            and not isinstance(text, Comment)
            and text.strip()
            and not (text.parent.name == "[document]" or text.parent.name == "html")
        ]

        # Determine chapter type
        is_cover = len(images) == 1 and not text_nodes
        is_image_only = len(images) > 0 and not text_nodes

        chapter_info = {
            "id": item.get_id(),
            "name": item.get_name(),
            "content": content,
            "media_type": item.media_type,
            "text_content": " ".join(text_nodes),
            "has_text": bool(text_nodes),
            "image_count": len(images),
            "is_cover": is_cover,
            "is_image_only": is_image_only,
            "content_type": (
                "cover" if is_cover else "image_only" if is_image_only else "content"
            ),
        }

        logger.debug(
            f"Processed chapter {chapter_info['id']}: "
            f"type={chapter_info['content_type']}, "
            f"images={chapter_info['image_count']}, "
            f"has_text={chapter_info['has_text']}"
        )

        return chapter_info

    def _extract_metadata(self) -> Dict:
        """Extract metadata from the EPUB book."""
        try:
            metadata = {
                "title": self.book.get_metadata("DC", "title"),
                "creator": self.book.get_metadata("DC", "creator"),
                "language": self.book.get_metadata("DC", "language"),
                "identifier": self.book.get_metadata("DC", "identifier"),
                "publisher": self.book.get_metadata("DC", "publisher"),
                "date": self.book.get_metadata("DC", "date"),
            }
            logger.debug(f"Extracted metadata: {metadata}")
            return metadata
        except Exception as e:
            logger.error(f"Failed to extract metadata: {str(e)}")
            return {}

    def get_chapter_by_id(self, chapter_id: str) -> Optional[Dict]:
        """Get a specific chapter by its ID."""
        for chapter in self.chapters:
            if chapter.get("id") == chapter_id:  # Use get() to handle missing 'id' key
                logger.debug(
                    f"Found chapter {chapter_id} with content type {chapter.get('media_type')}"
                )
                return chapter
        logger.warning(
            f"Chapter {chapter_id} not found in {[c.get('id') for c in self.chapters]}"
        )
        return None

    def _get_context_path(self, element) -> str:
        """Get element's context path."""
        path_parts = []
        for parent in element.parents:
            if parent.name == "[document]":
                break
            attrs = []
            if parent.get("id"):
                attrs.append(f"#{parent['id']}")
            if parent.get("class"):
                attrs.append(f".{'.'.join(parent['class'])}")
            path_parts.append(f"{parent.name}{''.join(attrs)}")
        return "/" + "/".join(reversed(path_parts))

    def _should_process_text(self, element, excluded_tags: List[str]) -> bool:
        """Check if text element should be processed."""
        if not isinstance(element, NavigableString) or isinstance(element, Comment):
            return False

        text = element.strip()
        if not text:
            return False

        parent = element.parent
        if parent.name == "[document]" or (
            parent.name == "html" and not self._get_context_path(element)
        ):
            return False

        return not any(
            p.name in excluded_tags for p in element.parents if hasattr(p, "name")
        )

    def get_translatable_content(
        self, chapter_id: str, excluded_tags: List[str] = None
    ) -> List[Dict]:
        """Extract translatable content from a chapter while preserving excluded tags."""
        excluded_tags = excluded_tags or ["script", "style"]

        chapter = self.get_chapter_by_id(chapter_id)
        if not chapter:
            logger.warning(f"Chapter {chapter_id} not found")
            return []

        logger.debug(f"Processing chapter {chapter_id}")

        # Create a new soup to avoid modifying the original
        soup = BeautifulSoup(chapter["content"], "lxml")

        # Remove comments
        for comment in soup.find_all(text=lambda text: isinstance(text, Comment)):
            comment.extract()

        # Find the body tag or main content container
        root = soup.find("body") or soup.find(["article", "main", "div"]) or soup

        # Process text nodes within the root element
        segments = []
        for element in root.find_all(text=True):
            if self._should_process_text(element, excluded_tags):
                segments.append(
                    {
                        "text": element.strip(),
                        "context_path": self._get_context_path(element),
                        "chapter_id": chapter_id,
                    }
                )

        logger.debug(
            f"Found {len(segments)} translatable segments in chapter {chapter_id}"
        )
        return segments

    def get_all_translatable_content(
        self, excluded_tags: List[str] = None
    ) -> Dict[str, List[Dict]]:
        """Get all translatable content from the EPUB file.

        This method processes all chapters that potentially have text content and returns
        a dictionary mapping chapter IDs to their translatable segments. Chapters that
        don't yield any translatable segments (e.g., image-only chapters) are excluded
        from the results.

        Args:
            excluded_tags: Optional list of HTML tags to exclude from translation

        Returns:
            Dict mapping chapter IDs to lists of translatable segments. Only chapters
            that yielded actual translatable content are included in the results.
        """
        excluded_tags = excluded_tags or ["script", "style"]
        all_content = {}

        # Only process chapters that have text content
        text_chapters = self.get_text_chapters()
        logger.debug(f"Processing {len(text_chapters)} chapters with text content")

        for chapter in text_chapters:
            chapter_id = chapter["id"]
            content = self.get_translatable_content(chapter_id, excluded_tags)
            if content:  # Only include chapters that yielded translatable content
                all_content[chapter_id] = content
                logger.debug(f"Processed chapter {chapter_id}: {len(content)} segments")
            else:
                logger.debug(f"No translatable content found in chapter {chapter_id}")

        if not all_content:
            logger.warning("No translatable content found in any chapter")
        else:
            logger.info(f"Found translatable content in {len(all_content)} chapters")

        return all_content

    @property
    def has_text_content(self) -> bool:
        """Check if the EPUB has any text content to translate."""
        return any(chapter.get("has_text") for chapter in self.chapters)

    @property
    def chapter_count(self) -> int:
        """Get the total number of chapters."""
        return len(self.chapters)

    @property
    def text_chapter_count(self) -> int:
        """Get the number of chapters containing text."""
        return sum(1 for chapter in self.chapters if chapter.get("has_text"))

    def get_chapter_names(self) -> List[str]:
        """Get a list of all chapter names."""
        return [chapter["name"] for chapter in self.chapters]

    def get_text_chapters(self) -> List[Dict]:
        """Get all chapters that contain text content."""
        return [chapter for chapter in self.chapters if chapter.get("has_text")]

    @property
    def cover_chapters(self) -> List[Dict]:
        """Get all chapters identified as cover pages."""
        return [chapter for chapter in self.chapters if chapter.get("is_cover")]

    @property
    def image_only_chapters(self) -> List[Dict]:
        """Get all chapters that contain only images."""
        # Only count as image-only if it's not already counted as a cover
        return [
            chapter
            for chapter in self.chapters
            if chapter.get("is_image_only") and not chapter.get("is_cover")
        ]

    @property
    def content_chapters(self) -> List[Dict]:
        """Get all chapters that contain actual content (not just images)."""
        return [
            chapter
            for chapter in self.chapters
            if not chapter.get("is_cover")
            and not chapter.get("is_image_only")
            and chapter.get("content_type") == "content"
        ]

    def get_chapter_stats(self) -> Dict:
        """Get statistics about the chapters in this EPUB.

        Returns:
            Dict containing counts of different chapter types and overall statistics
        """
        stats = {
            "total_chapters": len(self.chapters),
            "cover_pages": len(self.cover_chapters),
            "image_only_pages": len(self.image_only_chapters),
            "content_pages": len(self.content_chapters),
            "chapters_with_text": self.text_chapter_count,
            "total_images": sum(
                chapter.get("image_count", 0) for chapter in self.chapters
            ),
            "unclassified_pages": len(self.chapters)
            - (
                len(self.cover_chapters)
                + len(self.image_only_chapters)
                + len(self.content_chapters)
            ),
        }

        if stats["unclassified_pages"] > 0:
            logger.warning(
                f"Found {stats['unclassified_pages']} chapters that couldn't be classified. "
                "This might indicate invalid content or parsing issues."
            )

        logger.debug(f"EPUB Statistics: {stats}")
        return stats
