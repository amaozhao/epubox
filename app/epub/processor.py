"""
EPUB processor module.
Handles the core EPUB processing functionality.
"""

import shutil
import zipfile
from pathlib import Path
from typing import Dict, Tuple

import ebooklib
from bs4 import BeautifulSoup
from ebooklib import epub

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import LimitType, TranslationProvider

# from app.html.processor import HTMLProcessor
from app.html.tree import TreeProcessor
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
        self.tree_processor = TreeProcessor(
            translator=self.translator,
            source_lang=self.source_lang,
            target_lang=self.target_lang,
        )

    def init_translator(self, translator):
        """
        初始化翻译提供者

        Args:
            translator: 翻译提供者名称 ('mistral', 'google', 'groq')

        Returns:
            TranslationProvider: 翻译提供者实例
        """
        # 获取 API key 配置
        api_key_configs = {
            "mistral": settings.MISTRAL_API_KEY,
            "google": settings.GOOGLE_API_KEY,
            "groq": settings.GROQ_API_KEY,
        }

        # 默认模型配置
        default_models = {
            "mistral": "mistral-large-latest",
            "groq": "mixtral-8x7b-32768",
            "google": None,  # Google Translate 不需要指定模型
        }

        # 检查提供者是否支持
        if translator not in api_key_configs:
            raise ValueError(f"Unsupported translator: {translator}")

        # 从配置文件加载提供者配置
        factory = ProviderFactory()
        provider_config = factory.get_provider_config(translator)

        # 创建提供者模型
        provider_model = TranslationProvider(
            name=translator,
            provider_type=translator,
            config={"api_key": api_key_configs[translator]},
            enabled=True,
            is_default=False,
            rate_limit=provider_config.get("default_rate_limit", 3),
            retry_count=provider_config.get("retry", {}).get("max_attempts", 3),
            retry_delay=provider_config.get("retry", {}).get("initial_delay", 5),
            limit_type=LimitType[provider_config.get("limit_type", "CHARS").upper()],
            limit_value=provider_config.get("default_max_units", 4000),
            model=default_models[translator],
        )

        return factory.create_provider(provider_model)

    def load_epub(self) -> None:
        """
        加载EPUB文件.
        """
        self.book = epub.read_epub(str(self.work_file))

    def get_epub_files(self):
        input_archive = zipfile.ZipFile(str(self.work_file), "r")
        file_list = input_archive.infolist()
        epub_dict = {}
        for x in range(0, len(file_list)):
            filename = file_list[x].filename
            if filename.endswith(".xhtml") or filename.endswith(".html"):
                # 标准化路径格式
                name = filename.replace("\\", "/").lstrip("/")
                item = input_archive.open(file_list[x])
                content = item.read()
                epub_dict[name] = content
                logger.info(f"已加载文件：{name}")
        return epub_dict

    def extract(self) -> Dict[str, Tuple[str, int]]:
        """提取 EPUB 文件中的内容。

        Returns:
            Dict[str, Tuple[str, int]]: 内容字典，键为文件名，值为内容和类型的元组
        """
        contents = {}
        _contents = self.get_epub_files()
        for item in self.book.get_items():
            # 标准化路径格式
            name = item.get_name().replace("\\", "/").lstrip("/")
            logger.info(f"处理项目：{name}, 类型：{item.get_type()}")

            # 检查是否是文档或导航
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                logger.info(f"找到文档项目：{name}")
                
                # 尝试不同的路径格式
                possible_paths = [
                    name,  # 原始路径
                    f"OEBPS/{name}",  # 添加 OEBPS 前缀
                    name.replace("Text/", "OEBPS/Text/"),  # 替换 Text 为 OEBPS/Text
                ]
                
                content_found = False
                for path in possible_paths:
                    if path in _contents:
                        contents[name] = (
                            _contents[path].decode("utf-8"),
                            ebooklib.ITEM_DOCUMENT,
                        )
                        logger.info(f"已提取文档内容：{name} (从路径：{path})")
                        content_found = True
                        break
                
                if not content_found:
                    logger.warning(f"找不到文件内容：{name}")
                    logger.debug(f"尝试的路径：{possible_paths}")
                    logger.debug(f"可用的文件：{list(_contents.keys())}")
                    
            elif item.get_type() == ebooklib.ITEM_NAVIGATION:
                logger.info(f"找到导航项目：{name}")
                contents[name] = (
                    item.get_content().decode("utf-8"),
                    ebooklib.ITEM_NAVIGATION,
                )
                logger.info(f"已提取导航内容：{name}")

        return contents

    def save_epub(self) -> None:
        """保存 EPUB 文件。"""
        # 在保存之前确保所有项目的路径格式正确
        for item in self.book.get_items():
            if hasattr(item, 'file_name'):
                # 标准化文件路径
                original_path = item.file_name
                normalized_path = original_path.replace("\\", "/").lstrip("/")
                if original_path != normalized_path:
                    logger.info(f"标准化文件路径：{original_path} -> {normalized_path}")
                    item.file_name = normalized_path

        # 确保工作目录存在
        self.work_file.parent.mkdir(parents=True, exist_ok=True)

        # 保存文件
        try:
            options = {"html_write_using_document_content": True}
            epub.write_epub(str(self.work_file), self.book, options)
            logger.info(f"成功保存EPUB文件：{self.work_file}")
        except Exception as e:
            logger.error(f"保存EPUB文件失败：{e}")
            raise

    def genarate_content(self, original_content, translated_content, parser, item_type):
        """
        生成保存内容
        """
        return str(translated_content)

    def get_parser(self, item_type):
        if item_type == ebooklib.ITEM_DOCUMENT:
            return "html.parser"
        if item_type == ebooklib.ITEM_NAVIGATION:
            return "lxml-xml"
        return "html.parser"

    async def update_content(
        self,
        item_name,
        original_content,
        translated_content,
        item_type=ebooklib.ITEM_DOCUMENT,
    ):
        """更新 EPUB 文件中的内容。

        Args:
            item_name: 项目名称
            original_content: 原始内容
            translated_content: 翻译后的内容
            item_type: 项目类型，默认为文档类型
        """
        for item in self.book.get_items():
            if item.get_type() == item_type:
                # 标准化路径格式，移除开头的斜杠
                name = item.get_name().lstrip('/')
                item_name = item_name.lstrip('/')
                
                if name == item_name:
                    parser = self.get_parser(item_type)
                    # 使用 BeautifulSoup 解析和处理内容
                    original_soup = BeautifulSoup(original_content, parser)
                    translated_soup = BeautifulSoup(translated_content, parser)

                    if item_type == ebooklib.ITEM_DOCUMENT:
                        if name in ("cover.xhtml", "cover.html"):
                            item.set_content(str(original_soup).encode())
                            break
                        else:
                            if translated_soup.find("body"):
                                original_soup.find("body").replace_with(
                                    translated_soup.find("body")
                                )
                            else:
                                original_soup.find("body").replace_with(translated_soup)
                    elif item_type == ebooklib.ITEM_NAVIGATION:
                        if translated_soup.find("ncx"):
                            original_soup.find("ncx").replace_with(
                                translated_soup.find("ncx")
                            )
                        elif translated_soup.find("package"):
                            original_soup.find("package").replace_with(
                                translated_soup.find("package")
                            )
                        else:
                            original_soup.find("ncx").replace_with(translated_soup)

                    # 将处理后的内容设置回项目
                    item.set_content(str(original_soup).encode())
                    logger.info(f"已更新内容：{name}")
                    break

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

    async def process(self) -> None:
        """处理 EPUB 文件。"""
        logger.info("开始处理 EPUB 文件...")
        await self.prepare()

        try:
            # 提取需要处理的内容
            contents = self.extract()
            logger.info(f"提取到 {len(contents)} 个文件需要处理")

            # 处理每个文档项
            for name, (content, item_type) in contents.items():
                logger.info(f"处理文件：{name}")
                
                # 创建处理器
                processor = TreeProcessor(
                    self.translator,
                    self.source_lang,
                    self.target_lang,
                )

                try:
                    # 处理内容
                    await processor.process(content)
                    translated = processor.restore_html(processor.root)

                    # 更新内容
                    await self.update_content(
                        name,
                        content,
                        translated,
                        item_type,
                    )

                    # 检查更新后的内容
                    updated = False
                    for item in self.book.get_items():
                        if item.get_name().replace("\\", "/").lstrip("/") == name:
                            updated_content = item.get_content().decode("utf-8")
                            updated = True
                            logger.info(f"已确认更新：{name}")
                            break
                    
                    if not updated:
                        logger.warning(f"未能确认更新：{name}")

                except Exception as e:
                    logger.error(f"处理文件 {name} 时出错：{e}")
                    raise

            # 保存文件
            self.save_epub()
            logger.info("EPUB 文件处理完成")

        except Exception as e:
            logger.error(f"处理 EPUB 文件时出错：{e}")
            raise
