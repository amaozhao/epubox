import json
import os
import zipfile
from typing import List, Optional

from bs4 import BeautifulSoup

from engine.item import DomChunker, PreCodeExtractor
from engine.schemas import EpubBook, EpubItem
from engine.schemas.epub import CHECKPOINT_SCHEMA_VERSION
from engine.agents.verifier import verify_html_integrity
from engine.core.logger import engine_logger as logger


class Parser:
    def __init__(self, path: str, limit: int = 1500, secondary_placeholder_limit: int = 12):
        self.limit = limit
        self.secondary_placeholder_limit = secondary_placeholder_limit
        self.path = path
        self.output_dir = self._get_output_dir()

    @property
    def name(self) -> str:
        """获取 EPUB 文件的名称（不带路径和后缀）。"""
        return os.path.splitext(os.path.basename(self.path))[0]

    def _get_output_dir(self) -> str:
        """根据 EPUB 文件名生成解压目录名，与 EPUB 文件在同一目录下。"""
        epub_dir = os.path.dirname(self.path)
        return os.path.join(epub_dir, "temp", self.name)

    @property
    def json_path(self) -> str:
        """根据 EPUB 文件名生成对应的 JSON 文件路径。"""
        return os.path.join(os.path.dirname(self.path), f"{self.name}.json")

    @staticmethod
    def _is_nav_file(relative_path: str) -> bool:
        lowered = relative_path.lower()
        return "toc.ncx" in lowered or lowered.endswith(("nav.xhtml", "toc.xhtml"))

    @staticmethod
    def _has_embedded_toc_nav(html: str) -> bool:
        soup = BeautifulSoup(html, "html.parser")
        for nav in soup.find_all("nav"):
            classes = {cls.lower() for cls in nav.get("class", []) if isinstance(cls, str)}
            if "toc" in classes:
                return True
            for attr in ("epub:type", "type", "role", "id"):
                value = nav.get(attr)
                if not value:
                    continue
                values = value if isinstance(value, list) else [value]
                if any("toc" in str(v).lower() for v in values):
                    return True
        return False

    def _rebuild_nav_item_chunks(self, item: EpubItem, *, is_nav_file: bool) -> None:
        """将导航文件或内嵌目录块重建为文本节点模式。"""
        soup = BeautifulSoup(item.content, "html.parser")
        normalized_content = str(soup)

        pre_extractor = PreCodeExtractor()
        content_after_pre = pre_extractor.extract(normalized_content)

        dom_chunker = DomChunker(
            token_limit=self.limit,
            secondary_placeholder_limit=self.secondary_placeholder_limit,
        )
        item.chunks = dom_chunker.chunk(html=content_after_pre, is_nav_file=is_nav_file)
        item.preserved_pre = pre_extractor.preserved_pre
        item.preserved_code = pre_extractor.preserved_code
        item.preserved_style = pre_extractor.preserved_style

    def _upgrade_legacy_nav_chunks(self, book: EpubBook) -> bool:
        upgraded = False
        for item in book.items:
            is_nav_file = self._is_nav_file(item.id)
            has_embedded_toc = not is_nav_file and self._has_embedded_toc_nav(item.content)
            if not is_nav_file and not has_embedded_toc:
                continue
            if not item.chunks:
                self._rebuild_nav_item_chunks(item, is_nav_file=is_nav_file)
                upgraded = True
                continue

            if any(chunk.chunk_mode == "nav_text" for chunk in item.chunks):
                continue

            self._rebuild_nav_item_chunks(item, is_nav_file=is_nav_file)
            upgraded = True

        if upgraded:
            logger.info("检测到旧版导航/目录 checkpoint，已重建相关 chunk 为 nav_text 模式。")
        return upgraded

    def load_json(self) -> Optional[EpubBook]:
        """尝试从 JSON 文件中加载解析数据，如果成功则返回 EpubBook 对象。"""
        if os.path.exists(self.json_path):
            try:
                with open(self.json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (IOError, json.JSONDecodeError):
                return None

            checkpoint_version = data.get("checkpoint_schema_version")
            if checkpoint_version != CHECKPOINT_SCHEMA_VERSION:
                raise ValueError(
                    "Incompatible checkpoint schema version: "
                    f"expected {CHECKPOINT_SCHEMA_VERSION}, got {checkpoint_version}"
                )

            book = EpubBook.model_validate(data)
            if self._upgrade_legacy_nav_chunks(book):
                self.save_json(book)
            return book
        return None

    def save_json(self, book: EpubBook):
        """将 EpubBook 对象保存到 JSON 文件。"""
        book.checkpoint_schema_version = CHECKPOINT_SCHEMA_VERSION
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump(book.model_dump(), f, ensure_ascii=False, indent=4)

    def extract(self):
        """
        将 EPUB 文件逐个解压到指定的目录。
        如果目标文件已存在，则跳过该文件的解压。
        """
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

        with zipfile.ZipFile(self.path, "r") as zf:
            for member in zf.infolist():
                file_path = os.path.join(self.output_dir, member.filename)

                if member.is_dir():
                    if not os.path.exists(file_path):
                        os.makedirs(file_path)
                    continue

                if os.path.exists(file_path):
                    continue

                zf.extract(member, self.output_dir)

    def parse(self) -> EpubBook:
        """
        解析 EPUB 文件，返回一个只包含可翻译文档的 EpubBook 对象。

        流程：
        1. BeautifulSoup 解析（规范化 HTML）
        2. PreCodeExtractor.extract() - 提取 pre/code/style 标签为占位符
        3. DomChunker.chunk() - DOM 级别分块，返回 List[Chunk]
        """
        # 优先从 JSON 文件加载
        book = self.load_json()
        if book:
            return book

        # 如果 JSON 文件不存在或加载失败，则执行解析逻辑
        self.extract()

        items: List[EpubItem] = []

        for root, dirs, files in os.walk(self.output_dir):
            for file in files:
                file_path = os.path.join(root, file)
                relative_path = os.path.relpath(file_path, self.output_dir)

                if relative_path.lower().endswith((".xhtml", ".html", ".htm", ".xml", ".ncx")):
                    if relative_path.endswith("container.xml"):
                        continue
                    if "META-INF" in relative_path:
                        continue
                    with open(file_path, "r", encoding="utf-8") as f:
                        original_content = f.read()

                    # Step 0: 验证原始 HTML/XML 完整性
                    is_valid, errors = verify_html_integrity(original_content)
                    if not is_valid:
                        logger.warning(f"原始 HTML/XML 结构不完整: {relative_path}, 错误: {errors}")

                    # Step 1: BeautifulSoup 解析（规范化 HTML，确保标签配对）
                    soup = BeautifulSoup(original_content, "html.parser")
                    normalized_content = str(soup)

                    # Step 2: 提取 pre/code/style 标签为占位符（二级占位符方案）
                    pre_extractor = PreCodeExtractor()
                    content_after_pre = pre_extractor.extract(normalized_content)

                    # Step 3: 检测是否是 EPUB 导航文件
                    is_nav_file = self._is_nav_file(relative_path)

                    # Step 4: 使用 DomChunker 进行 DOM 级别分块
                    dom_chunker = DomChunker(
                        token_limit=self.limit,
                        secondary_placeholder_limit=self.secondary_placeholder_limit,
                    )
                    chunks = dom_chunker.chunk(html=content_after_pre, is_nav_file=is_nav_file)

                    epub_item = EpubItem(
                        id=relative_path,
                        path=file_path,
                        content=original_content,
                        chunks=chunks,
                        preserved_pre=pre_extractor.preserved_pre,
                        preserved_code=pre_extractor.preserved_code,
                        preserved_style=pre_extractor.preserved_style,
                    )
                    items.append(epub_item)

        book = EpubBook(name=self.name, path=self.path, items=items, extract_path=self.output_dir)
        self.save_json(book)

        return book


if __name__ == "__main__":
    parser = Parser("/Users/amaozhao/workspace/epubox/depth-leadership-unlocking-unconscious.epub")
    book = parser.parse()
