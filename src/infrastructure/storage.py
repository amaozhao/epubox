import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Optional
from zipfile import ZipFile

import aiofiles
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.storage import Storage, StorageStatus
from ..utils.errors import FileError
from .config import settings
from .logging import get_logger

logger = get_logger(__name__)


class StorageService:
    """存储服务组件"""

    def __init__(
        self,
        session: AsyncSession,
        upload_dir: Optional[str] = None,
        translation_dir: Optional[str] = None,
    ):
        """
        初始化存储服务
        :param session: 数据库会话
        :param upload_dir: 上传文件目录
        :param translation_dir: 翻译文件目录
        """
        self.session = session
        self.upload_dir = Path(upload_dir or settings.UPLOAD_DIR)
        self.translation_dir = Path(translation_dir or settings.TRANSLATION_DIR)
        self._ensure_dirs()

        logger.info(
            "storage_service_initialized",
            upload_dir=str(self.upload_dir),
            translation_dir=str(self.translation_dir),
        )

    def _ensure_dirs(self) -> None:
        """确保必要的目录存在"""
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.translation_dir.mkdir(parents=True, exist_ok=True)

    async def _generate_file_id(self) -> str:
        """生成唯一的文件ID"""
        return str(uuid.uuid4())

    async def save_upload(self, file: BinaryIO, filename: str, user_id: str) -> str:
        """
        保存上传的文件
        :param file: 文件对象
        :param filename: 原始文件名
        :param user_id: 用户ID
        :return: 文件ID
        """
        try:
            file_id = await self._generate_file_id()
            file_path = self.upload_dir / file_id

            # 保存文件
            async with aiofiles.open(file_path, "wb") as f:
                # 读取并写入文件内容
                content = file.read()
                await f.write(content)

            # 创建存储记录
            storage = Storage(
                id=file_id,
                original_filename=filename,
                file_size=len(content),
                mime_type="application/epub+zip",  # EPUB文件类型
                status=StorageStatus.UPLOADED,
                upload_path=str(file_path),
                user_id=user_id,
                created_at=datetime.now(timezone.utc),  # 统一使用 UTC 时间
            )

            self.session.add(storage)
            await self.session.commit()

            logger.info(
                "file_uploaded",
                file_id=file_id,
                original_filename=filename,
                size=len(content),
                user_id=user_id,
            )

            return file_id

        except Exception as e:
            await self.session.rollback()
            logger.error(
                "file_upload_failed",
                filename=filename,
                error=str(e),
                exc_info=True,
            )
            raise FileError(f"Failed to upload file: {str(e)}")

    async def create_work_copy(self, file_id: str) -> str:
        """
        创建工作副本
        :param file_id: 原始文件ID
        :return: 工作副本路径
        """
        try:
            # 获取存储记录
            storage = await self.session.get(Storage, file_id)
            if not storage:
                raise FileError(f"Storage record {file_id} not found")

            source_path = Path(storage.upload_path)
            if not source_path.exists():
                raise FileError(f"Original file {file_id} not found")

            work_copy_id = await self._generate_file_id()
            target_path = self.translation_dir / work_copy_id

            # 复制文件
            shutil.copy2(source_path, target_path)

            # 更新存储记录
            storage.translation_path = str(target_path)
            storage.status = StorageStatus.PROCESSING
            await self.session.commit()

            logger.info(
                "work_copy_created",
                original_file_id=file_id,
                work_copy_id=work_copy_id,
                target_path=str(target_path),
                user_id=storage.user_id,
            )

            return str(target_path)

        except Exception as e:
            await self.session.rollback()
            logger.error(
                "work_copy_creation_failed",
                file_id=file_id,
                error=str(e),
                exc_info=True,
            )
            raise FileError(f"Failed to create work copy: {str(e)}")

    async def get_file(self, file_id: str, directory: Optional[str] = None) -> bytes:
        """
        获取文件内容
        :param file_id: 文件ID
        :param directory: 可选的目录指定（upload或translation）
        :return: 文件内容
        """
        try:
            # 获取存储记录
            storage = await self.session.get(Storage, file_id)
            if not storage:
                raise FileError(f"Storage record {file_id} not found")

            # 确定文件路径
            if directory == "upload":
                file_path = Path(storage.upload_path)
            elif directory == "translation":
                if not storage.translation_path:
                    raise FileError(f"Translation file not found for {file_id}")
                file_path = Path(storage.translation_path)
            else:
                # 优先使用翻译文件
                file_path = Path(storage.translation_path or storage.upload_path)

            if not file_path.exists():
                raise FileError(f"File {file_id} not found at {file_path}")

            # 读取文件内容
            async with aiofiles.open(file_path, "rb") as f:
                content = await f.read()

            logger.info(
                "file_retrieved",
                file_id=file_id,
                size=len(content),
                user_id=storage.user_id,
            )

            return content

        except Exception as e:
            logger.error(
                "file_retrieval_failed", file_id=file_id, error=str(e), exc_info=True
            )
            raise FileError(f"Failed to retrieve file: {str(e)}")

    async def delete_file(self, file_id: str, directory: Optional[str] = None) -> None:
        """
        删除文件
        :param file_id: 文件ID
        :param directory: 可选的目录指定（upload或translation）
        """
        try:
            # 获取存储记录
            storage = await self.session.get(Storage, file_id)
            if not storage:
                raise FileError(f"Storage record {file_id} not found")

            deleted = False

            # 删除物理文件
            if directory in (None, "upload") and storage.upload_path:
                upload_path = Path(storage.upload_path)
                if upload_path.exists():
                    upload_path.unlink()
                    deleted = True

            if directory in (None, "translation") and storage.translation_path:
                translation_path = Path(storage.translation_path)
                if translation_path.exists():
                    translation_path.unlink()
                    deleted = True

            # 更新存储记录
            if directory is None:
                # 如果没有指定目录，标记为删除状态
                storage.status = StorageStatus.DELETED
            elif directory == "translation" and storage.translation_path:
                # 如果删除翻译文件，清除翻译路径
                storage.translation_path = None
                storage.status = StorageStatus.UPLOADED

            await self.session.commit()

            if not deleted:
                raise FileError(f"No files found to delete for {file_id}")

            logger.info(
                "file_deleted",
                file_id=file_id,
                directory=directory,
                user_id=storage.user_id,
            )

        except Exception as e:
            await self.session.rollback()
            logger.error(
                "file_deletion_failed", file_id=file_id, error=str(e), exc_info=True
            )
            raise FileError(f"Failed to delete file: {str(e)}")

    async def verify_epub(self, file_id: str, directory: str = "upload") -> bool:
        """
        验证文件是否为有效的EPUB文件
        :param file_id: 文件ID
        :param directory: 文件所在目录
        :return: 是否为有效的EPUB文件
        """
        try:
            # 获取存储记录
            storage = await self.session.get(Storage, file_id)
            if not storage:
                raise FileError(f"Storage record {file_id} not found")

            content = await self.get_file(file_id, directory)

            # 创建临时文件
            temp_path = self.upload_dir / f"temp_{file_id}"
            try:
                # 写入临时文件
                async with aiofiles.open(temp_path, "wb") as f:
                    await f.write(content)

                # 验证EPUB结构
                with ZipFile(temp_path) as zip_file:
                    # 检查mimetype文件
                    if "mimetype" not in zip_file.namelist():
                        return False

                    # 读取mimetype内容
                    mimetype = zip_file.read("mimetype").decode("utf-8").strip()
                    if mimetype != "application/epub+zip":
                        return False

                    # 检查必要的文件
                    required_files = [
                        "META-INF/container.xml",
                    ]
                    for required_file in required_files:
                        if required_file not in zip_file.namelist():
                            return False

                logger.info(
                    "epub_verified",
                    file_id=file_id,
                    valid=True,
                    user_id=storage.user_id,
                )
                return True

            finally:
                # 清理临时文件
                if temp_path.exists():
                    temp_path.unlink()

        except Exception as e:
            logger.error(
                "epub_verification_failed", file_id=file_id, error=str(e), exc_info=True
            )
            return False

    async def cleanup_expired_files(self, max_age_hours: int = 24) -> None:
        """
        清理过期文件
        :param max_age_hours: 最大保留时间（小时）
        """
        try:
            current_time = datetime.now(timezone.utc)  # 统一使用 UTC 时间

            # 刷新会话，确保获取最新数据
            await self.session.flush()

            # 获取过期的存储记录
            stmt = select(Storage).where(
                and_(
                    Storage.status.in_(
                        [
                            StorageStatus.UPLOADED,
                            StorageStatus.FAILED,
                            StorageStatus.COMPLETED,
                        ]
                    ),
                    Storage.created_at < current_time - timedelta(hours=max_age_hours),
                )
            )
            result = await self.session.execute(stmt)
            expired_storages = result.scalars().all()

            deleted_count = 0
            for storage in expired_storages:
                try:
                    # 删除物理文件
                    if storage.upload_path:
                        upload_path = Path(storage.upload_path)
                        if upload_path.exists():
                            upload_path.unlink()
                            logger.info(
                                "deleted_upload_file",
                                file_id=storage.id,
                                path=str(upload_path),
                            )

                    if storage.translation_path:
                        translation_path = Path(storage.translation_path)
                        if translation_path.exists():
                            translation_path.unlink()
                            logger.info(
                                "deleted_translation_file",
                                file_id=storage.id,
                                path=str(translation_path),
                            )

                    # 更新存储状态
                    storage.status = StorageStatus.DELETED
                    deleted_count += 1

                    logger.info(
                        "expired_file_deleted",
                        file_id=storage.id,
                        age_hours=(current_time - storage.created_at).total_seconds()
                        / 3600,
                        user_id=storage.user_id,
                    )
                except Exception as e:
                    logger.error(
                        "failed_to_delete_file",
                        file_id=storage.id,
                        error=str(e),
                        exc_info=True,
                    )
                    continue

            # 提交所有更改
            if deleted_count > 0:
                await self.session.commit()

            logger.info(
                "cleanup_completed",
                max_age_hours=max_age_hours,
                files_deleted=deleted_count,
            )

        except Exception as e:
            await self.session.rollback()
            logger.error("cleanup_failed", error=str(e), exc_info=True)
            raise FileError(f"Failed to cleanup expired files: {str(e)}")
