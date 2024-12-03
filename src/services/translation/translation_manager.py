"""Translation manager service."""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from pytz import UTC
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.config import Settings
from src.models.storage import Storage, StorageStatus

from .translator import (
    TranslationError,
    TranslationProvider,
    TranslationService,
    create_translator,
)


class TranslationManagerError(Exception):
    """Base exception for translation manager errors."""


class TranslationManager:
    """Manager for translation tasks."""

    def __init__(
        self, settings: Settings, session: AsyncSession, provider: TranslationProvider
    ):
        """Initialize translation manager."""
        self.settings = settings
        self.session = session
        self.provider = provider

        try:
            # 直接使用 create_translator，传入对应的 API key
            api_key = getattr(settings, f"{provider.value.upper()}_API_KEY")
            self.translator = create_translator(provider, api_key=api_key)
            logging.info(f"Initialized TranslationManager with provider: {provider}")
        except Exception as e:
            raise TranslationManagerError(
                f"Failed to initialize TranslationManager: {str(e)}"
            )

    async def _update_storage_status(
        self, storage: Storage, status: StorageStatus, error_message: str = None
    ) -> Storage:
        """Update storage status and flush changes."""
        try:
            storage.status = status
            if error_message:
                storage.error_message = error_message
                logging.error(f"Storage {storage.id}: {error_message}")
            if status == StorageStatus.COMPLETED:
                storage.completed_at = datetime.now(UTC)

            # 确保更改被提交
            await self.session.flush()
            await self.session.refresh(storage)
            return storage
        except Exception as e:
            logging.error(f"Failed to update storage status: {str(e)}")
            # 如果更新失败，回滚会话
            await self.session.rollback()
            raise

    async def process_translation(
        self, storage: Storage, source_lang: str, target_lang: str
    ) -> Storage:
        """Process translation task."""
        try:
            # 验证输入路径
            upload_path = self.settings.STORAGE_PATH / storage.upload_path
            if not upload_path.exists():
                return await self._update_storage_status(
                    storage,
                    StorageStatus.FAILED,
                    f"Upload file not found: {upload_path}",
                )

            # 更新开始状态
            try:
                storage.status = StorageStatus.TRANSLATING
                storage.started_at = datetime.now(UTC)
                await self.session.flush()
                await self.session.refresh(storage)
                logging.info(f"Starting translation for storage {storage.id}")
            except Exception as e:
                return await self._update_storage_status(
                    storage,
                    StorageStatus.FAILED,
                    f"Failed to update initial status: {str(e)}",
                )

            # 提取内容
            try:
                # Import here to avoid circular dependency
                from ..processors.epub import EPUBProcessor, EPUBProcessorError

                self.epub_processor = EPUBProcessor(
                    str(self.settings.TEMP_PATH)
                )  # 保存引用以便 cleanup
                contents = await self.epub_processor.extract_content(str(upload_path))
                if not contents:
                    return await self._update_storage_status(
                        storage, StorageStatus.FAILED, "No content found in EPUB file"
                    )
                logging.info(f"Extracted {len(contents)} chapters from EPUB")
            except EPUBProcessorError as e:
                return await self._update_storage_status(
                    storage,
                    StorageStatus.FAILED,
                    f"Failed to extract EPUB content: {str(e)}",
                )

            # 翻译内容
            try:
                translated_contents = []
                total_chapters = len(contents)

                for i, content in enumerate(contents, 1):
                    logging.info(f"Translating chapter {i}/{total_chapters}")
                    try:
                        translated_text = await self.translator.translate_batch(
                            [content["content"]], source_lang, target_lang
                        )
                        if not translated_text:
                            raise TranslationError("Empty translation result")
                        translated_contents.append(
                            {
                                "id": content["id"],
                                "file_name": content["file_name"],
                                "media_type": content["media_type"],
                                "content": translated_text[0],
                            }
                        )
                    except TranslationError as e:
                        status_msg = f"Translation failed for chapter {i}: {str(e)}"
                        logging.error(status_msg)
                        return await self._update_storage_status(
                            storage, StorageStatus.FAILED, status_msg
                        )
            except Exception as e:
                status_msg = f"Translation process failed: {str(e)}"
                logging.error(status_msg)
                return await self._update_storage_status(
                    storage, StorageStatus.FAILED, status_msg
                )

            if not translated_contents:
                status_msg = "No content was translated successfully"
                logging.error(status_msg)
                return await self._update_storage_status(
                    storage, StorageStatus.FAILED, status_msg
                )

            # 准备输出路径
            try:
                translated_filename = f"translated_{storage.original_filename}"
                output_dir = self.settings.STORAGE_PATH / "translations"
                output_dir.mkdir(parents=True, exist_ok=True)
                output_path = output_dir / translated_filename

                # 保存翻译后的内容
                await self.epub_processor.save_translated_content(
                    str(upload_path), translated_contents, str(output_path)
                )
                storage.translation_path = f"translations/{translated_filename}"
                await self.session.flush()
                await self.session.refresh(storage)
                logging.info(f"Saved translated EPUB to {output_path}")
            except (EPUBProcessorError, OSError) as e:
                return await self._update_storage_status(
                    storage,
                    StorageStatus.FAILED,
                    f"Failed to save translated content: {str(e)}",
                )

            # 更新完成状态
            return await self._update_storage_status(storage, StorageStatus.COMPLETED)

        except Exception as e:
            error_msg = str(e)
            if "rate limit" in error_msg.lower():
                status_msg = "Translation failed: API rate limit exceeded. Please try again later."
            else:
                status_msg = f"Unexpected error: {error_msg}"
            return await self._update_storage_status(
                storage, StorageStatus.FAILED, status_msg
            )

    async def cleanup(self):
        """Clean up resources."""
        if hasattr(self, "epub_processor"):
            await self.epub_processor.cleanup()

    async def __aenter__(self):
        """Enter the async context manager."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit the async context manager."""
        await self.cleanup()
