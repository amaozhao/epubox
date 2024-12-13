"""Translation progress tracking models."""

from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from sqlalchemy import JSON, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class TranslationStatus(str, Enum):
    """Translation status enum."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class TranslationProgress(Base):
    """Model for tracking translation progress of books."""

    __tablename__ = "translation_progress"

    book_id: Mapped[str] = mapped_column(String(255), index=True)
    total_chapters: Mapped[Dict] = mapped_column(JSON)  # 存储所有章节的信息
    completed_chapters: Mapped[Dict] = mapped_column(
        JSON, default={}
    )  # 存储已完成章节的信息
    _status: Mapped[TranslationStatus] = mapped_column(
        "status", SQLEnum(TranslationStatus), default=TranslationStatus.PENDING
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def __init__(
        self,
        book_id: str,
        total_chapters: Dict,
        status: TranslationStatus = TranslationStatus.PENDING,
        completed_chapters: Dict = {},
        started_at=None,
        completed_at=None,
    ):
        """Initialize translation progress."""
        self.book_id = book_id
        self.validate_chapters(total_chapters)
        self.total_chapters = total_chapters
        self.status = status  # 使用 property setter 进行验证
        self.completed_chapters = completed_chapters or {}
        self.started_at = started_at or datetime.now()
        self.completed_at = completed_at

    @staticmethod
    def validate_chapters(chapters: Dict) -> None:
        """验证章节数据的结构."""
        required_fields = {"id", "type", "name"}
        for chapter_id, chapter in chapters.items():
            missing_fields = required_fields - set(chapter.keys())
            if missing_fields:
                raise ValueError(
                    f"Chapter {chapter_id} is missing required fields: {missing_fields}"
                )

    @property
    def status(self) -> TranslationStatus:
        """Get the current status."""
        return self._status

    @status.setter
    def status(self, value: str | TranslationStatus):
        """Set and validate the status."""
        if isinstance(value, str):
            try:
                value = TranslationStatus(value)
            except ValueError:
                raise ValueError(f"Invalid status value: {value}")
        elif not isinstance(value, TranslationStatus):
            raise ValueError(
                f"Status must be string or TranslationStatus enum, got {type(value)}"
            )
        self._status = value

    def update_chapter_status(
        self, chapter_id: str, status: str, completed_at: Optional[datetime] = None
    ):
        """Update the status of a specific chapter."""
        if status == "completed" and chapter_id in self.total_chapters:
            # Create a new dictionary to ensure SQLAlchemy detects the change
            new_completed = dict(self.completed_chapters)
            new_completed[chapter_id] = {
                **self.total_chapters[chapter_id],
                "status": status,
                "completed_at": completed_at.isoformat() if completed_at else None,
            }
            self.completed_chapters = new_completed

        # Update overall progress status
        if len(self.completed_chapters) == len(self.total_chapters):
            self.status = TranslationStatus.COMPLETED
            self.completed_at = datetime.now()
        elif len(self.completed_chapters) > 0:
            self.status = TranslationStatus.PROCESSING

    def get_progress_percentage(self) -> float:
        """Calculate the progress percentage.

        Returns:
            float: Progress percentage (0-100)
        """
        if not self.total_chapters:
            return 0.0
        return (len(self.completed_chapters) / len(self.total_chapters)) * 100
