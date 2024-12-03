import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..infrastructure.database import Base


class StorageStatus(enum.Enum):
    """存储状态枚举"""

    UPLOADING = "uploading"  # 上传中
    UPLOADED = "uploaded"  # 上传完成
    PROCESSING = "processing"  # 处理中
    TRANSLATING = "translating"  # 翻译中
    COMPLETED = "completed"  # 完成
    FAILED = "failed"  # 失败
    DELETED = "deleted"  # 已删除


class Storage(Base):
    """存储模型，用于管理上传的文件及其翻译状态"""

    __tablename__ = "storages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # UUID
    original_filename: Mapped[str] = mapped_column(
        String(255), nullable=False
    )  # 原始文件名
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)  # 文件大小（字节）
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)  # 文件类型
    status: Mapped[StorageStatus] = mapped_column(
        SQLEnum(StorageStatus), nullable=False, default=StorageStatus.UPLOADING
    )

    # 文件路径信息
    upload_path: Mapped[str] = mapped_column(
        String(255), nullable=False
    )  # 上传文件路径
    translation_path: Mapped[Optional[str]] = mapped_column(
        String(255)
    )  # 翻译文件路径（如果有）

    # 用户关联（在测试环境中可以为空）
    user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    user = relationship("User", back_populates="storages")

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
        onupdate=lambda: datetime.now(tz=timezone.utc),
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)  # 完成时间

    # 错误信息
    error_message: Mapped[Optional[str]] = mapped_column(
        String(1000)
    )  # 如果失败，存储错误信息

    def __repr__(self) -> str:
        return f"<Storage {self.original_filename} ({self.id})>"
