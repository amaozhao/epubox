"""Database models for EPUB processing."""

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column, validates

from ..base import Base


class TranslationStatus(enum.Enum):
    """Translation status."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"


class TranslationProgress(Base):
    """Translation progress tracking for EPUB files."""

    __tablename__ = "epub_translation_progress"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[str] = mapped_column(String, nullable=False)  # epub 文件的唯一标识
    chapters: Mapped[dict] = mapped_column(JSON, nullable=False)  # 所有章节信息列表
    status: Mapped[str] = mapped_column(
        String, nullable=False
    )  # pending/processing/completed

    @validates("status")
    def validate_status(self, key, value):
        """验证状态值是否有效."""
        try:
            return TranslationStatus(value).value
        except ValueError:
            raise ValueError(f"Invalid status: {value}")

    @validates("chapters")
    def validate_chapters(self, key, chapters):
        """验证章节数据结构."""
        required_fields = {"id", "type", "name", "status"}
        valid_statuses = {status.value for status in TranslationStatus}

        for chapter in chapters:
            if not isinstance(chapter, dict):
                raise ValueError("Chapter must be a dictionary")
            if not all(field in chapter for field in required_fields):
                raise ValueError(f"Chapter missing required fields: {required_fields}")
            if chapter["status"] not in valid_statuses:
                raise ValueError(f"Invalid chapter status: {chapter['status']}")
            if (
                chapter["status"] == TranslationStatus.COMPLETED.value
                and "completed_at" not in chapter
            ):
                raise ValueError("Completed chapter must have completed_at timestamp")
        return chapters
