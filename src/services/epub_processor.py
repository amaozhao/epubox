"""EPUB processor service."""

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from bs4 import BeautifulSoup
from ebooklib import epub


class EPUBProcessorError(Exception):
    """Base exception for EPUB processor errors."""

    pass


class EPUBProcessor:
    """EPUB file processor."""

    def __init__(self, temp_dir: str = None):
        """Initialize EPUB processor."""
        if temp_dir:
            self.temp_dir = temp_dir
            logging.info(f"Using temporary directory: {self.temp_dir}")
        else:
            self.temp_dir = tempfile.mkdtemp()
            logging.info(f"Created temporary directory: {self.temp_dir}")

    def _ensure_temp_dir_exists(self):
        """Ensure temporary directory exists."""
        if not os.path.exists(self.temp_dir):
            raise EPUBProcessorError(f"Temporary directory not found: {self.temp_dir}")

    async def create_temp_dir(self):
        """Create temporary directory if it doesn't exist."""
        try:
            os.makedirs(self.temp_dir, exist_ok=True)
            logging.info(f"Created directory: {self.temp_dir}")
        except Exception as e:
            raise EPUBProcessorError(f"Failed to create temporary directory: {str(e)}")

    async def extract_content(self, epub_path: str) -> List[Dict[str, Any]]:
        """Extract content from EPUB file."""
        if not os.path.exists(epub_path):
            raise EPUBProcessorError(f"EPUB file not found: {epub_path}")

        try:
            book = epub.read_epub(epub_path)
            contents = []

            for item in book.get_items():
                if isinstance(item, epub.EpubHtml) and not isinstance(
                    item, epub.EpubNav
                ):
                    content = item.get_content()
                    if isinstance(content, bytes):
                        content = content.decode("utf-8")
                    soup = BeautifulSoup(content, "html.parser")
                    content = {
                        "id": item.get_name().replace(".xhtml", ""),
                        "file_name": item.get_name(),
                        "media_type": "application/xhtml+xml",
                        "content": soup.get_text(strip=True),
                    }
                    contents.append(content)
                    logging.debug(f"Extracted content from {item.get_name()}")

            if not contents:
                logging.warning(f"No content found in EPUB file: {epub_path}")

            return contents
        except Exception as e:
            error_msg = f"Failed to extract EPUB content: {str(e)}"
            logging.error(error_msg)
            raise EPUBProcessorError(error_msg)

    async def save_translated_content(
        self,
        epub_path: str,
        translated_contents: List[Dict[str, Any]],
        output_path: str,
    ) -> str:
        """Save translated content back to EPUB file."""
        work_dir = None
        try:
            # Ensure temp directory exists
            await self.create_temp_dir()

            # Create a temporary working directory
            work_dir = os.path.join(self.temp_dir, "translation_work")
            os.makedirs(work_dir, exist_ok=True)
            logging.info(f"Created temporary working directory: {work_dir}")

            # Copy original epub to work directory
            temp_epub = os.path.join(work_dir, "original.epub")
            if not os.path.exists(epub_path):
                raise EPUBProcessorError(f"Source EPUB file not found: {epub_path}")
            try:
                shutil.copy2(epub_path, temp_epub)
                logging.info(
                    f"Copied original EPUB to temporary directory: {temp_epub}"
                )
            except Exception as e:
                raise EPUBProcessorError(f"Failed to copy original EPUB: {str(e)}")

            # Read the book
            try:
                book = epub.read_epub(temp_epub)
            except Exception as e:
                raise EPUBProcessorError(f"Failed to read EPUB: {str(e)}")

            # Update content
            content_updated = False
            for item in book.get_items():
                if isinstance(item, epub.EpubHtml) and not isinstance(
                    item, epub.EpubNav
                ):
                    for content in translated_contents:
                        if content["file_name"] == item.get_name():
                            try:
                                item.set_content(content["content"])
                                content_updated = True
                                logging.debug(f"Updated content for {item.get_name()}")
                            except Exception as e:
                                raise EPUBProcessorError(
                                    f"Failed to update content for {item.get_name()}: {str(e)}"
                                )

            if not content_updated:
                raise EPUBProcessorError("No matching content found to update in EPUB")

            # Ensure output directory exists
            try:
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                logging.info(
                    f"Created output directory: {os.path.dirname(output_path)}"
                )
            except Exception as e:
                raise EPUBProcessorError(f"Failed to create output directory: {str(e)}")

            # Write the translated epub
            try:
                epub.write_epub(output_path, book)
                logging.info(f"Wrote translated EPUB to output path: {output_path}")
            except Exception as e:
                raise EPUBProcessorError(f"Failed to write translated EPUB: {str(e)}")

            return output_path

        except EPUBProcessorError:
            raise
        except Exception as e:
            error_msg = f"Failed to save translated content: {str(e)}"
            logging.error(error_msg)
            raise EPUBProcessorError(error_msg)
        finally:
            # Clean up work directory
            if work_dir and os.path.exists(work_dir):
                try:
                    shutil.rmtree(work_dir)
                    logging.info(f"Removed temporary working directory: {work_dir}")
                except Exception as e:
                    logging.error(
                        f"Error removing temporary working directory: {str(e)}"
                    )

    async def cleanup(self):
        """Clean up temporary files."""
        self._ensure_temp_dir_exists()  # Will raise if directory doesn't exist

        try:
            for file in os.listdir(self.temp_dir):
                file_path = os.path.join(self.temp_dir, file)
                try:
                    if os.path.isfile(file_path):
                        os.unlink(file_path)
                        logging.info(f"Removed temporary file: {file_path}")
                except Exception as e:
                    logging.error(f"Error deleting {file_path}: {e}")
            try:
                os.rmdir(self.temp_dir)
                logging.info(f"Removed temporary directory: {self.temp_dir}")
            except Exception as e:
                logging.error(f"Error removing directory {self.temp_dir}: {e}")
        except Exception as e:
            raise EPUBProcessorError(f"Failed to cleanup temporary directory: {str(e)}")

    def __del__(self):
        """Destructor to ensure cleanup."""
        try:
            # Since we can't await in __del__, we'll just remove the directory synchronously
            if hasattr(self, "temp_dir") and os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir, ignore_errors=True)
                logging.info(
                    f"Cleaned up temporary directory in __del__: {self.temp_dir}"
                )
        except Exception as e:
            logging.error(f"Error in __del__ cleanup: {str(e)}")
