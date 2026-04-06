import re
from typing import List

from lxml import etree

from engine.core.logger import engine_logger as logger
from engine.schemas import Chunk, TranslationStatus
from engine.agents.html_validator import HtmlValidator


class Merger:
    """
    Merges a list of chunks' translated content into a single string and updates XHTML language attributes.
    """

    def merge(self, chunks: List[Chunk], language: str = "zh", original_content: str = "") -> str:
        """
        Merges the translated content from a list of chunks and updates lang and xml:lang attributes to the specified language.

        Args:
            chunks: A list of Chunk objects, each containing translated XHTML content.
            language: Language code to set in lang and xml:lang attributes (default 'zh').
            original_content: 原始的完整 HTML 内容（用于提取 <html> 标签和 xmlns 属性）

        Returns:
            A single string with all translated content joined together, with lang and xml:lang attributes updated.
        """
        if not chunks:
            return ""

        # Step 1: 直接使用 chunks 的 translated 或 original
        merged_chunks = []
        for chunk in chunks:
            if chunk.status == TranslationStatus.UNTRANSLATED or not chunk.translated:
                merged_chunks.append(chunk.original)
            else:
                merged_chunks.append(chunk.translated)

        # Step 2: 验证每个 chunk 翻译后的结构
        validator = HtmlValidator()
        for i, (chunk_html, chunk) in enumerate(zip(merged_chunks, chunks)):
            valid, errors = validator.validate_chunk(chunk_html, i, chunk.name)

            # 额外检查：chunk 中有未闭合标签也算验证失败
            chunk_unclosed = [tag for tag, idx in validator.stack if idx == i]

            if not valid or chunk_unclosed:
                if chunk_unclosed:
                    errors = [{"type": "unclosed_tags", "details": chunk_unclosed}]
                logger.warning(
                    f"Chunk[{i}] ({chunk.name}) 翻译后 HTML 结构异常，"
                    f"回退到 original: {errors}"
                )
                merged_chunks[i] = chunk.original
                # 标记为 UNTRANSLATED，这样 replacer 知道内容已回退
                chunk.status = TranslationStatus.UNTRANSLATED

        # Step 3: 注意！不检查 validator.stack，因为跨 chunk 的标签闭合是正常的
        # 例如 <p> 在 chunk 1 打开，在 chunk 2 闭合，这是合法的
        # Step 2 已经验证了每个 chunk 内部的标签配对

        # Step 4: 最终合并
        translated = "".join(merged_chunks)

        # Step 5: 检查并修复 void 元素被错误地写成非自闭合形式
        # 例如 <link ...> 而不是 <link .../>
        void_elements = ["link", "meta", "br", "hr", "img", "input", "area", "base", "col", "embed", "param", "source", "track", "wbr"]
        for tag in void_elements:
            # 匹配 <tag ...> 后面没有 /> 或 </tag> 的情况（未闭合）
            # 匹配 <tag attr="value"> 这种形式
            pattern = rf'<({tag})\s+([^>]+?)(?<!/)>(?!</{tag}>)'
            fixed, count = re.subn(pattern, r'<\1 \2/>', translated, flags=re.IGNORECASE)
            if count > 0:
                logger.warning(f"修复了 {count} 个未闭合的 <{tag}> 标签")
                translated = fixed

        # Step 6: 如果合并结果缺少 <html> 包裹，使用原文重建
        translated = self._ensure_html_wrapper(translated, chunks, original_content)

        # Step 6: 强制确保 DOCTYPE 存在（如果原文有，但翻译后丢失了）
        translated = self._ensure_doctype(translated, chunks, original_content)

        # Step 6: 替换 lang 属性
        lang_pattern = r'lang="en[^"]*"|xml:lang="en[^"]*"'
        if re.search(lang_pattern, translated):
            translated = re.sub(r'lang="en[^"]*"', f'lang="{language}"', translated)
            translated = re.sub(r'xml:lang="en[^"]*"', f'xml:lang="{language}"', translated)
        else:
            logger.warning("合并后的 XHTML 内容中未找到 lang 或 xml:lang 属性匹配 'en*'")

        return translated

    def _ensure_html_wrapper(self, content: str, chunks: List[Chunk], original_content: str = "") -> str:
        """
        确保内容有完整的 XML 声明。

        策略：
        1. 如果原文有 XML 声明但翻译后丢失了，添加回去
        2. 对于 HTML 文档，还需要保留 DOCTYPE
        3. 对于其他 XML 文档（如 NCX），只添加 XML 声明
        """
        stripped = content.strip()
        if not stripped:
            return content

        source = original_content if original_content else (chunks[0].original if chunks else "")

        # 提取原文的 XML 声明
        xml_decl = ""
        xml_match = re.search(r'<\?xml[^?]+\?\>', source)
        if xml_match:
            xml_decl = xml_match.group(0) + "\n"

        # 提取原文的 DOCTYPE
        doctype = ""
        doctype_match = re.search(r'<!DOCTYPE[^>]+>', source, re.IGNORECASE)
        if doctype_match:
            doctype = doctype_match.group(0) + "\n"

        # 检查 translated 是否已经有 XML 声明
        if re.search(r'<\?xml', stripped):
            # 已有 XML 声明，检查是否需要添加 DOCTYPE
            if doctype and not re.search(r'<!DOCTYPE', stripped, re.IGNORECASE):
                return xml_decl + doctype + content
            return content

        # 检查 translated 是否以 DOCTYPE 开头
        if stripped.startswith("<!DOCTYPE") or stripped.startswith("<!doctype"):
            if xml_decl:
                return xml_decl + content
            return content

        # 检查 translated 是否已经有 <html> 包裹
        if stripped.startswith("<html"):
            if xml_decl or doctype:
                return xml_decl + doctype + content
            return content

        # 否则是 fragment，需要包裹
        # 对于 HTML，用 <html> 包裹
        html_match = re.search(r'<html[^>]*>', source, re.IGNORECASE)
        if html_match:
            html_open_tag = html_match.group(0)
            new_content = xml_decl
            new_content += doctype
            new_content += html_open_tag + "\n"
            new_content += stripped + "\n"
            new_content += "</html>\n"
            return new_content

        # 对于其他 XML 文档（如 NCX），只添加 XML 声明
        # 检查是否有任何根元素标签
        root_match = re.match(r'<([a-zA-Z][a-zA-Z0-9]*)[^>]*>', stripped)
        if root_match and xml_decl:
            return xml_decl + content

        return content

        # 如果原文没有 <html> 标签，说明只是简单 fragment，不需要包裹
        if not html_match:
            return content

        html_open_tag = html_match.group(0)

        # 构建完整结构
        new_content = xml_decl
        new_content += doctype
        new_content += html_open_tag + "\n"
        new_content += stripped + "\n"
        new_content += "</html>\n"

        return new_content

    def _ensure_doctype(self, content: str, chunks: List[Chunk], original_content: str = "") -> str:
        """
        确保 DOCTYPE 存在。如果原文有 DOCTYPE 但翻译后丢失了，从原文提取并添加。
        """
        # 如果已经有 DOCTYPE，直接返回
        if re.search(r'<!DOCTYPE', content, re.IGNORECASE):
            return content

        # 从原文提取 DOCTYPE
        source = original_content if original_content else (chunks[0].original if chunks else "")
        doctype_match = re.search(r'<!DOCTYPE[^>]+>', source, re.IGNORECASE)
        if not doctype_match:
            return content  # 原文也没有 DOCTYPE，不需要添加

        doctype = doctype_match.group(0)
        # 提取 XML 声明（如果有）
        xml_decl = ""
        xml_match = re.search(r'<\?xml[^?]+\?>', source)
        if xml_match:
            xml_decl = xml_match.group(0) + "\n"

        # 在 <html> 之前插入 DOCTYPE
        html_pos = content.find("<html")
        if html_pos == -1:
            return content

        new_content = xml_decl + doctype + "\n" + content
        return new_content
