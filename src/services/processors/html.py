"""HTML处理器模块"""

import asyncio
import copy
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import tiktoken
from bs4 import BeautifulSoup, Comment, NavigableString

from ..translation.translation_service import TranslationService


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


class HTMLProcessor:
    """HTML内容处理组件

    职责：
    1. 处理从EPUB提取的HTML内容
    2. 确保内容分段不超过翻译服务的token限制
    3. 保持HTML结构和语义的完整性
    4. 处理长文档的分割和合并
    5. 保护和还原特殊HTML内容
    """

    __slots__ = ["translation_service", "max_tokens", "logger"]

    # 类级别的常量
    NON_TRANSLATABLE_TAGS = {"script", "style", "pre", "code", "head", "meta", "link"}
    METADATA_TAGS = {"title", "meta"}
    BLOCK_TAGS = [
        # 文档元数据
        "title",
        "meta",
        # 块级元素
        "div",
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "table",
        "ul",
        "ol",
        "li",
        "section",
        "article",
        "aside",
        "header",
        "footer",
        "nav",
        "figure",
        "figcaption",
        "details",
        "summary",
        # 表单元素
        "form",
        "fieldset",
        "legend",
        "label",
        # 其他块级元素
        "hr",
        "address",
    ]
    INLINE_TAGS = ["span", "a", "em", "strong", "b", "i", "u", "s", "sub", "sup"]
    SEMANTIC_UNITS = [
        ("h1", "p"),  # 标题和其后的第一段
        ("h2", "p"),
        ("h3", "p"),
        ("li", "p"),  # 列表项和其中的段落
        ("p", "blockquote"),  # 引用和其上下文
    ]

    # 需要保护的属性
    PROTECTED_ATTRIBUTES = {
        "xmlns",
        "epub:type",
        "data-type",
        "data-pdf-bookmark",
        "class",
        "id",
        "style",
        "lang",
        "dir",
    }

    def __init__(self, translation_service: TranslationService):
        """初始化HTML处理器"""
        self.translation_service = translation_service
        self.max_tokens = translation_service.get_token_limit()
        self.logger = logging.getLogger(__name__)

    def _count_tokens(self, text: str) -> int:
        """使用tiktoken准确计算token数量"""
        return len(tiktoken.get_encoding("cl100k_base").encode(text))

    async def process_content(
        self, content: Dict[str, Any], source_lang: str, target_lang: str
    ) -> Dict[str, Any]:
        """处理从EPUB提取的内容

        Args:
            content: EPUBProcessor提取的内容字典
                {
                    "id": str,           # 文件ID
                    "file_name": str,    # 文件名
                    "media_type": str,   # 媒体类型
                    "content": str       # HTML内容
                }
            source_lang: 源语言
            target_lang: 目标语言

        Returns:
            处理后的内容字典，保持相同的结构

        处理流程：
        1. 预处理：识别和保护不可翻译内容
        2. 分析结构：解析HTML树，识别可翻译节点
        3. 分段处理：按照语义和token限制分割内容
        4. 翻译处理：调用翻译服务
        5. 内容重组：合并翻译结果，还原HTML结构
        """
        try:
            html_content = content["content"]
            self.logger.info(f"原始HTML内容: {html_content}")

            # 1. 预处理：保护不需要翻译的内容
            processed_html, placeholders = await self.preprocess(html_content)
            self.logger.info(f"预处理后的HTML: {processed_html}")

            # 2. 分析结构
            segments, placeholders = await self.analyze_structure(processed_html)
            self.logger.info(f"分析的结构: {segments}")

            # 3. 分段处理
            translated_segments = []
            for segment in segments:
                self.logger.info(f"发送给翻译服务的文本: {segment}")
                translated_text = await self.translation_service.translate(
                    segment, source_lang=source_lang, target_lang=target_lang
                )
                self.logger.info(f"翻译后的文本: {translated_text}")
                translated_segments.append(translated_text)

            # 5. 内容重组：还原占位符，保持原始结构
            restored_html = await self.restore_content(
                html_content, translated_segments, placeholders
            )
            self.logger.info(f"还原后的HTML: {restored_html}")

            # 返回结果
            return {
                "id": content["id"],
                "file_name": content["file_name"],
                "media_type": content["media_type"],
                "content": restored_html,
            }

        except Exception as e:
            self.logger.error(f"处理HTML内容失败: {str(e)}")
            raise HTMLProcessingError(f"处理失败: {str(e)}") from e

    async def preprocess(self, html_content: str) -> Tuple[str, Dict[str, Any]]:
        """预处理HTML内容，保护不需要翻译的部分

        Args:
            html_content: HTML内容

        Returns:
            处理后的HTML内容和占位符映射
            {
                "序号_标签类型_uuid": {
                    "content": 原始内容,
                    "index": 序号
                }
            }
        """
        try:
            soup = BeautifulSoup(html_content, "html.parser")
            placeholders = {}
            placeholder_index = 0

            # 处理注释
            for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
                placeholder = self._generate_placeholder(f"{placeholder_index}_comment")
                placeholders[placeholder] = {
                    "content": str(comment),
                    "index": placeholder_index,
                    "type": "comment",
                }
                comment.replace_with(placeholder)
                placeholder_index += 1

            # 处理不需要翻译的标签
            for tag in soup.find_all(self.NON_TRANSLATABLE_TAGS):
                placeholder = self._generate_placeholder(
                    f"{placeholder_index}_{tag.name}"
                )
                placeholders[placeholder] = {
                    "content": str(tag),
                    "index": placeholder_index,
                    "type": "tag",
                }
                tag.replace_with(placeholder)
                placeholder_index += 1

            # 保护特殊属性
            for tag in soup.find_all(True):
                protected_attrs = {}
                for attr, value in list(tag.attrs.items()):
                    # 检查是否需要保护这个属性
                    if (
                        attr in self.PROTECTED_ATTRIBUTES
                        or attr.startswith("data-")
                        or ":" in attr
                    ):  # 处理命名空间属性
                        protected_attrs[attr] = value
                        del tag[attr]

                if protected_attrs:
                    placeholder = self._generate_placeholder(
                        f"{placeholder_index}_attrs"
                    )
                    placeholders[placeholder] = {
                        "content": protected_attrs,
                        "index": placeholder_index,
                        "type": "attributes",
                        "tag_index": len(placeholders),  # 用于还原时定位标签
                    }
                    # 添加占位符作为属性
                    tag["data-attrs-placeholder"] = placeholder
                    placeholder_index += 1

            return str(soup), placeholders

        except Exception as e:
            raise HTMLProcessingError(f"预处理失败: {str(e)}") from e

    def _generate_placeholder(self, tag_type: str) -> str:
        """生成唯一的占位符"""
        import uuid

        unique_id = str(uuid.uuid4())[:8]  # 使用UUID的前8位作为唯一标识
        placeholder = f"___{tag_type}_{unique_id}___"
        return placeholder

    async def analyze_structure(self, html: str) -> Tuple[List[str], Dict[str, Any]]:
        """分析HTML结构，提取可翻译内容并替换为占位符

        Args:
            html: HTML内容

        Returns:
            (分段列表, 占位符映射)
        """
        try:
            # 解析HTML
            soup = BeautifulSoup(html, "html.parser")

            # 找到body元素
            body = soup.find("body")
            if not body:
                return [], {}

            # 找到所有直接子块级元素
            block_elements = []
            for child in body.children:
                if hasattr(child, "name") and child.name in self.BLOCK_TAGS:
                    block_elements.append(child)

            if not block_elements:
                return [], {}

            # 将所有块级元素合并为一个字符串
            content_html = "".join(str(element) for element in block_elements)

            # 计算token数量
            token_count = self._count_tokens(content_html)
            if token_count > self.max_tokens:
                # 如果超过限制，按块级元素分段
                segments = []
                current_segment = []
                current_tokens = 0

                for element in block_elements:
                    element_html = str(element)
                    element_tokens = self._count_tokens(element_html)

                    if current_tokens + element_tokens > self.max_tokens:
                        if current_segment:
                            segments.append("".join(current_segment))
                            current_segment = []
                            current_tokens = 0

                    current_segment.append(element_html)
                    current_tokens += element_tokens

                if current_segment:
                    segments.append("".join(current_segment))
            else:
                # 不超过限制，作为一个整体
                segments = [content_html]

            return segments, {}

        except Exception as e:
            raise HTMLProcessingError(f"HTML结构分析失败: {str(e)}") from e

    async def restore_content(
        self, html: str, translated_segments: List[str], placeholders: Dict[str, Any]
    ) -> str:
        """还原被保护的内容

        Args:
            html: 原始HTML
            translated_segments: 翻译后的分段（包含HTML标签）
            placeholders: 占位符映射，包含序号信息

        Returns:
            还原后的HTML
        """
        try:
            # 解析原始HTML和翻译后的HTML
            original_soup = BeautifulSoup(html, "html.parser")
            translated_soup = BeautifulSoup("".join(translated_segments), "html.parser")

            # 1. 还原结构
            body = original_soup.find("body")
            translated_body = translated_soup.find("body")
            if body and translated_body:
                # 保存原始属性
                body_attrs = body.attrs.copy()
                body.clear()

                # 复制翻译后的内容
                for content in translated_body.contents:
                    body.append(copy.copy(content))

                # 恢复原始属性
                body.attrs = body_attrs

            # 2. 还原占位符内容
            # 按照索引排序，确保正确的还原顺序
            sorted_placeholders = sorted(
                placeholders.items(), key=lambda x: x[1]["index"]
            )

            for placeholder, info in sorted_placeholders:
                if info["type"] == "attributes":
                    # 还原属性
                    tags = original_soup.find_all(
                        attrs={"data-attrs-placeholder": placeholder}
                    )
                    for tag in tags:
                        # 删除占位符属性
                        del tag["data-attrs-placeholder"]
                        # 还原保护的属性
                        for attr, value in info["content"].items():
                            tag[attr] = value
                else:
                    # 还原其他类型的占位符（注释、不可翻译标签等）
                    placeholder_tags = original_soup.find_all(string=placeholder)
                    for tag in placeholder_tags:
                        if info["type"] == "comment":
                            tag.replace_with(Comment(info["content"]))
                        else:
                            tag.replace_with(
                                BeautifulSoup(info["content"], "html.parser")
                            )

            return str(original_soup)

        except Exception as e:
            raise HTMLProcessingError(f"内容还原失败: {str(e)}") from e

    async def translate_html(
        self,
        html: str,
        source_lang: str,
        target_lang: str,
        translator: TranslationService,
    ) -> str:
        """递归翻译HTML内容

        Args:
            html: HTML内容
            source_lang: 源语言
            target_lang: 目标语言
            translator: 翻译器实例

        Returns:
            翻译后的HTML

        处理流程：
        1. 预处理HTML，保护不需要翻译的内容
        2. 检查token数量：
           - 如果不超过限制，直接整体翻译
           - 如果超过限制，递归处理HTML结构
        3. 还原HTML结构
        """
        try:
            self.logger.info(
                f"开始翻译HTML，源语言: {source_lang}，目标语言: {target_lang}"
            )

            # 1. 预处理HTML
            processed_html, placeholders = await self.preprocess(html)

            # 2. 检查token数量
            tokens = self._count_tokens(processed_html)
            if tokens <= self.max_tokens:
                # 如果不超过限制，直接整体翻译
                translated_html = await translator.translate(
                    processed_html, source_lang=source_lang, target_lang=target_lang
                )
                translated_segments = [translated_html]
            else:
                # 如果超过限制，递归处理HTML结构
                soup = BeautifulSoup(processed_html, "html.parser")
                body = soup.find("body")
                if body:
                    await self._translate_element(
                        body, source_lang, target_lang, translator
                    )
                translated_segments = [str(soup)]

            # 3. 还原占位符
            return await self.restore_content(html, translated_segments, placeholders)

        except Exception as e:
            raise HTMLProcessingError(f"HTML翻译失败: {str(e)}") from e

    async def _translate_element(
        self,
        element: BeautifulSoup,
        source_lang: str,
        target_lang: str,
        translator: TranslationService,
    ) -> None:
        """递归翻译HTML元素

        Args:
            element: BeautifulSoup元素
            source_lang: 源语言
            target_lang: 目标语言
            translator: 翻译器实例
        """
        # 跳过注释和特殊标签
        if isinstance(element, Comment) or (
            hasattr(element, "name") and element.name in self.NON_TRANSLATABLE_TAGS
        ):
            return

        # 如果是文本节点且不为空，翻译它
        if isinstance(element, NavigableString) and str(element).strip():
            content = str(element)
            tokens = self._count_tokens(content)

            if tokens > self.max_tokens:
                raise TokenLimitError(
                    f"文本内容超过token限制: {tokens} > {self.max_tokens}"
                )

            translated = await translator.translate(
                content, source_lang=source_lang, target_lang=target_lang
            )
            element.replace_with(translated)
            return

        # 如果是元素节点
        if hasattr(element, "name"):
            content = element.decode_contents()
            if content.strip():
                tokens = self._count_tokens(content)

                if tokens <= self.max_tokens:
                    # 如果不超过限制，整体翻译
                    translated = await translator.translate(
                        content, source_lang=source_lang, target_lang=target_lang
                    )
                    element.clear()
                    element.append(BeautifulSoup(translated, "html.parser"))
                else:
                    # 如果超过限制，递归处理子元素
                    for child in list(element.children):
                        await self._translate_element(
                            child, source_lang, target_lang, translator
                        )
