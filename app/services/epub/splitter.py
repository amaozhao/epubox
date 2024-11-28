"""HTML content splitter for translation."""

import re
from typing import Dict, List, Set

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from app.core.logging import services_logger as logger

logger = logger.bind(service="splitter")


class HTMLSplitter:
    """Split HTML content into translatable chunks."""

    # Tags that should not be translated
    UNTRANSLATABLE_TAGS = {"code", "pre", "script", "style"}

    # Inline tags that should be treated as complete units
    INLINE_TAGS = {"em", "strong", "i", "b", "span", "a", "sub", "sup"}

    def __init__(self, custom_untranslatable_tags: Set[str] = None):
        """Initialize splitter with optional custom untranslatable tags."""
        self.untranslatable_tags = self.UNTRANSLATABLE_TAGS.copy()
        if custom_untranslatable_tags:
            self.untranslatable_tags.update(custom_untranslatable_tags)
        logger.debug(
            f"Initialized HTMLSplitter with untranslatable tags: {self.untranslatable_tags}"
        )

    def _normalize_whitespace(self, text: str) -> str:
        """Normalize whitespace while preserving intentional spaces."""
        # Replace newlines and tabs with spaces
        text = re.sub(r"[\n\t]+", " ", text)
        # Collapse multiple spaces into single space
        text = re.sub(r" +", " ", text)
        return text.strip()

    def _extract_text_with_markers(self, tag: Tag) -> tuple[str, dict]:
        """Extract text content from a tag, replacing nested tags with markers."""
        result = []
        markers = {}
        marker_index = 0

        def process_node(node):
            nonlocal marker_index
            if isinstance(node, NavigableString) and not isinstance(node, Comment):
                text = self._normalize_whitespace(str(node))
                if text:
                    result.append(text)
            elif isinstance(node, Tag):
                if node.name in self.untranslatable_tags:
                    marker = f"__UNTRANSLATABLE_{marker_index}__"
                    result.append(marker)
                    markers[marker] = str(node)
                    marker_index += 1
                elif node.name in self.INLINE_TAGS:
                    # Create marker for this tag
                    marker = f"__TAG_{marker_index}__"
                    result.append(marker)

                    # For inline tags, preserve the complete HTML structure
                    markers[marker] = (
                        node.decode()
                    )  # Use decode() to prevent HTML escaping
                    marker_index += 1

        for child in tag.children:
            process_node(child)

        return " ".join(result), markers

    def split_content(
        self, html_content: str, source_file: str, project_id: int
    ) -> List[Dict]:
        """Split HTML content into translatable and untranslatable chunks."""
        try:
            soup = BeautifulSoup(html_content, "html.parser")
            chunks = []
            text_node_index = 0

            # First, process untranslatable tags
            for tag in soup.find_all(self.untranslatable_tags):
                chunks.append(
                    {
                        "content": str(tag),
                        "content_type": "untranslatable",
                        "original_content": str(tag),
                        "type": "untranslatable",
                        "context": {"source_file": source_file},
                        "word_count": 0,
                        "source_file": source_file,
                        "tag_name": tag.name,
                        "attributes": dict(tag.attrs) if tag.attrs else {},
                        "tag": tag.name,
                        "node_index": text_node_index,
                    }
                )
                # Replace with marker
                marker = f"__UNTRANSLATABLE_{text_node_index}__"
                tag.replace_with(marker)
                text_node_index += 1

            # Process block-level tags
            for tag in soup.find_all(["p"]):
                # Skip empty tags
                if not tag.get_text(strip=True):
                    continue

                # Extract text and markers
                text, markers = self._extract_text_with_markers(tag)
                if text:
                    # Generate a CSS-like selector for the tag
                    selector = f"{tag.name}"
                    if tag.get("id"):
                        selector += f"#{tag.get('id')}"
                    elif tag.get("class"):
                        selector += f".{'.'.join(tag.get('class'))}"

                    chunks.append(
                        {
                            "content": text,
                            "content_type": "translatable",
                            "original_content": text,
                            "type": "translatable",
                            "context": {"source_file": source_file},
                            "word_count": len(text.split()),
                            "source_file": source_file,
                            "tag_name": tag.name,
                            "attributes": dict(tag.attrs) if tag.attrs else {},
                            "tag": tag.name,
                            "node_index": text_node_index,
                            "markers": markers,
                            "selector": selector,
                        }
                    )
                    text_node_index += 1

            # Add project ID and status
            for chunk in chunks:
                chunk["project_id"] = project_id
                chunk["status"] = "pending"

            return sorted(chunks, key=lambda x: x["node_index"])

        except Exception as e:
            logger.error(f"Error splitting content from {source_file}: {e}")
            raise

    def reassemble_content(self, chunks: List[Dict], original_html: str) -> str:
        """Reassemble translated chunks back into HTML."""
        try:
            soup = BeautifulSoup(original_html, "html.parser")

            # Create a mapping of original content to translated content
            translations = {
                chunk["original_content"]: chunk["content"]
                for chunk in chunks
                if chunk["content_type"] == "translatable"
            }

            def process_tag(tag: Tag):
                if tag.name in self.untranslatable_tags:
                    return  # Skip untranslatable tags

                # Extract text with markers
                text, markers = self._extract_text_with_markers(tag)
                if text in translations:
                    # Get translated text
                    translated_text = translations[text]
                    # Replace markers with original tags
                    for marker, original_tag in markers.items():
                        translated_text = translated_text.replace(marker, original_tag)
                    # Clear and update tag content
                    tag.clear()
                    tag.append(BeautifulSoup(translated_text, "html.parser"))

            # Process each tag that might contain translatable content
            for tag in soup.find_all(
                ["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li"]
            ):
                process_tag(tag)

            return str(soup)

        except Exception as e:
            logger.error(f"Error reassembling content: {e}")
            raise
