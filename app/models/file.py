"""File metadata model."""
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import relationship
from enum import Enum

from app.db.base import Base, TimestampMixin

class FileStatus(str, Enum):
    UPLOADED = "uploaded"
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    TRANSLATED = "translated"

class FileMetadata(Base, TimestampMixin):
    __tablename__ = "file_metadata"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, index=True)
    original_filename = Column(String)
    file_path = Column(String, unique=True)
    file_size = Column(BigInteger)  # in bytes
    mime_type = Column(String)
    file_hash = Column(String)  # SHA-256 hash
    status = Column(String)  # pending, processing, completed, failed
    user_id = Column(Integer, ForeignKey("users.id"))
    deleted_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="files")
    translations_as_source = relationship(
        "TranslationTask",
        foreign_keys="TranslationTask.file_id",
        back_populates="source_file"
    )
    translations_as_result = relationship(
        "TranslationTask",
        foreign_keys="TranslationTask.result_file_id",
        back_populates="result_file"
    )

    class Config:
        orm_mode = True
