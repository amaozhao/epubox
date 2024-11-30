import enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class StorageStatus(enum.Enum):
    """存储状态枚举"""

    UPLOADING = "uploading"  # 上传中
    UPLOADED = "uploaded"  # 上传完成
    PROCESSING = "processing"  # 处理中
    TRANSLATING = "translating"  # 翻译中
    COMPLETED = "completed"  # 完成
    FAILED = "failed"  # 失败
    DELETED = "deleted"  # 已删除


class User(Base):
    """用户模型"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # UUID
    username: Mapped[str] = mapped_column(String(50), unique=True)  # 用户名
    email: Mapped[str] = mapped_column(String(255), unique=True)  # 电子邮件
    password_hash: Mapped[str] = mapped_column(String(255))  # 密码哈希

    # 用户状态
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    # 关联存储
    storages: Mapped[List["Storage"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    def __repr__(self) -> str:
        return f"<User {self.username} ({self.id})>"


class Storage(Base):
    """存储模型，用于管理上传的文件及其翻译状态"""

    __tablename__ = "storages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # UUID
    original_filename: Mapped[str] = mapped_column(String(255))  # 原始文件名
    file_size: Mapped[int] = mapped_column(Integer)  # 文件大小（字节）
    mime_type: Mapped[str] = mapped_column(String(100))  # 文件类型
    status: Mapped[StorageStatus] = mapped_column(
        SQLEnum(StorageStatus), default=StorageStatus.UPLOADING
    )

    # 文件路径信息
    upload_path: Mapped[str] = mapped_column(String(255))  # 上传文件路径
    translation_path: Mapped[Optional[str]] = mapped_column(
        String(255)
    )  # 翻译文件路径（如果有）

    # 用户关联
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    user: Mapped["User"] = relationship(back_populates="storages")

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)  # 完成时间

    # 错误信息
    error_message: Mapped[Optional[str]] = mapped_column(
        String(1000)
    )  # 如果失败，存储错误信息

    def __repr__(self) -> str:
        return f"<Storage {self.original_filename} ({self.id})>"
