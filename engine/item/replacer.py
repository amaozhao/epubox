import re
import secrets
import string
from typing import Dict, Optional, Set, Tuple

from bs4 import BeautifulSoup, Tag

from engine.constant import ID_LENGTH, PLACEHOLDER_DELIMITER, PLACEHOLDER_PATTERN
from engine.core.logger import engine_logger


class Placeholder:
    # 修改点 1: 只保留大写字母和数字
    characters = string.ascii_uppercase + string.digits  # 36种字符 (A-Z, 0-9)

    def __init__(self):
        self.placer_map: Dict[str, str] = {}
        self.value_to_key_map: Dict[str, str] = {}
        self.generated = set()

    def generate(self):
        while True:
            # 生成全大写的占位符
            _placeholder = "".join(secrets.choice(self.characters) for _ in range(ID_LENGTH))
            if _placeholder not in self.generated:
                break
        return _placeholder

    def placeholder(self, original):
        original_str = str(original)
        if original_str in self.value_to_key_map:
            return self.value_to_key_map[original_str]

        _placeholder = self.generate()
        self.generated.add(_placeholder)
        holder = f"{PLACEHOLDER_DELIMITER}{_placeholder}{PLACEHOLDER_DELIMITER}"
        self.placer_map[holder] = original_str
        self.value_to_key_map[original_str] = holder

        return holder


class Replacer:
    # ... (IGNORE_TAGS 和 IGNORE_TAG_CLASSES 保持不变) ...
    IGNORE_TAGS = {
        "script",
        "style",
        "code",
        "pre",
        "svg",
        "math",
        "img",
        "source",
        "meta",
        "link",
        "pageList",
        "content",
    }
    IGNORE_TAG_CLASSES: Set[Tuple[str, str]] = {
        ("table", "processedcode"),
        ("div", "no-translate"),
        ("span", "notranslate"),
        ("code", "language-plaintext"),
        ("code", "language-text"),
        ("code", "language-none"),
    }

    def __init__(self, parser: str = "html.parser"):
        self.parser = parser
        self.placeholder = Placeholder()

    def _replace(self, node):
        for child in list(node.contents):
            if isinstance(child, Tag):
                if child.name in self.IGNORE_TAGS:
                    placeholder = self.placeholder.placeholder(child)
                    child.replace_with(placeholder)
                elif child.name:
                    tag_classes = set(child.get("class") or [])
                    for tag_name, class_name in self.IGNORE_TAG_CLASSES:
                        if child.name == tag_name and class_name in tag_classes:
                            placeholder = self.placeholder.placeholder(child)
                            child.replace_with(placeholder)
                            break
                    else:
                        self._replace(child)
        return str(node)

    def replace(self, content: str) -> str:
        soup = BeautifulSoup(content, self.parser)
        return self._replace(soup)

    def restore(self, content: str, placeholders: Optional[Dict[str, str]] = None) -> str:
        placeholders = placeholders or self.placeholder.placer_map

        # 修改点 2: 提高还原的容错率
        # 如果 LLM 把 ##ABC## 变成了 ##abc##，我们可以通过 re.sub 忽略大小写进行替换
        for placeholder, original in placeholders.items():
            # 使用 re.escape 处理分隔符可能是特殊字符的情况
            # flags=re.IGNORECASE 确保即使 LLM 输出了小写也能匹配回我们的大写原始 key
            pattern = re.compile(re.escape(placeholder), re.IGNORECASE)
            content = pattern.sub(lambda m: original, content)

        remaining_placeholders = re.findall(PLACEHOLDER_PATTERN, content)
        if remaining_placeholders:
            engine_logger.warning(
                f"还有未还原的占位符: {len(remaining_placeholders)} 个, 示例: {remaining_placeholders[:5]}"
            )
        return content
