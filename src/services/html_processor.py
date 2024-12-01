import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

import tiktoken
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from src.services.translation_service import TranslationService


class HTMLProcessingError(Exception):
    """HTML 处理通用错误"""

    pass


class TokenLimitError(HTMLProcessingError):
    """Token 超限错误"""

    pass


class ContentRestoreError(HTMLProcessingError):
    """内容还原错误"""

    pass


class StructureError(HTMLProcessingError):
    """结构分析错误"""

    pass


@dataclass
class HTMLElement:
    """HTML元素的数据类"""

    tag: str  # 标签名
    html: str  # 原始HTML
    token_estimate: int  # token估计数
    is_metadata: bool = False  # 是否是元数据标签
    is_block: bool = False  # 是否是块级元素
    depth: int = 0  # 元素深度
    content: str = ""  # 元素内容
    attributes: Dict[str, str] = field(default_factory=dict)  # 元素属性
    start_pos: int = 0  # 元素开始位置
    end_pos: int = 0  # 元素结束位置
    children: List["HTMLElement"] = field(default_factory=list)  # 子元素


class AttributeProcessor:
    """属性处理器"""

    @staticmethod
    def process_boolean_attr(value: Any) -> Optional[None]:
        """处理布尔属性（如async, defer）"""
        return None if value in (True, "") else value

    @staticmethod
    def process_class_attr(value: str) -> List[str]:
        """处理class属性"""
        return value.split() if isinstance(value, str) else value

    @staticmethod
    def process_style_attr(value: str) -> str:
        """处理style属性"""
        return value.strip()

    @staticmethod
    def process_data_attr(value: Any) -> Any:
        """处理data-*属性"""
        return json.dumps(value) if isinstance(value, (dict, list)) else str(value)


class RestoreStrategy:
    """内容还原策略"""

    @staticmethod
    def from_complete_content(mapping: Dict[str, Any]) -> str:
        """从完整内容还原"""
        return mapping["content"]

    @staticmethod
    def rebuild_from_parts(mapping: Dict[str, Any]) -> str:
        """从部分信息重建"""
        tag_name = mapping["name"]
        attrs = " ".join(
            f'{k}="{v}"' if v is not None else k
            for k, v in mapping["attributes"].items()
        )
        inner_html = mapping["structure"]["inner_html"]
        return f"<{tag_name} {attrs}>{inner_html}</{tag_name}>"


class ErrorRecovery:
    """错误恢复处理"""

    @staticmethod
    def validate_placeholder(placeholder: str) -> bool:
        """验证占位符格式"""
        import re

        return bool(re.match(r"\[\[([A-Z]+)_(\d+)\]\]", placeholder))

    @staticmethod
    def validate_mapping(mapping: Dict[str, Any]) -> bool:
        """验证映射数据完整性"""
        required_fields = {"type", "name", "content"}
        return all(field in mapping for field in required_fields)


