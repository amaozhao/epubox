"""存储服务模块"""

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
        # 只有当参数为 None 时才使用默认值
        self.upload_dir = Path(
            settings.UPLOAD_DIR if upload_dir is None else upload_dir
        )
        self.translation_dir = Path(
            settings.TRANSLATION_DIR if translation_dir is None else translation_dir
        )
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

    def _generate_file_id(self) -> str:
        """生成唯一的文件ID"""
        return str(uuid.uuid4())

    async def save_upload(self, file: BinaryIO, filename: str, user_id: str) -> str:
        """
        保存上传的文件
        - 生成唯一文件ID
        - 保存到上传目录
        - 返回文件ID

        Args:
            file: 文件对象
            filename: 原始文件名
            user_id: 上传用户ID

        Returns:
            str: 文件ID
        """
        file_id = self._generate_file_id()
        file_path = os.path.join(self.upload_dir, file_id)

        # 读取文件内容
        file_data = file.read()

        # 保存文件
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(file_data)

        # 创建存储记录
        storage = Storage(
            id=file_id,
            original_filename=filename,
            upload_path=str(file_path),
            file_size=len(file_data),
            mime_type="application/epub+zip",
            status=StorageStatus.UPLOADED,
            created_at=datetime.now(timezone.utc),
            user_id=user_id,
        )

        # 保存到数据库
        self.session.add(storage)
        await self.session.commit()

        return file_id

    async def create_work_copy(self, file_id: str) -> str:
        """
        创建工作副本
        :param file_id: 文件ID
        :return: 工作副本路径
        """
        try:
            # 获取存储记录
            storage = await self.session.get(Storage, file_id)
            if not storage:
                raise FileError(f"Storage record not found: {file_id}")

            # 获取原始文件路径
            original_file = Path(storage.upload_path)
            if not original_file.exists():
                raise FileError(f"Original file not found: {file_id}")

            # 创建工作副本路径
            work_copy_path = (
                self.translation_dir / f"{file_id}_work{original_file.suffix}"
            )

            # 复制文件
            shutil.copy2(original_file, work_copy_path)

            return str(work_copy_path)

        except Exception as e:
            raise FileError(f"Failed to create work copy: {str(e)}")

    async def get_file(self, file_id: str, directory: str = "upload") -> bytes:
        """
        获取文件内容
        - 支持获取原始文件或翻译后的文件
        - 文件不存在时抛出异常
        """
        # 获取存储记录
        storage = await self.session.get(Storage, file_id)
        if not storage:
            raise FileError(f"Storage record {file_id} not found")

        # 根据目录选择文件路径
        if directory == "translation" and storage.translation_path:
            file_path = storage.translation_path
        else:
            file_path = storage.upload_path

        if not os.path.exists(file_path):
            raise FileError(f"File {file_id} not found at {file_path}")

        async with aiofiles.open(file_path, "rb") as f:
            return await f.read()

    async def read_epub_html(self, epub_id: str, html_path: str) -> str:
        """
        读取EPUB中的HTML文件内容
        - 使用epub库读取HTML内容
        - 处理编码问题
        """
        epub_path = os.path.join(
            self.translation_dir, f"{epub_id}.epub"
        )  # 添加 .epub 扩展名
        with ZipFile(epub_path, "r") as epub:
            try:
                with epub.open(html_path) as html_file:
                    return html_file.read().decode("utf-8")
            except KeyError:
                raise FileError(f"HTML file {html_path} not found in EPUB")

    async def update_epub_html(self, epub_id: str, html_path: str, content: str):
        """
        更新EPUB中的HTML文件
        - 使用epub库更新文件内容
        - 保持EPUB结构完整
        """
        epub_path = os.path.join(
            self.translation_dir, f"{epub_id}.epub"
        )  # 添加 .epub 扩展名
        # 创建临时文件
        temp_path = f"{epub_path}.tmp"

        with ZipFile(epub_path, "r") as src_epub:
            with ZipFile(temp_path, "w") as dst_epub:
                # 复制所有文件
                for item in src_epub.namelist():
                    if item != html_path:
                        dst_epub.writestr(item, src_epub.read(item))
                    else:
                        # 更新HTML文件
                        dst_epub.writestr(item, content.encode("utf-8"))

        # 替换原文件
        os.replace(temp_path, epub_path)

    async def _copy_file(self, source: str, target: str):
        """复制文件"""
        async with aiofiles.open(source, "rb") as sf:
            async with aiofiles.open(target, "wb") as tf:
                await tf.write(await sf.read())

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
                                "deleted_file",
                                file_id=storage.id,
                                path=str(upload_path),
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

    async def save_file(self, file_path: str) -> str:
        """
        保存文件到存储系统
        :param file_path: 文件路径
        :return: 文件ID
        """
        try:
            # 生成唯一的文件ID
            file_id = str(uuid.uuid4())

            # 构建目标路径
            target_path = self.upload_dir / f"{file_id}.epub"

            # 复制文件到目标路径
            shutil.copy2(file_path, target_path)

            # 创建存储记录
            storage = Storage(
                id=file_id,
                original_path=str(file_path),
                upload_path=str(target_path),
                status=StorageStatus.UPLOADED,
                created_at=datetime.now(timezone.utc),
            )

            # 保存到数据库
            self.session.add(storage)
            await self.session.commit()

            return file_id

        except Exception as e:
            raise FileError(f"Failed to save file: {str(e)}")

    async def get_output_path(self, file_id: str, target_lang: str) -> str:
        """
        获取输出文件路径
        :param file_id: 文件ID
        :param target_lang: 目标语言
        :return: 输出文件路径
        """
        return str(self.translation_dir / f"{file_id}_{target_lang}.epub")

    async def create_task(self, file_id: str, output_path: str) -> str:
        """
        创建翻译任务
        :param file_id: 文件ID
        :param output_path: 输出文件路径
        :return: 任务ID
        """
        try:
            # 更新存储记录状态
            storage = await self.session.get(Storage, file_id)
            if storage:
                storage.status = StorageStatus.PROCESSING
                storage.output_path = output_path
                storage.updated_at = datetime.now(timezone.utc)
                await self.session.commit()

            return file_id

        except Exception as e:
            raise FileError(f"Failed to create task: {str(e)}")
