import re
import secrets
import string
from typing import Dict

from bs4 import BeautifulSoup, Tag

from engine.constant import ID_LENGTH, PLACEHOLDER_DELIMITER, PLACEHOLDER_PATTERN
from engine.core.logger import engine_logger


class Placeholder:
    characters = string.ascii_letters + string.digits  # 62种字符

    def __init__(self):
        self.placer_map: Dict[str, str] = {}
        self.generated = set()

    def generate(self):
        while True:
            _placeholder = "".join(secrets.choice(self.characters) for _ in range(ID_LENGTH))
            if _placeholder in self.generated:
                _placeholder = "".join(secrets.choice(self.characters) for _ in range(ID_LENGTH))
            break
        return _placeholder

    def placeholder(self, original):
        while True:
            _placeholder = self.generate()
            if _placeholder not in self.generated:
                self.generated.add(_placeholder)
                holder = f"{PLACEHOLDER_DELIMITER}{_placeholder}{PLACEHOLDER_DELIMITER}"
                self.placer_map[holder] = str(original)
                return holder


class Replacer:
    IGNORE_TAGS = {
        # 脚本和样式
        "script",
        "style",
        # 代码相关
        "code",
        "pre",
        # "kbd",
        # "var",
        # "samp",
        # 特殊内容
        "svg",
        "math",
        # "canvas",
        # "address",
        # "applet",
        # 多媒体标签
        "img",
        # "audio",
        # "video",
        # "track",
        "source",
        "figure",
        # 表单相关
        # "input",
        # "button",
        # "select",
        # "option",
        # "textarea",
        # "form",
        # 元数据和链接
        "meta",
        "link",
        # "a", # User commented out 'a', keeping consistent
        # 嵌入内容
        # "iframe",
        # "embed",
        # "object",
        # "param",
        # 技术标记
        # "time",
        # "data",
        # "meter",
        # "progress",
        # XML相关
        # "xml",
        # "xmlns",
        # EPUB特有标签
        # "epub:switch",
        # "epub:case",
        # "epub:default",
        # "annotation",
        # "note",
        "pageList",
        "content",
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
                else:
                    self._replace(child)
        return str(node)

    def replace(self, content: str) -> str:
        soup = BeautifulSoup(content, self.parser)
        return self._replace(soup)

    def restore(self, content: str, placeholders: Dict[str, str]) -> str:
        for placeholder, original in placeholders.items():
            content = content.replace(placeholder, original)

        remaining_placeholders = re.findall(PLACEHOLDER_PATTERN, content)
        if remaining_placeholders:
            engine_logger.warning(
                "还有未还原的占位符", count=len(remaining_placeholders), examples=remaining_placeholders[:5]
            )
        return content
