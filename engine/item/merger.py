import re
from typing import List

from engine.core.logger import engine_logger as logger
from engine.schemas import Chunk, TranslationStatus


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

        # Merge parts based on translation status
        parts = []
        for chunk in chunks:
            if chunk.status == TranslationStatus.UNTRANSLATED or not chunk.translated:
                # 失败或翻译为空 → 使用原文保结构
                parts.append(chunk.original)
            else:
                parts.append(chunk.translated)
        translated = "".join(parts)

        # Replace lang and xml:lang attributes
        lang_pattern = r'lang="en[^"]*"|xml:lang="en[^"]*"'
        if re.search(lang_pattern, translated):
            translated = re.sub(r'lang="en[^"]*"', f'lang="{language}"', translated)
            translated = re.sub(r'xml:lang="en[^"]*"', f'xml:lang="{language}"', translated)
        else:
            logger.warning("合并后的 XHTML 内容中未找到 lang 或 xml:lang 属性匹配 'en*'")

        return translated
