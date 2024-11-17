# services/storages/parser.py

import ebooklib
from ebooklib import epub


class FileParserService:
    @staticmethod
    def parse_epub(file_path: str):
        """解析EPUB文件，提取内容"""
        book = epub.read_epub(file_path)
        # 这里可以添加具体的EPUB解析逻辑，例如提取章节、文本等
        return book
