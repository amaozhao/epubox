from copy import copy, deepcopy

from bs4 import BeautifulSoup

from engine.agents.verifier import verify_final_html
from engine.core.logger import engine_logger as logger
from engine.item import PreCodeExtractor
from engine.item.xpath import find_by_xpath
from engine.schemas import Chunk, EpubItem, TranslationStatus


class DomReplacer:
    """
    使用 xpath 将翻译后的 chunks 恢复到原始 HTML。

    替代旧的 Merger + Replacer 方案：
    - 旧方案：字符串拼接 + 占位符替换
    - 新方案：在原始 DOM 上精确替换翻译后的元素
    """

    def restore(self, item: EpubItem) -> str:
        """
        将翻译后的 chunks 恢复到原始 HTML

        步骤：
        1. 解析原始 HTML 为 DOM 树
        2. 按 xpath 逐个替换翻译后的元素
        3. 恢复 PreCodeExtractor 占位符
        4. 验证最终 HTML 结构

        Returns:
            恢复后的完整 HTML 字符串
        """
        if not item.chunks:
            return item.content

        # 1. 解析原始 HTML
        soup = BeautifulSoup(item.content, "html.parser")

        # 2. 按 xpath 替换
        for chunk in item.chunks:
            if not chunk.translated or chunk.status in (
                TranslationStatus.ACCEPTED_AS_IS,
                TranslationStatus.TRANSLATION_FAILED,
                TranslationStatus.WRITEBACK_FAILED,
            ):
                continue
            if not self._replace_by_xpaths(soup, chunk):
                chunk.status = TranslationStatus.WRITEBACK_FAILED

        # 3. 恢复 PreCodeExtractor 占位符
        result = str(soup)
        if item.preserved_pre or item.preserved_code or item.preserved_style:
            pre_extractor = PreCodeExtractor()
            pre_extractor.preserved_pre = item.preserved_pre or []
            pre_extractor.preserved_code = item.preserved_code or []
            pre_extractor.preserved_style = item.preserved_style or []
            result = pre_extractor.restore(result)

        # 4. 验证 HTML 结构
        is_valid, error = verify_final_html(item.content, result)
        if not is_valid:
            logger.error(f"HTML 结构验证失败: {item.id}, 错误: {error}")

        # 5. 更新 item
        item.translated = result

        return result

    def _replace_by_xpaths(self, soup, chunk: Chunk):
        """
        解析 chunk 的翻译结果，按 xpath 逐个替换原始 DOM 中的元素

        关键假设：翻译后的 HTML 与原始 chunk 有相同数量、相同顺序的顶层元素
        """
        translated_soup = BeautifulSoup(chunk.translated, "html.parser")
        translated_elements = [e for e in translated_soup.children if hasattr(e, "name") and e.name]

        # 校验：翻译后元素数量应与 xpath 数量一致
        if len(translated_elements) != len(chunk.xpaths):
            logger.warning(
                f"Chunk {chunk.name}: 翻译后元素数量 ({len(translated_elements)}) "
                f"!= xpath 数量 ({len(chunk.xpaths)})，放弃整块回写"
            )
            return False

        trial_soup = deepcopy(soup)

        # 按 xpath 逐个替换；任一步失败都放弃整块回写，避免混入原文
        for i, xpath in enumerate(chunk.xpaths):
            original_element = find_by_xpath(trial_soup, xpath)
            if not original_element:
                logger.warning(f"Chunk {chunk.name}: xpath '{xpath}' 未找到对应元素，放弃整块回写")
                return False
            original_element.replace_with(copy(translated_elements[i]))

        soup.clear()
        for child in list(trial_soup.contents):
            soup.append(child)
        return True
