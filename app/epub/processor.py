"""
EPUB processor module.
Handles the core EPUB processing functionality.
"""

import shutil
import zipfile
from pathlib import Path
from typing import Dict, Tuple

from bs4 import BeautifulSoup

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import LimitType, TranslationProvider
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
        self.original_name = self.file_path.name
        self.work_file = self.work_dir / self.file_path.name
        self.tree_processor = TreeProcessor(
            translator=self.translator,
            source_lang=self.source_lang,
            target_lang=self.target_lang,
        )
        self.epub_files: Dict[str, bytes] = {}
        self.file_mapping: Dict[str, str] = {}  # 存储原始路径到标准化路径的映射
        self.translated_contents: Dict[str, str] = {}  # 存储已翻译的文件内容

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

    def normalize_path(self, path: str) -> str:
        """标准化文件路径。

        Args:
            path: 原始文件路径

        Returns:
            标准化后的路径
        """
        # 移除开头的斜杠和 OEBPS 前缀
        normalized = path.lstrip("/")
        if normalized.startswith("OEBPS/"):
            normalized = normalized[6:]
        return normalized

    def load_epub(self) -> None:
        """加载 EPUB 文件到内存中"""
        try:
            with zipfile.ZipFile(str(self.work_file), "r") as epub_zip:
                # 获取所有文件列表
                for info in epub_zip.infolist():
                    original_path = info.filename
                    normalized_path = self.normalize_path(original_path)
                    content = epub_zip.read(info.filename)

                    # 存储文件内容和路径映射
                    self.epub_files[normalized_path] = content
                    self.file_mapping[normalized_path] = original_path
        except Exception as e:
            raise

    def get_epub_files(self) -> Dict[str, bytes]:
        """获取所有EPUB文件内容"""
        return self.epub_files

    def extract(self) -> Dict[str, Tuple[str, int]]:
        """提取 EPUB 文件中的内容。

        Returns:
            Dict[str, Tuple[str, int]]: 内容字典，键为文件名，值为内容和类型的元组
        """
        contents = {}
        for normalized_path, content in self.epub_files.items():
            try:
                original_path = self.file_mapping[normalized_path]
                # 检查文件类型
                is_html = original_path.endswith((".xhtml", ".html", ".htm"))
                is_nav = original_path.endswith(("nav.xhtml", "toc.ncx"))

                if is_html or is_nav:
                    try:
                        text_content = content.decode("utf-8")
                        content_type = "navigation" if is_nav else "document"
                        # 使用原始路径作为键
                        contents[original_path] = (text_content, content_type)
                    except UnicodeDecodeError:
                        # 尝试其他编码
                        for encoding in ["utf-8-sig", "latin1", "cp1252"]:
                            try:
                                text_content = content.decode(encoding)
                                content_type = "navigation" if is_nav else "document"
                                contents[original_path] = (text_content, content_type)
                                break
                            except UnicodeDecodeError:
                                continue
            except Exception as e:
                continue

        return contents

    def save_epub(self, modified_contents: Dict[str, str]) -> None:
        """保存修改后的 EPUB 文件。

        Args:
            modified_contents: 修改后的内容字典，键为文件名，值为新的内容
        """
        output_path = self.work_file
        temp_path = output_path.parent / (
            output_path.stem + "_temp" + output_path.suffix
        )
        try:
            # 更新已翻译内容字典
            self.translated_contents.update(modified_contents)

            # 在保存前记录要保存的文件和内容

            # 写入到临时文件
            with zipfile.ZipFile(
                str(temp_path), "w", zipfile.ZIP_DEFLATED
            ) as output_epub:
                # 写入所有已翻译的文件
                for name, content in self.translated_contents.items():
                    output_epub.writestr(name, content.encode("utf-8"))

                # 写入未翻译的文件
                for normalized_path, content in self.epub_files.items():
                    original_path = self.file_mapping[normalized_path]
                    if original_path not in self.translated_contents:
                        output_epub.writestr(original_path, content)

            # 替换原文件
            if output_path.exists():
                output_path.unlink()
            temp_path.rename(output_path)

        except Exception as e:
            if temp_path.exists():
                temp_path.unlink()
            raise

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
                return False

            # 创建工作目录
            ensure_directory(self.work_dir)

            # 复制原始文件到工作目录
            shutil.copy2(str(self.file_path), str(self.work_file))

            # 加载EPUB文件
            self.load_epub()

            return True

        except Exception as e:
            return False

    async def process(self) -> None:
        """处理 EPUB 文件。"""
        try:
            # 准备环境
            if not await self.prepare():
                return

            # 提取需要处理的内容
            contents = self.extract()

            # 处理每个文档项
            for name, (content, content_type) in contents.items():
                try:
                    if content_type == "document" or content_type == "navigation":
                        # 解析HTML内容
                        soup = BeautifulSoup(content, "html.parser")
                        # 使用TreeProcessor处理内容
                        processed_content = await self.tree_processor.process(str(soup))

                        if processed_content:  # 确保有处理结果
                            # 立即保存这个文件
                            current_file = {name: processed_content}
                            self.save_epub(current_file)
                        else:
                            current_file = {name: content}
                            self.save_epub(current_file)
                    else:
                        # 对于非文档类型的文件，保持原样
                        current_file = {name: content}
                        self.save_epub(current_file)

                except Exception as e:
                    # 发生错误时保存原内容
                    current_file = {name: content}
                    self.save_epub(current_file)
                    continue  # 继续处理其他文件

        except Exception as e:
            raise
