"""
EPUB processor module.
Handles the core EPUB processing functionality.
"""

import shutil
from pathlib import Path
from typing import Dict

import ebooklib
from ebooklib import epub

from app.core.logging import get_logger

from .utils import ensure_directory

logger = get_logger(__name__)


class EpubProcessor:
    """Main class for processing EPUB files."""

    def __init__(self, file_path: str, work_dir: str):
        """
        Initialize the EPUB processor.

        Args:
            file_path: Path to the original EPUB file
            work_dir: Directory for processing files
        """
        self.file_path = Path(file_path)
        self.work_dir = Path(work_dir)
        self.book = None
        self.original_name = self.file_path.name
        self.work_file = self.work_dir / self.original_name
        self.html_contents: Dict[str, str] = {}

    def _load_epub(self) -> bool:
        """
        加载EPUB文件.

        Returns:
            bool: 加载是否成功
        """
        try:
            self.book = epub.read_epub(str(self.work_file))
            return True
        except Exception as e:
            logger.error(f"Failed to load EPUB file: {e}")
            return False

    def _extract_html(self) -> Dict[str, str]:
        """
        从EPUB文件中提取HTML内容.

        Returns:
            Dict[str, str]: HTML内容字典
        """
        contents = {}
        for item in self.book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                content = item.get_content().decode("utf-8")
                contents[item.get_name()] = content
        return contents

    def _update_html(self, translated_contents: Dict[str, str]) -> bool:
        """
        更新EPUB文件中的HTML内容.

        Args:
            translated_contents: 翻译后的内容

        Returns:
            bool: 更新是否成功
        """
        try:
            # 只更新内容，让 ebooklib 处理 TOC
            for item in self.book.get_items():
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    name = item.get_name()
                    if name in translated_contents:
                        logger.info(f"Updating content for {name}")
                        item.set_content(translated_contents[name].encode("utf-8"))
                    else:
                        logger.debug(
                            f"Skipping file {name} - not in translated contents"
                        )
            return True
        except Exception as e:
            logger.error(f"Failed to update HTML content: {e}", exc_info=True)
            return False

    def _save_epub(self) -> bool:
        """
        保存EPUB文件.

        Returns:
            bool: 保存是否成功
        """
        try:
            logger.info(f"Saving EPUB to {self.work_file}")
            logger.info("Book structure:")
            logger.info(
                f"- Items: {[item.get_name() for item in self.book.get_items()]}"
            )
            logger.info(f"- Spine: {self.book.spine}")
            logger.info(f"- TOC: {self.book.toc}")
            epub.write_epub(str(self.work_file), self.book)
            return True
        except Exception as e:
            logger.error(f"Failed to save EPUB file: {e}", exc_info=True)
            return False

    async def prepare(self) -> bool:
        """
        准备EPUB文件处理环境.
        1. 复制原始文件到工作目录
        2. 加载EPUB文件

        Returns:
            bool: 准备是否成功
        """
        try:
            # 检查输入文件是否存在
            if not self.file_path.exists():
                logger.error(f"Input file not found: {self.file_path}")
                return False

            # 创建工作目录
            ensure_directory(self.work_dir)

            # 复制原始文件到工作目录
            shutil.copy2(str(self.file_path), str(self.work_file))

            # 加载EPUB文件
            return self._load_epub()

        except Exception as e:
            logger.error(f"Failed to prepare EPUB file: {e}")
            return False

    async def extract_content(self) -> Dict[str, str]:
        """
        提取EPUB文件中的HTML内容.

        Returns:
            Dict[str, str]: HTML内容字典，键为文件名，值为内容
        """
        if not self.book:
            raise RuntimeError("EPUB file not loaded. Call prepare() first.")

        try:
            self.html_contents = self._extract_html()
            return self.html_contents

        except Exception as e:
            logger.error(f"Failed to extract content: {e}")
            raise

    async def update_content(self, translated_contents: Dict[str, str]) -> bool:
        """
        使用翻译后的内容更新EPUB文件.

        Args:
            translated_contents: 翻译后的HTML内容字典

        Returns:
            bool: 更新是否成功
        """
        if not self.book:
            raise RuntimeError("EPUB file not loaded. Call prepare() first.")

        try:
            # 更新HTML内容
            if not self._update_html(translated_contents):
                return False

            # 保存更新后的EPUB文件
            return self._save_epub()

        except Exception as e:
            logger.error(f"Failed to update content: {e}")
            return False

    async def cleanup(self) -> None:
        """清理临时文件和资源."""
        try:
            # 清理内存资源
            self.book = None
            self.html_contents.clear()

            # 清理临时文件
            if self.work_file.exists():
                self.work_file.unlink()

            # 清理工作目录（如果为空）
            try:
                self.work_dir.rmdir()
            except OSError:
                # 目录不为空，忽略错误
                pass

        except Exception as e:
            logger.error(f"Failed to cleanup: {e}")

    def get_work_file_path(self) -> str:
        """获取工作文件路径."""
        return str(self.work_file)

        try:
            self.html_contents = self._extract_html()
            return self.html_contents

        except Exception as e:
            logger.error(f"Failed to extract content: {e}")
            raise

    async def update_content(self, translated_contents: Dict[str, str]) -> bool:
        """
        使用翻译后的内容更新EPUB文件.

        Args:
            translated_contents: 翻译后的HTML内容字典

        Returns:
            bool: 更新是否成功
        """
        if not self.book:
            raise RuntimeError("EPUB file not loaded. Call prepare() first.")

        try:
            # 更新HTML内容
            if not self._update_html(translated_contents):
                return False

            # 保存更新后的EPUB文件
            return self._save_epub()

        except Exception as e:
            logger.error(f"Failed to update content: {e}")
            return False

    async def cleanup(self) -> None:
        """清理临时文件和资源."""
        try:
            # 清理内存资源
            self.book = None
            self.html_contents.clear()

            # 清理临时文件
            if self.work_file.exists():
                self.work_file.unlink()

            # 清理工作目录（如果为空）
            try:
                self.work_dir.rmdir()
            except OSError:
                # 目录不为空，忽略错误
                pass

        except Exception as e:
            logger.error(f"Failed to cleanup: {e}")

    def get_work_file_path(self) -> str:
        """获取工作文件路径."""
        return str(self.work_file)
