"""EPUB processor service."""

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List

import ebooklib
from ebooklib import epub

from .html import HTMLProcessor


class EPUBProcessorError(Exception):
    """Base exception for EPUB processor errors."""

    pass


class EPUBProcessor:
    """EPUB file processor.

    职责：
    1. 管理EPUB文件的读写
    2. 协调HTML内容的翻译流程
    3. 维护临时文件和工作目录
    """

    def __init__(self, html_processor: HTMLProcessor, project_root: str):
        """初始化EPUB处理器

        Args:
            html_processor: HTML内容处理器
            project_root: 项目根目录路径
        """
        self.html_processor = html_processor
        self.temp_dir = os.path.join(project_root, "temp")
        os.makedirs(self.temp_dir, exist_ok=True)

    async def translate_epub(
        self, epub_path: str, source_lang: str, target_lang: str, output_path: str
    ) -> str:
        """翻译EPUB文件的主入口

        Args:
            epub_path: 源EPUB文件路径
            source_lang: 源语言
            target_lang: 目标语言
            output_path: 输出文件路径

        Returns:
            输出文件路径

        Raises:
            EPUBProcessorError: 处理过程中的错误
        """
        # 为每个翻译任务创建独立的工作目录
        task_dir = os.path.join(self.temp_dir, Path(epub_path).stem)
        os.makedirs(task_dir, exist_ok=True)

        work_path = os.path.join(task_dir, "work.epub")
        try:
            # 1. 创建工作副本
            shutil.copy2(epub_path, work_path)

            # 2. 提取内容
            contents = await self.extract_content(work_path)

            # 3. 处理每个内容文件
            translated_contents = []
            for content in contents:
                translated_content = await self.html_processor.process_content(
                    content, source_lang=source_lang, target_lang=target_lang
                )
                translated_contents.append(translated_content)

            # 4. 保存翻译后的内容到工作副本
            await self.save_translated_content(work_path, translated_contents)

            # 5. 移动工作副本到目标位置
            shutil.move(work_path, output_path)
            return output_path

        except Exception as e:
            logging.error(f"Failed to translate EPUB: {str(e)}")
            raise EPUBProcessorError(f"Translation failed: {str(e)}")

    async def extract_content(self, epub_path: str) -> List[Dict[str, Any]]:
        """提取EPUB文件中的可翻译内容

        Args:
            epub_path: EPUB文件路径

        Returns:
            提取的内容列表，每项包含：
            {
                "id": str,           # 文件ID
                "file_name": str,    # 文件名
                "media_type": str,   # 媒体类型
                "content": str       # HTML内容
            }
        """
        try:
            book = epub.read_epub(epub_path)
            contents = []

            for item in book.get_items():
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    content = {
                        "id": item.id,
                        "file_name": item.file_name,
                        "media_type": item.media_type,
                        "content": item.get_content().decode("utf-8"),
                    }
                    contents.append(content)

            return contents

        except Exception as e:
            raise EPUBProcessorError(f"Failed to extract EPUB content: {str(e)}")

    async def save_translated_content(
        self, epub_path: str, translated_contents: List[Dict[str, Any]]
    ) -> None:
        """保存翻译后的内容到工作副本

        Args:
            epub_path: 工作副本EPUB文件路径
            translated_contents: 翻译后的内容列表
        """
        try:
            # 1. 读取工作副本
            book = epub.read_epub(epub_path)

            # 2. 只替换文档内容，保持其他结构不变
            for item in book.get_items():
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    # 查找对应的翻译内容
                    translated = next(
                        (c for c in translated_contents if c["id"] == item.id), None
                    )
                    if translated:
                        item.set_content(translated["content"].encode("utf-8"))

            # 3. 保存更新后的书，保持原有选项
            epub.write_epub(epub_path, book, {"epub3_pages": False})

        except Exception as e:
            raise EPUBProcessorError(f"Failed to save translated EPUB: {str(e)}")
