import json
import os
import zipfile
from typing import List, Optional

from engine.item import Chunker, Replacer
from engine.schemas import EpubBook, EpubItem


class Parser:
    def __init__(self, path: str, limit: int = 1500):
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
                        content = f.read()

                    _replacer = Replacer()
                    processed_content = _replacer.replace(content)
                    placeholder_map = _replacer.placeholder.placer_map
                    # processed_content = content
                    # placeholder_map = {}

                    chunker = Chunker(limit=self.limit)
                    chunks = chunker.chunk(processed_content)

                    epub_item = EpubItem(
                        id=relative_path,
                        path=file_path,
                        content=processed_content,
                        placeholder=placeholder_map,
                        chunks=chunks,
                    )
                    items.append(epub_item)

        book = EpubBook(name=self.name, path=self.path, items=items, extract_path=self.output_dir)
        self.save_json(book)

        return book


if __name__ == "__main__":
    parser = Parser("/Users/amaozhao/workspace/epubox/depth-leadership-unlocking-unconscious.epub")
    book = parser.parse()
