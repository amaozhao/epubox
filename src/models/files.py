import enum
from datetime import datetime

from sqlalchemy import Column, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from ..infrastructure.database import Base


class FileStatus(enum.Enum):
    """文件状态枚举"""

    UPLOADING = "uploading"  # 上传中
    UPLOADED = "uploaded"  # 上传完成
    PROCESSING = "processing"  # 处理中
    TRANSLATING = "translating"  # 翻译中
    COMPLETED = "completed"  # 完成
    FAILED = "failed"  # 失败
    DELETED = "deleted"  # 已删除


class File(Base):
    """文件模型"""

    __tablename__ = "files"

    id = Column(String(36), primary_key=True)  # UUID
    original_filename = Column(String(255), nullable=False)  # 原始文件名
    file_size = Column(Integer, nullable=False)  # 文件大小（字节）
    mime_type = Column(String(100), nullable=False)  # 文件类型
    status = Column(SQLEnum(FileStatus), nullable=False, default=FileStatus.UPLOADING)

    # 文件路径信息
    upload_path = Column(String(255), nullable=False)  # 上传文件路径
    translation_path = Column(String(255))  # 翻译文件路径（如果有）

    # 用户关联
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    user = relationship("User", back_populates="files")

    # 时间戳
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    completed_at = Column(DateTime)  # 完成时间

    # 错误信息
    error_message = Column(String(1000))  # 如果失败，存储错误信息

    def __repr__(self):
        return f"<File {self.original_filename} ({self.id})>"
