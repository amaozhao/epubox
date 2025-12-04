import re
import secrets
import string
from typing import Dict, Optional, Set, Tuple

from bs4 import BeautifulSoup, Tag

from engine.constant import ID_LENGTH, PLACEHOLDER_DELIMITER, PLACEHOLDER_PATTERN
from engine.core.logger import engine_logger


class Placeholder:
    characters = string.ascii_letters + string.digits  # 62种字符

    def __init__(self):
        self.placer_map: Dict[str, str] = {}
        self.value_to_key_map: Dict[str, str] = {}
        self.generated = set()

    def generate(self):
        while True:
            _placeholder = "".join(secrets.choice(self.characters) for _ in range(ID_LENGTH))
            if _placeholder not in self.generated:
                break
        return _placeholder

    def placeholder(self, original):
        original_str = str(original)
        # Check if the value already exists in the map
        if original_str in self.value_to_key_map:
            return self.value_to_key_map[original_str]

        # If not, generate a new placeholder
        _placeholder = self.generate()
        self.generated.add(_placeholder)
        holder = f"{PLACEHOLDER_DELIMITER}{_placeholder}{PLACEHOLDER_DELIMITER}"

        # Store both forward and reverse mappings
        self.placer_map[holder] = original_str
        self.value_to_key_map[original_str] = holder

        return holder


class Replacer:
    # Tags to completely ignore
    IGNORE_TAGS = {
        "script",
        "style",
        "code",
        "pre",
        "svg",
        "math",
        "img",
        "source",
        "figure",
        "meta",
        "link",
        "pageList",
        "content",
    }

    # Tags with specific classes to ignore (tag_name, class_name)
    IGNORE_TAG_CLASSES: Set[Tuple[str, str]] = {
        ("table", "processedcode"),
        ("div", "no-translate"),
        ("span", "notranslate"),
        ("code", "language-plaintext"),
        ("code", "language-text"),
        ("code", "language-none"),
        # Add more tag-class combinations here as needed, e.g., ("div", "no-translate")
    }

    def __init__(self, parser: str = "html.parser"):
        self.parser = parser
        self.placeholder = Placeholder()

    def _replace(self, node):
        for child in list(node.contents):
            if isinstance(child, Tag):
                # Check if the tag is in IGNORE_TAGS
                if child.name in self.IGNORE_TAGS:
                    placeholder = self.placeholder.placeholder(child)
                    child.replace_with(placeholder)
                # Check if the tag has a specific class that should be ignored
                elif child.name:
                    # Explicitly handle the class attribute to satisfy type checker
                    tag_classes = set(child.get("class") or [])
                    for tag_name, class_name in self.IGNORE_TAG_CLASSES:
                        if child.name == tag_name and class_name in tag_classes:
                            placeholder = self.placeholder.placeholder(child)
                            child.replace_with(placeholder)
                            break
                    else:
                        # Recursively process child tags if not ignored
                        self._replace(child)
        return str(node)

    def replace(self, content: str) -> str:
        soup = BeautifulSoup(content, self.parser)
        return self._replace(soup)

    def restore(self, content: str, placeholders: Optional[Dict[str, str]] = None) -> str:
        # Use self.placeholder.placer_map if placeholders is None
        placeholders = placeholders or self.placeholder.placer_map
        for placeholder, original in placeholders.items():
            content = content.replace(placeholder, original)

        remaining_placeholders = re.findall(PLACEHOLDER_PATTERN, content)
        if remaining_placeholders:
            engine_logger.warning(
                f"还有未还原的占位符: {len(remaining_placeholders)} 个, 示例: {remaining_placeholders[:5]}"
            )
        return content