class HTMLProcessor:
    """HTML内容处理组件

    负责处理HTML内容的分割、翻译和重组，确保在翻译过程中保持HTML结构和语义的完整性。
    """

    # 不需要翻译的标签
    NON_TRANSLATABLE_TAGS = {"script", "style", "code", "pre"}

    # 元数据标签
    METADATA_TAGS = {"title", "meta"}

    # 块级元素标签，按优先级排序
    BLOCK_TAGS = [
        # 文档元数据
        {"title"},
        # 章节级别
        {"section", "article", "nav", "aside"},
        # 标题族
        {"h1", "h2", "h3", "h4", "h5", "h6"},
        # 内容块
        {"div", "p", "blockquote"},
        # 列表
        {"ul", "ol"},
        # 列表项
        {"li"},
    ]

    # 需要保持在一起的语义单元
    SEMANTIC_UNITS = [
        ("h1", "p"),  # 标题和其后的第一段
        ("h2", "p"),
        ("h3", "p"),
        ("li", "p"),  # 列表项和其中的段落
        ("p", "blockquote"),  # 引用和其上下文
    ]

    def __init__(self, translation_service: TranslationService):
        """初始化HTML处理器"""
        self.translation_service = translation_service
        self.max_tokens = translation_service.get_token_limit()  # 保持原始token限制
        self._placeholder_counter = 1
        # 初始化tiktoken编码器，使用GPT-3.5的编码方式
        self.tokenizer = tiktoken.get_encoding("cl100k_base")

    def _count_tokens(self, text: str) -> int:
        """使用tiktoken准确计算token数量

        Args:
            text: 需要计算token数的文本

        Returns:
            token数量
        """
        return len(self.tokenizer.encode(text))

    async def process_content(
        self, content: Union[str, Dict[str, Any]], source_lang: str, target_lang: str
    ) -> Union[str, Dict[str, Any]]:
        """处理HTML内容的主入口方法"""
        try:
            # 如果输入是字符串，直接处理
            if isinstance(content, str):
                html_content = content
            else:
                html_content = content["content"]

            # 在解析前保存原始文本
            original_html = html_content

            # 检查HTML结构
            soup = BeautifulSoup(html_content, "html.parser")

            # 如果HTML标签不匹配，直接返回原文
            if original_html.count("<div>") != original_html.count("</div>"):
                return (
                    original_html
                    if isinstance(content, str)
                    else {
                        "id": content["id"],
                        "file_name": content["file_name"],
                        "media_type": content.get("media_type", "text/html"),
                        "content": original_html,
                    }
                )

            # 1. 预处理，保护不需要翻译的内容
            processed_html, mapping = await self.preprocess(html_content)

            # 检查token限制
            text_content = soup.get_text()
            if len(text_content) > self.max_tokens:
                raise TokenLimitError(
                    f"Content exceeds token limit of {self.max_tokens}"
                )

            # 2. 翻译处理后的内容
            translated_html = await self.translation_service.translate(
                processed_html, source_lang, target_lang
            )

            # 3. 还原保护的内容
            restored_html = await self.restore_content(translated_html, mapping)

            # 4. 返回结果
            if isinstance(content, str):
                return restored_html
            return {
                "id": content["id"],
                "file_name": content["file_name"],
                "media_type": content.get("media_type", "text/html"),
                "content": restored_html,
            }

        except Exception as e:
            # 转换为适当的错误类型
            if isinstance(e, (TokenLimitError, ContentRestoreError)):
                raise
            raise HTMLProcessingError(f"处理内容失败: {str(e)}") from e

    async def preprocess(self, html: str) -> Tuple[str, Dict[str, Any]]:
        """预处理HTML内容"""
        if not html:
            raise StructureError("Empty HTML content")

        try:
            soup = BeautifulSoup(html, "html.parser")
            if not soup.find():  # 检查是否有有效的HTML结构
                raise StructureError("Invalid HTML structure")
        except Exception as e:
            raise StructureError(f"Failed to parse HTML: {str(e)}")

        mapping = {}
        for tag in soup.find_all(self.NON_TRANSLATABLE_TAGS):
            if tag.parent and tag.parent.name in self.NON_TRANSLATABLE_TAGS:
                continue

            placeholder = self.generate_placeholder(tag.name)
            mapping[placeholder] = {
                "type": tag.name,
                "name": tag.name,
                "content": str(tag),
                "attributes": {k: v for k, v in tag.attrs.items()},
            }
            tag.replace_with(placeholder)

        return str(soup), mapping

    def generate_placeholder(self, tag_type: str) -> str:
        """生成唯一的占位符"""
        placeholder = f"[[{tag_type.upper()}_{self._placeholder_counter}]]"
        self._placeholder_counter += 1
        return placeholder

    async def analyze_structure(self, html: str) -> Dict[str, Any]:
        """分析HTML结构，标记元素的类型和位置"""
        try:
            soup = BeautifulSoup(html, "html.parser")
            # 检查HTML是否有效
            if not soup.find():
                raise StructureError("HTML内容为空或无效")

            # 分析结构
            structure = {"elements": [], "token_count": 0}

            # 递归分析元素
            for element in soup.find_all(True):  # True matches all tags
                # 计算当前元素的token数
                text_content = element.get_text()
                token_count = len(self.tokenizer.encode(text_content))

                # 记录元素信息
                elem_info = {
                    "tag": element.name,
                    "html": str(element),
                    "token_estimate": token_count,
                    "is_metadata": element.name in self.METADATA_TAGS,
                    "is_block": element.name
                    in {tag for group in self.BLOCK_TAGS for tag in group},
                }
                structure["elements"].append(elem_info)
                structure["token_count"] += token_count

            return structure

        except Exception as e:
            if isinstance(e, StructureError):
                raise
            raise StructureError(f"分析HTML结构失败: {str(e)}") from e

    async def segment_text(self, structure: Dict[str, Any]) -> List[str]:
        """根据结构信息和token限制分割文本，保持语义完整性"""
        try:

            def wrap_segment(
                content: List[Dict[str, Any]], add_split_mark: bool = True
            ) -> str:
                """为分段添加分隔标记"""
                html = "".join(element["html"] for element in content)
                if not html.strip():
                    return ""

                # 确保有根元素
                if not html.strip().startswith("<div"):
                    html = f"<div>{html}</div>"

                if add_split_mark:
                    return f"<!-- SPLIT_POINT -->{html}"
                return html

            def is_block_boundary(element: Dict[str, Any]) -> bool:
                """判断是否是块级边界"""
                for tag_set in self.BLOCK_TAGS:
                    if element["tag"] in tag_set:
                        return True
                return False

            def is_semantic_pair(
                current: Dict[str, Any], next_elem: Dict[str, Any]
            ) -> bool:
                """检查两个元素是否构成语义单元"""
                if not current or not next_elem:
                    return False
                for first, second in self.SEMANTIC_UNITS:
                    if current["tag"] == first and next_elem["tag"] == second:
                        return True
                return False

            elements = structure["elements"]
            segments = []
            current_segment = []
            current_tokens = 0
            i = 0

            while i < len(elements):
                element = elements[i]
                next_element = elements[i + 1] if i + 1 < len(elements) else None

                # 跳过元数据标签
                if element.get("is_metadata"):
                    i += 1
                    continue

                # 计算添加当前元素后的token数
                element_tokens = element["token_estimate"]
                next_tokens = current_tokens + element_tokens

                # 检查是否需要分段
                should_split = False

                # 1. Token限制检查（考虑翻译膨胀）
                max_segment_tokens = int(self.max_tokens / 1.5)  # 预留膨胀空间
                if next_tokens > max_segment_tokens:
                    should_split = True
                    # 如果当前元素本身就超过限制，需要进一步分割
                    if element_tokens > max_segment_tokens:
                        # 尝试分割当前元素的内容
                        soup = BeautifulSoup(element["html"], "html.parser")
                        text_nodes = soup.find_all(string=True)
                        current_text = []
                        current_text_tokens = 0

                        for text in text_nodes:
                            text_str = str(text)
                            text_tokens = self._count_tokens(text_str)

                            if current_text_tokens + text_tokens > max_segment_tokens:
                                if current_text:
                                    segments.append(
                                        wrap_segment(
                                            [
                                                {
                                                    "tag": "div",
                                                    "html": "".join(current_text),
                                                }
                                            ],
                                            len(segments) > 0,
                                        )
                                    )
                                current_text = [text_str]
                                current_text_tokens = text_tokens
                            else:
                                current_text.append(text_str)
                                current_text_tokens += text_tokens

                        if current_text:
                            segments.append(
                                wrap_segment(
                                    [{"tag": "div", "html": "".join(current_text)}],
                                    len(segments) > 0,
                                )
                            )
                        i += 1
                        continue

                # 2. 块级边界检查
                if current_tokens > 0 and is_block_boundary(element):
                    # 除非与下一个元素构成语义单元
                    if not is_semantic_pair(element, next_element):
                        should_split = True

                if should_split and current_segment:
                    segments.append(wrap_segment(current_segment, len(segments) > 0))
                    current_segment = []
                    current_tokens = 0

                current_segment.append(element)
                current_tokens = next_tokens
                i += 1

            # 添加最后一段
            if current_segment:
                segments.append(wrap_segment(current_segment, len(segments) > 0))

            return [s for s in segments if s.strip()]  # 过滤空分段
        except Exception as e:
            raise HTMLProcessingError(f"分段失败: {str(e)}") from e

    async def restore_content(self, html: str, mapping: Dict[str, Any]) -> str:
        """还原被保护的内容

        处理流程：
        1. 验证输入
        2. 按占位符长度排序（避免部分替换）
        3. 还原标签及其属性
        4. 验证结果

        Args:
            html: 包含占位符的HTML
            mapping: 占位符到原始内容的映射表

        Returns:
            还原后的HTML

        Raises:
            ContentRestoreError: 还原过程中的错误
        """
        try:
            if not isinstance(html, str):
                raise ContentRestoreError("输入HTML必须是字符串类型")
            if not isinstance(mapping, dict):
                raise ContentRestoreError("映射必须是字典类型")

            # 按占位符长度排序
            placeholders = sorted(mapping.keys(), key=len, reverse=True)
            result = html

            for placeholder in placeholders:
                if placeholder not in result:
                    raise ContentRestoreError(f"找不到占位符: {placeholder}")

                item = mapping[placeholder]
                if not isinstance(item, dict) or "type" not in item:
                    raise ContentRestoreError(f"无效的映射信息: {placeholder}")

                # 使用完整的标签内容进行还原
                if "content" in item:
                    content = RestoreStrategy.from_complete_content(item)
                else:
                    # 如果没有完整内容，尝试重建标签
                    tag_content = (
                        item["structure"]["inner_html"]
                        if "structure" in item and "inner_html" in item["structure"]
                        else ""
                    )
                    tag_attrs = " ".join(
                        f'{k}="{v}"' if v is not None else k
                        for k, v in item["attributes"].items()
                    )
                    tag_name = item["name"]
                    rebuilt_tag = f"<{tag_name} {tag_attrs}>{tag_content}</{tag_name}>"
                    content = rebuilt_tag

                result = result.replace(placeholder, content)

            # 验证结果
            if any(key in result for key in mapping.keys()):
                raise ContentRestoreError("部分占位符未被替换")

            # 验证HTML结构
            soup = BeautifulSoup(result, "html.parser")
            if not soup.find():
                raise ContentRestoreError("还原后的HTML无效")

            return str(soup)

        except Exception as e:
            if isinstance(e, ContentRestoreError):
                raise
            raise ContentRestoreError(f"还原内容失败: {str(e)}") from e
