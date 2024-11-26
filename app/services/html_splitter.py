from typing import Dict, List, Optional, Set, Union

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from app.core.logging import get_logger

logger = get_logger("app.services.html_splitter")  # Use fully qualified logger name


class HTMLSplitter:
    # Default tags that should never be translated
    DEFAULT_UNTRANSLATABLE_TAGS = {
        "xml",  # XML declaration
        "doctype",  # DOCTYPE declaration
        "meta",  # Metadata
        "style",  # CSS styles
        "script",  # JavaScript
        "img",  # Images
        "link",  # External resources
        "code",  # Code blocks
        "pre",  # Preformatted text
        "svg",  # SVG graphics
        "math",  # MathML
    }

    def __init__(self, custom_untranslatable_tags: Set[str] = None):
        """Initialize the HTML splitter with optional custom untranslatable tags."""
        self.untranslatable_tags = self.DEFAULT_UNTRANSLATABLE_TAGS.copy()
        if custom_untranslatable_tags:
            self.untranslatable_tags.update(custom_untranslatable_tags)
        logger.debug(
            f"Initialized HTMLSplitter with untranslatable tags: {self.untranslatable_tags}"
        )

    def _is_translatable_node(self, node: Union[Tag, NavigableString, Comment]) -> bool:
        """
        Determine if a node should be translated.
        """
        # Skip comments and empty strings
        if isinstance(node, Comment) or (
            isinstance(node, NavigableString) and not str(node).strip()
        ):
            return False

        # Get the parent tag
        parent = node.parent
        if not parent:
            return False

        # Check if any parent is in untranslatable tags
        current = parent
        while current:
            if current.name in self.untranslatable_tags:
                return False
            current = current.parent

        # Only translate text nodes
        return isinstance(node, NavigableString)

    def _get_node_context(self, node: NavigableString) -> Dict:
        """
        Get context information for a text node.
        """
        parent_tag = node.parent.name if node.parent else None
        parent_class = node.parent.get("class", []) if node.parent else []
        parent_id = node.parent.get("id", "") if node.parent else ""

        # Get full path to node
        path = []
        current = node.parent
        while current:
            tag_info = current.name
            if current.get("class"):
                tag_info += "." + ".".join(current["class"])
            if current.get("id"):
                tag_info += "#" + current["id"]
            path.append(tag_info)
            current = current.parent

        return {
            "parent_tag": parent_tag,
            "parent_class": parent_class,
            "parent_id": parent_id,
            "path": "->".join(reversed(path)),
            "text": str(node).strip(),  # Store stripped version for matching
            "original_text": str(node),  # Store original with whitespace
        }

    def split_content(self, html_content: str) -> List[Dict[str, str]]:
        """Split HTML content into translatable and non-translatable parts."""
        # Parse with html.parser to handle XML/XHTML correctly
        soup = BeautifulSoup(html_content, "html.parser")
        result = []

        # First pass: collect all text nodes
        text_nodes = []
        for node in soup.find_all(text=True):
            if self._is_translatable_node(node):
                text_nodes.append(node)

        # Second pass: collect untranslatable tags
        for tag_name in self.untranslatable_tags:
            for tag in soup.find_all(tag_name):
                if str(tag).strip():
                    result.append(
                        {
                            "type": "untranslatable",
                            "content": str(tag),
                            "context": {"tag": tag.name, "path": tag.name},
                        }
                    )

        # Process text nodes
        for node in text_nodes:
            text = str(node).strip()
            if text:  # Skip empty nodes
                context = self._get_node_context(node)
                result.append(
                    {
                        "type": "translatable",
                        "content": context["original_text"],
                        "context": context,
                    }
                )

        return result

    def reassemble_content(
        self, split_parts: List[Dict[str, str]], original_html: str
    ) -> str:
        """Reassemble the split parts back into HTML content."""
        # Create translation mapping using stripped text as key
        translations = {}
        for part in split_parts:
            if part["type"] == "translatable":
                # Use the stored stripped text as key
                original_text = part["context"]["text"]
                translations[original_text] = part["content"]

        # Parse original HTML
        soup = BeautifulSoup(original_html, "html.parser")

        # Process all text nodes
        for node in soup.find_all(text=True):
            if self._is_translatable_node(node):
                stripped_text = str(node).strip()
                if stripped_text in translations:
                    # Preserve original whitespace
                    original_text = str(node)
                    prefix = original_text[
                        : len(original_text) - len(original_text.lstrip())
                    ]
                    suffix = original_text[len(original_text.rstrip()) :]
                    translated_text = translations[stripped_text]

                    # Create new text node with preserved whitespace
                    new_text = prefix + translated_text + suffix

                    try:
                        node.replace_with(NavigableString(new_text))
                    except Exception as e:
                        logger.error(f"Failed to replace text node: {e}")
                        logger.error(
                            f"Node details: parent={node.parent}, string='{node}'"
                        )
                        raise

        return str(soup)
