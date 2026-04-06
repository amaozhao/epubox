import json
import os
import re
import uuid
import zipfile
from typing import List, Optional

from bs4 import BeautifulSoup

from engine.agents.html_validator import HtmlValidator
from engine.agents.verifier import verify_html_integrity
from engine.core.logger import engine_logger as logger
from engine.item import PreCodeExtractor, chunk_html, add_context_to_chunks, count_tokens
from engine.schemas import Chunk, EpubBook, EpubItem
from engine.schemas.translator import TranslationStatus


class Parser:
    def __init__(self, path: str, limit: int = 1200):
        self.limit = limit
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

    def load_json(self) -> Optional[EpubBook]:
        """尝试从 JSON 文件中加载解析数据，如果成功则返回 EpubBook 对象。"""
        if os.path.exists(self.json_path):
            try:
                with open(self.json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                book = EpubBook.model_validate(data)
                return book
            except (IOError, json.JSONDecodeError):
                return None
        return None

    def save_json(self, book: EpubBook):
        """将 EpubBook 对象保存到 JSON 文件。"""
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

        新流程：
        1. BeautifulSoup 解析（规范化 HTML）
        2. PreCodeExtractor.extract() - 提取 pre/code 标签
        3. HtmlChunker.chunk_by_html_tags() - 按块级标签分割
        4. Create Chunk objects directly from ChunkState (HTML tags preserved)
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
                    # XML 文件（如 toc.ncx）本身是格式良好的，不需要规范化处理
                    needs_xml = original_content.strip().startswith("<?xml") or original_content.strip().startswith(
                        "<ncx"
                    )
                    if needs_xml:
                        # XML 文件直接使用原始内容，避免 BeautifulSoup 修改标签大小写
                        normalized_content = original_content
                    else:
                        soup = BeautifulSoup(original_content, "html.parser")
                        normalized_content = str(soup)

                    # Step 2: 提取 pre/code/style 标签为占位符（二级占位符方案）
                    pre_extractor = PreCodeExtractor()
                    content_after_pre = pre_extractor.extract(normalized_content)

                    # Step 3: 检测是否是 EPUB 导航文件
                    is_nav_file = "toc.ncx" in relative_path.lower() or relative_path.lower().endswith("nav.xhtml")

                    # Step 4: 按块级 HTML 标签分割（先分块）
                    raw_chunk_states = chunk_html(content_after_pre, token_limit=self.limit)
                    raw_chunks = add_context_to_chunks(raw_chunk_states)

                    # Step 5: Create Chunk objects directly from ChunkState
                    chunks = []
                    for cs in raw_chunks:
                        chunk = Chunk(
                            name=cs.xpath,
                            original=cs.original,
                            translated=None,
                            status=TranslationStatus.PENDING,
                            tokens=cs.tokens,
                        )
                        chunks.append(chunk)

                    # Step 5.1: 验证 chunk 拆分后的 HTML 结构
                    chunk_names = [c.name for c in chunks]
                    validator = HtmlValidator()
                    valid, errors = validator.validate_merged([c.original for c in chunks], chunk_names)
                    if not valid:
                        logger.warning(f"文件 {relative_path} 拆分后 HTML 结构异常: {errors}")

                    # Step 6: 对 toc.ncx 合并太小的相邻 chunks
                    if is_nav_file:
                        chunks = self._merge_small_nav_chunks(chunks, min_tokens=500)

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

    def _merge_small_nav_chunks(self, chunks: List[Chunk], min_tokens: int = 500) -> List[Chunk]:
        """
        合并 toc.ncx 中太小的相邻 chunks

        流程：
        1. 贪婪合并太小的相邻 chunks
        2. 创建新的 Chunk 对象

        Args:
            chunks: 原始 chunks 列表
            min_tokens: 最小 token 阈值

        Returns:
            合并后的 chunks 列表
        """
        if len(chunks) <= 1:
            return chunks

        # 贪婪合并太小的相邻 chunks
        # 注意：用 HTML tokens 而非文本 tokens 判断，因为翻译按 HTML 整体进行
        merged_htmls = []
        i = 0
        while i < len(chunks):
            current_html = chunks[i].original
            current_tokens = count_tokens(current_html)

            j = i + 1
            while j < len(chunks):
                next_html = chunks[j].original
                next_tokens = count_tokens(next_html)
                combined_tokens = current_tokens + next_tokens

                if current_tokens >= min_tokens and combined_tokens > min_tokens * 1.5:
                    break

                current_html += next_html
                current_tokens = combined_tokens
                j += 1

            merged_htmls.append(current_html)
            i = j

        # 创建新的 Chunk 对象
        final_chunks = []
        for html in merged_htmls:
            chunk = Chunk(
                name=str(uuid.uuid4())[:8],
                original=html,
                translated=None,
                status=TranslationStatus.PENDING,
                tokens=count_tokens(html),
            )
            final_chunks.append(chunk)

        return final_chunks

    def _extract_text_from_html(self, html: str) -> str:
        """
        从 HTML 中提取可翻译文本（去掉标签后的纯文本）

        Args:
            html: HTML 文本

        Returns:
            纯文本
        """
        # 去掉所有 HTML 标签
        text = re.sub(r"<[^>]+>", "", html)
        return text


if __name__ == "__main__":
    parser = Parser("/Users/amaozhao/workspace/epubox/depth-leadership-unlocking-unconscious.epub")
    book = parser.parse()
