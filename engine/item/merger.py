import re
from typing import List, Tuple

from engine.core.logger import engine_logger as logger
from engine.schemas import Chunk, TranslationStatus
from engine.agents.html_validator import HtmlValidator


class Merger:
    """
    Merges a list of chunks' translated content into a single string and updates XHTML language attributes.
    """

    def merge(self, chunks: List[Chunk], language: str = "zh") -> str:
        """
        Merges the translated content from a list of chunks and updates lang and xml:lang attributes to the specified language.

        Args:
            chunks: A list of Chunk objects, each containing translated XHTML content.
            language: Language code to set in lang and xml:lang attributes (default 'zh').

        Returns:
            A single string with all translated content joined together, with lang and xml:lang attributes updated.
        """
        if not chunks:
            return ""

        # Step 1: 恢复每个 chunk 的占位符为实际标签
        restored_chunks = []
        for i, chunk in enumerate(chunks):
            if chunk.status == TranslationStatus.UNTRANSLATED or not chunk.translated:
                # 使用 original（已经是占位符形式）
                text = chunk.original
            else:
                text = chunk.translated

            # 恢复占位符为实际标签
            text = self._restore_placeholders(text, chunk.local_tag_map)
            restored_chunks.append(text)

        # Step 2: 栈验证 - 每合并一个 chunk 立即验证
        # 使用 validate_chunk 累积追踪标签状态
        validator = HtmlValidator()

        for i, (chunk_html, chunk) in enumerate(zip(restored_chunks, chunks)):
            # 验证当前 chunk，更新累积的栈状态
            valid, errors = validator.validate_chunk(chunk_html, i, chunk.name)

            if not valid:
                logger.warning(
                    f"Chunk[{i}] ({chunk.name}) 翻译后 HTML 结构异常，"
                    f"回退到 original: {errors}"
                )
                # 回退到 original（保持占位符形式）
                original_text = self._restore_placeholders(
                    chunk.original,
                    chunk.local_tag_map
                )
                restored_chunks[i] = original_text

        # 最终检查：栈中是否有未闭合的标签
        if validator.stack:
            unclosed = []
            for tag, chunk_idx in reversed(validator.stack):
                unclosed.append(f"</{tag}> (来自 Chunk[{chunk_idx}])")
            logger.warning(f"合并后有未闭合的标签: {unclosed}")

        # Step 3: 最终合并
        translated = "".join(restored_chunks)

        # Step 4: 替换 lang 属性
        lang_pattern = r'lang="en[^"]*"|xml:lang="en[^"]*"'
        if re.search(lang_pattern, translated):
            translated = re.sub(r'lang="en[^"]*"', f'lang="{language}"', translated)
            translated = re.sub(r'xml:lang="en[^"]*"', f'xml:lang="{language}"', translated)
        else:
            logger.warning("合并后的 XHTML 内容中未找到 lang 或 xml:lang 属性匹配 'en*'")

        return translated

    def _restore_placeholders(self, text: str, local_tag_map: dict) -> str:
        """
        将占位符 [id0], [id1] 等替换为实际标签

        按任意顺序遍历替换即可，因为 str.replace() 是精确字符串匹配，
        不存在子串冲突问题（如 [id1] 不会错误匹配 [id10] 中的部分）。
        """
        if not local_tag_map:
            return text

        for placeholder, original in local_tag_map.items():
            text = text.replace(placeholder, original)

        return text
