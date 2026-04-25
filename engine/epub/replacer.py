import re
from copy import copy, deepcopy

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from engine.agents.verifier import verify_final_html
from engine.core.logger import engine_logger as logger
from engine.core.markup import get_markup_parser
from engine.item import PreCodeExtractor
from engine.item.xpath import find_by_xpath, get_xpath
from engine.schemas import Chunk, EpubItem, TranslationStatus


class DomReplacer:
    """
    使用 xpath 将翻译后的 chunks 恢复到原始 HTML。

    替代旧的 Merger + Replacer 方案：
    - 旧方案：字符串拼接 + 占位符替换
    - 新方案：在原始 DOM 上精确替换翻译后的元素
    """

    NAV_MARKER_PATTERN = re.compile(r"\[NAVTXT:\d+\]")
    SECONDARY_PLACEHOLDER_PATTERN = re.compile(r"\[(PRE|CODE|STYLE):\d+\]")
    WRITEBACK_TRACK_ATTR = "data-epubox-wb-id"

    @staticmethod
    def _xpath_depth(xpath: str) -> int:
        return len([part for part in (xpath or "").split("/") if part])

    @staticmethod
    def _is_xpath_ancestor(ancestor: str, descendant: str) -> bool:
        if not ancestor or not descendant or ancestor == descendant:
            return False
        return descendant.startswith(f"{ancestor}/")

    def _build_writeback_soup(self, item: EpubItem) -> BeautifulSoup:
        """
        用与分块阶段一致的预处理 DOM 做 xpath 回写。

        PreCodeExtractor 会把部分节点替换成占位符文本；如果直接在原始 DOM 上
        回写，同名兄弟索引可能漂移，导致 chunk 记录的 xpath 对不上节点。
        """
        source = item.content
        if item.preserved_pre or item.preserved_code or item.preserved_style:
            parser = get_markup_parser(item.content)
            normalized = str(BeautifulSoup(item.content, parser))
            pre_extractor = PreCodeExtractor()
            source = pre_extractor.extract(normalized)
        return BeautifulSoup(source, get_markup_parser(source))

    def _build_writeback_locator_map(self, soup) -> dict[str, str]:
        locator_map: dict[str, str] = {}
        counter = 0
        for element in soup.find_all(True):
            marker = f"wb-{counter}"
            counter += 1
            element.attrs[self.WRITEBACK_TRACK_ATTR] = marker
            locator_map[get_xpath(element)] = marker
        return locator_map

    def _find_writeback_target(self, soup, xpath: str, locator_map: dict[str, str]):
        marker = locator_map.get(xpath)
        if marker:
            tracked = soup.find(attrs={self.WRITEBACK_TRACK_ATTR: marker})
            if tracked is not None:
                return tracked
        return find_by_xpath(soup, xpath)

    def _strip_writeback_tracking_attrs(self, soup) -> None:
        for element in soup.find_all(True):
            element.attrs.pop(self.WRITEBACK_TRACK_ATTR, None)

    def restore(self, item: EpubItem) -> str | None:
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

        self._mark_overlapping_chunks(item)

        # 1. 解析原始 HTML
        soup = self._build_writeback_soup(item)
        locator_map = self._build_writeback_locator_map(soup)

        # 2. 按 xpath 替换
        for chunk in item.chunks:
            if not chunk.translated or chunk.status in (
                TranslationStatus.ACCEPTED_AS_IS,
                TranslationStatus.TRANSLATION_FAILED,
                TranslationStatus.WRITEBACK_FAILED,
            ):
                continue
            if chunk.chunk_mode == "nav_text":
                writeback_ok = self._replace_nav_text(soup, chunk)
            else:
                writeback_ok = self._replace_by_xpaths(soup, chunk, locator_map)
            if not writeback_ok:
                chunk.status = TranslationStatus.WRITEBACK_FAILED

        # 3. 恢复 PreCodeExtractor 占位符
        self._strip_writeback_tracking_attrs(soup)
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
            recovered = self._recover_valid_writeback(item)
            if recovered is not None:
                item.translated = recovered
                return recovered
            self._mark_writeback_failed_chunks(item, error)
            item.translated = None
            return None

        # 5. 更新 item
        item.translated = result

        return result

    def _render_soup_with_restored_placeholders(self, soup, item: EpubItem) -> str:
        self._strip_writeback_tracking_attrs(soup)
        result = str(soup)
        if item.preserved_pre or item.preserved_code or item.preserved_style:
            pre_extractor = PreCodeExtractor()
            pre_extractor.preserved_pre = item.preserved_pre or []
            pre_extractor.preserved_code = item.preserved_code or []
            pre_extractor.preserved_style = item.preserved_style or []
            result = pre_extractor.restore(result)
        return result

    def _recover_valid_writeback(self, item: EpubItem) -> str | None:
        """Fallback path: replay chunks one by one and keep only writes that preserve item-level validity."""
        soup = self._build_writeback_soup(item)
        locator_map = self._build_writeback_locator_map(soup)
        recovered_any = False

        for chunk in item.chunks or []:
            if not chunk.translated or chunk.status in (
                TranslationStatus.ACCEPTED_AS_IS,
                TranslationStatus.TRANSLATION_FAILED,
                TranslationStatus.WRITEBACK_FAILED,
            ):
                continue

            trial_soup = deepcopy(soup)
            if chunk.chunk_mode == "nav_text":
                writeback_ok = self._replace_nav_text(trial_soup, chunk)
            else:
                writeback_ok = self._replace_by_xpaths(trial_soup, chunk, locator_map)
            if not writeback_ok:
                chunk.status = TranslationStatus.WRITEBACK_FAILED
                continue

            candidate = self._render_soup_with_restored_placeholders(trial_soup, item)
            is_valid, error = verify_final_html(item.content, candidate)
            if not is_valid:
                logger.warning(f"Chunk {chunk.name}: 单块回写后仍导致 item 校验失败，已跳过: {error}")
                chunk.status = TranslationStatus.WRITEBACK_FAILED
                continue

            soup = trial_soup
            locator_map = self._build_writeback_locator_map(soup)
            recovered_any = True

        final_result = self._render_soup_with_restored_placeholders(soup, item)
        is_valid, error = verify_final_html(item.content, final_result)
        if is_valid:
            return final_result
        if recovered_any:
            logger.error(f"HTML 结构验证失败: {item.id}, 分块级恢复后仍无有效结果: {error}")
        return None

    def _mark_overlapping_chunks(self, item: EpubItem) -> None:
        active_chunks = [
            chunk
            for chunk in (item.chunks or [])
            if chunk.chunk_mode != "nav_text"
            and chunk.translated
            and chunk.status
            not in (
                TranslationStatus.ACCEPTED_AS_IS,
                TranslationStatus.TRANSLATION_FAILED,
                TranslationStatus.WRITEBACK_FAILED,
            )
        ]

        if len(active_chunks) < 2:
            return

        for index, chunk in enumerate(active_chunks):
            chunk_paths = chunk.xpaths or []
            if not chunk_paths:
                continue
            chunk_depth = min(self._xpath_depth(xpath) for xpath in chunk_paths)

            for other_index, other_chunk in enumerate(active_chunks):
                if index == other_index:
                    continue
                other_paths = other_chunk.xpaths or []
                if not other_paths:
                    continue
                other_depth = min(self._xpath_depth(xpath) for xpath in other_paths)

                overlaps_descendant = any(
                    self._is_xpath_ancestor(xpath, other_xpath) for xpath in chunk_paths for other_xpath in other_paths
                )
                if not overlaps_descendant:
                    continue

                if chunk_depth < other_depth:
                    logger.warning(f"Chunk {chunk.name}: 检测到与更具体 xpath 重叠，跳过整块回写以保留更细粒度分块")
                    chunk.status = TranslationStatus.WRITEBACK_FAILED
                    break

    def _mark_writeback_failed_chunks(self, item: EpubItem, error: str) -> None:
        """将导致最终文件校验失败的分块回退为 WRITEBACK_FAILED，便于进入人工处理报告。"""
        placeholder_pattern = re.compile(r"\[(PRE|CODE|STYLE):\d+\]")
        affected = 0

        if "残留占位符" in error:
            for chunk in item.chunks or []:
                if chunk.status in (TranslationStatus.TRANSLATION_FAILED, TranslationStatus.WRITEBACK_FAILED):
                    continue
                if chunk.translated and placeholder_pattern.search(chunk.translated):
                    chunk.status = TranslationStatus.WRITEBACK_FAILED
                    affected += 1

        if affected:
            return

        for chunk in item.chunks or []:
            if chunk.status in (TranslationStatus.TRANSLATION_FAILED, TranslationStatus.WRITEBACK_FAILED):
                continue
            if chunk.translated:
                chunk.status = TranslationStatus.WRITEBACK_FAILED

    def _replace_by_xpaths(self, soup, chunk: Chunk, locator_map: dict[str, str]):
        """
        解析 chunk 的翻译结果，按 xpath 逐个替换原始 DOM 中的元素

        关键假设：翻译后的 HTML 与原始 chunk 有相同数量、相同顺序的顶层元素
        """
        if chunk.translated is None:
            logger.warning(f"Chunk {chunk.name}: 缺少译文，放弃整块回写")
            return False

        translated_soup = BeautifulSoup(chunk.translated, "html.parser")
        translated_elements = [e for e in translated_soup.children if isinstance(e, Tag)]

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
            original_element = self._find_writeback_target(trial_soup, xpath, locator_map)
            if not original_element:
                logger.warning(f"Chunk {chunk.name}: xpath '{xpath}' 未找到对应元素，放弃整块回写")
                return False
            translated_copy = copy(translated_elements[i])
            translated_copy.attrs.pop(self.WRITEBACK_TRACK_ATTR, None)
            original_element.replace_with(translated_copy)

        soup.clear()
        for child in list(trial_soup.contents):
            soup.append(child)
        return True

    def _extract_nav_segments(self, text: str) -> list[tuple[str, str]]:
        matches = list(self.NAV_MARKER_PATTERN.finditer(text))
        segments: list[tuple[str, str]] = []
        for i, match in enumerate(matches):
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            marker = match.group(0)
            payload = text[start:end].strip()
            segments.append((marker, payload))
        return segments

    def _collect_translatable_text_nodes(self, element) -> list[NavigableString]:
        nodes: list[NavigableString] = []
        for child in element.contents:
            if not isinstance(child, NavigableString):
                continue
            text = str(child).strip()
            if not text:
                continue
            clean_text = self.SECONDARY_PLACEHOLDER_PATTERN.sub("", text)
            if not clean_text.strip():
                continue
            nodes.append(child)
        return nodes

    def _replace_nav_text(self, soup, chunk: Chunk) -> bool:
        if chunk.translated is None:
            logger.warning(f"Chunk {chunk.name}: 缺少导航译文，放弃整块回写")
            return False

        segments = self._extract_nav_segments(chunk.translated)
        expected_markers = [target.marker for target in chunk.nav_targets]
        translated_markers = [marker for marker, _ in segments]
        if translated_markers != expected_markers:
            logger.warning(
                f"Chunk {chunk.name}: 导航标记不一致，期望 {expected_markers}，实际 {translated_markers}，放弃整块回写"
            )
            return False

        segment_map = {marker: payload for marker, payload in segments}
        trial_soup = deepcopy(soup)

        for target in chunk.nav_targets:
            translated_text = segment_map.get(target.marker, "")
            if not translated_text:
                logger.warning(f"Chunk {chunk.name}: 导航标记 {target.marker} 缺少译文，放弃整块回写")
                return False

            parent_element = find_by_xpath(trial_soup, target.xpath)
            if not parent_element:
                logger.warning(f"Chunk {chunk.name}: 导航 xpath '{target.xpath}' 未找到，放弃整块回写")
                return False

            text_nodes = self._collect_translatable_text_nodes(parent_element)
            if target.text_index >= len(text_nodes):
                logger.warning(
                    f"Chunk {chunk.name}: 导航文本索引越界 ({target.text_index}/{len(text_nodes)})，放弃整块回写"
                )
                return False

            text_nodes[target.text_index].replace_with(translated_text)

        soup.clear()
        for child in list(trial_soup.contents):
            soup.append(child)
        return True
