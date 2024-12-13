"""
EPUB processor module.
Handles the core EPUB processing functionality.
"""

import shutil
from pathlib import Path
from typing import Dict

import ebooklib
from ebooklib import epub

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import LimitType, TranslationProvider
from app.html.processor import HTMLProcessor
from app.translation.factory import ProviderFactory

from .utils import ensure_directory

logger = get_logger(__name__)


class EpubProcessor:
    """Main class for processing EPUB files."""

    def __init__(
        self,
        file_path: str,
        work_dir: str,
        translator: str,
        source_lang="en",
        target_lang="zh",
    ):
        """
        Initialize the EPUB processor.

        Args:
            file_path: Path to the original EPUB file
            work_dir: Directory for processing files
            translator: Name of the translator to use
            source_lang: Source language code
            target_lang: Target language code
        """
        self.file_path = Path(file_path)
        self.work_dir = Path(work_dir)
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.translator = self.init_translator(translator)
        # self.book = None
        self.original_name = self.file_path.name
        self.work_file = self.work_dir / self.original_name
        self.html_contents: Dict[str, str] = {}
        self.ncxs = None
        self.html_processor = HTMLProcessor(
            translator=self.translator,
            source_lang=self.source_lang,
            target_lang=self.target_lang,
        )

    def init_translator(self, translator):
        provider_model = TranslationProvider(
            name=translator,
            provider_type=translator,
            config={"api_key": settings.MISTRAL_API_KEY},
            enabled=True,
            is_default=True,
            rate_limit=2,  # 降低速率限制
            retry_count=5,
            retry_delay=60,  # 增加重试延迟到60秒
            limit_type=LimitType.TOKENS,  # Mistral 需要使用基于token的限制
            limit_value=3000,  # 每次请求的token限制
            model="mistral-large-latest",  # 添加model字段
        )

        # 初始化翻译提供者
        factory = ProviderFactory()
        translator = factory.create_provider(provider_model)
        return translator

    def load_epub(self) -> None:
        """
        加载EPUB文件.
        """
        self.book = epub.read_epub(str(self.work_file))

    def extract_html(self) -> Dict[str, str]:
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

    def extract_ncx(self) -> Dict[str, str]:
        """
        从EPUB文件中提取ncx内容.

        Returns:
            Dict[str, str]: ncx内容字典
        """
        contents = {}
        for item in self.book.get_items():
            if item.get_type() == ebooklib.ITEM_NAVIGATION:
                content = item.get_content().decode("utf-8")
                contents[item.get_name()] = content
        return contents

    def save_epub(self) -> None:
        """
        保存EPUB文件.
        """
        epub.write_epub(str(self.work_file), self.book)

    async def update_content(
        self, item_name, content, item_type=ebooklib.ITEM_DOCUMENT
    ):
        for item in self.book.get_items():
            if item.get_type() == item_type:
                name = item.get_name()
                if name == item_name:
                    item.set_content(content.encode("utf-8"))
        self.save_epub()

    async def prepare(self) -> None:
        """
        准备EPUB文件处理环境.
        1. 复制原始文件到工作目录
        2. 加载EPUB文件

        Returns:
            bool: 准备是否成功
        """
        # 检查输入文件是否存在
        if not self.file_path.exists():
            logger.error(f"Input file not found: {self.file_path}")
            return

        # 创建工作目录
        ensure_directory(self.work_dir)

        # 复制原始文件到工作目录
        shutil.copy2(str(self.file_path), str(self.work_file))

        # 加载EPUB文件
        self.load_epub()

    async def process(self):
        """处理 EPUB 文件内容."""
        await self.prepare()

        # 提取内容
        self.ncxs = self.extract_ncx()
        self.html_contents = self.extract_html()

        # 串行处理 HTML 内容
        for name, content in self.html_contents.items():
            logger.info(f"Processing HTML name: {name}", name=name)
            logger.info(f"content: {content}")
            translated_content = await self.html_processor.process(content)
            await self.update_content(name, translated_content)
            # 每个文件处理完后保存一次，避免数据丢失
            self.save_epub()

        # 串行处理 NCX 内容
        for name, content in self.ncxs.items():
            translated_content = await self.html_processor.process(
                content, parser="lxml"
            )
            await self.update_content(
                name, translated_content, item_type=ebooklib.ITEM_NAVIGATION
            )
            # 每个文件处理完后保存一次，避免数据丢失
            self.save_epub()
