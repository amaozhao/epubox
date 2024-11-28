"""EPUB validation module."""

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import ebooklib
from bs4 import BeautifulSoup
from ebooklib import epub

from app.core.logging import services_logger as logger

logger = logger.bind(service="validator")


class EPUBValidator:
    """Validates EPUB files for structure and content."""

    def __init__(self):
        """Initialize the EPUB validator."""
        self.validation_results = {
            "structure": [],
            "content": [],
            "metadata": [],
            "resources": [],
        }

    def validate_epub(self, epub_path: str) -> Tuple[bool, Dict]:
        """
        Validate an EPUB file's structure and content.

        Args:
            epub_path: Path to the EPUB file

        Returns:
            Tuple of (is_valid, validation_results)
        """
        try:
            book = epub.read_epub(epub_path)
            is_valid = True

            # Validate structure
            structure_valid = self._validate_structure(book)
            is_valid = is_valid and structure_valid

            # Validate content
            content_valid = self._validate_content(book)
            is_valid = is_valid and content_valid

            # Validate metadata
            metadata_valid = self._validate_metadata(book)
            is_valid = is_valid and metadata_valid

            # Validate resources
            resources_valid = self._validate_resources(book)
            is_valid = is_valid and resources_valid

            return is_valid, self.validation_results

        except Exception as e:
            logger.error(f"Error validating EPUB file: {e}")
            self.validation_results["structure"].append(
                {"level": "error", "message": f"Failed to read EPUB file: {str(e)}"}
            )
            return False, self.validation_results

    def _validate_structure(self, book: epub.EpubBook) -> bool:
        """Validate EPUB structure."""
        is_valid = True

        # Check for table of contents
        if not book.toc:
            is_valid = False
            self.validation_results["structure"].append(
                {"level": "error", "message": "Missing table of contents"}
            )

        # Check for spine
        if not book.spine:
            is_valid = False
            self.validation_results["structure"].append(
                {"level": "error", "message": "Missing spine"}
            )

        # Check for required files
        required_files = ["toc.ncx", "content.opf"]
        for item in book.get_items():
            if item.file_name in required_files:
                required_files.remove(item.file_name)

        if required_files:
            is_valid = False
            self.validation_results["structure"].append(
                {
                    "level": "error",
                    "message": f"Missing required files: {required_files}",
                }
            )

        return is_valid

    def _validate_content(self, book: epub.EpubBook) -> bool:
        """Validate EPUB content."""
        is_valid = True

        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                try:
                    soup = BeautifulSoup(item.get_content(), "html.parser")

                    # Check for valid HTML structure
                    if not soup.find("body"):
                        is_valid = False
                        self.validation_results["content"].append(
                            {
                                "level": "error",
                                "message": f"Missing body tag in {item.file_name}",
                            }
                        )

                    # Check for broken links
                    for link in soup.find_all("a"):
                        href = link.get("href")
                        if href and not self._validate_internal_link(book, href):
                            is_valid = False
                            self.validation_results["content"].append(
                                {
                                    "level": "warning",
                                    "message": f"Broken internal link {href} in {item.file_name}",
                                }
                            )

                except Exception as e:
                    is_valid = False
                    self.validation_results["content"].append(
                        {
                            "level": "error",
                            "message": f"Error parsing {item.file_name}: {str(e)}",
                        }
                    )

        return is_valid

    def _validate_metadata(self, book: epub.EpubBook) -> bool:
        """Validate EPUB metadata."""
        is_valid = True
        required_metadata = ["title", "language"]

        for field in required_metadata:
            if not getattr(book.metadata, field, None):
                is_valid = False
                self.validation_results["metadata"].append(
                    {
                        "level": "error",
                        "message": f"Missing required metadata field: {field}",
                    }
                )

        return is_valid

    def _validate_resources(self, book: epub.EpubBook) -> bool:
        """Validate EPUB resources."""
        is_valid = True

        # Check images
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_IMAGE:
                if not self._validate_image(item):
                    is_valid = False
                    self.validation_results["resources"].append(
                        {
                            "level": "error",
                            "message": f"Invalid or corrupted image: {item.file_name}",
                        }
                    )

        return is_valid

    def _validate_internal_link(self, book: epub.EpubBook, href: str) -> bool:
        """Validate internal link."""
        if href.startswith("#"):
            # Internal anchor, needs more sophisticated checking
            return True

        # Check if the linked file exists
        for item in book.get_items():
            if item.file_name == href:
                return True
        return False

    def _validate_image(self, item: epub.EpubItem) -> bool:
        """Validate image file."""
        try:
            content = item.get_content()
            # Basic check - if we can get content, it's probably valid
            return bool(content and len(content) > 0)
        except Exception:
            return False
