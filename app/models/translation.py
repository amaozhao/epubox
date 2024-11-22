"""Translation models."""
from enum import Enum
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, JSON, Enum as SQLEnum
from sqlalchemy.orm import relationship

from app.db.base import Base, TimestampMixin

class TranslationStatus(str, Enum):
    """Translation task status."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class TranslationService(str, Enum):
    """Translation service provider."""
    GOOGLE = "google"
    DEEPL = "deepl"

class TranslationTask(Base, TimestampMixin):
    """Model for tracking translation tasks."""
    __tablename__ = "translation_tasks"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    file_id = Column(Integer, ForeignKey("file_metadata.id", ondelete="CASCADE"), nullable=False)
    source_language = Column(String(10), nullable=False)
    target_language = Column(String(10), nullable=False)
    service = Column(SQLEnum(TranslationService), nullable=False)
    status = Column(SQLEnum(TranslationStatus), default=TranslationStatus.PENDING, nullable=False)
    progress = Column(Float, default=0.0, nullable=False)
    error_message = Column(String, nullable=True)
    cost = Column(Float, default=0.0, nullable=False)
    character_count = Column(Integer, default=0, nullable=False)
    result_file_id = Column(Integer, ForeignKey("file_metadata.id", ondelete="SET NULL"), nullable=True)
    task_metadata = Column(JSON, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="translation_tasks")
    source_file = relationship(
        "FileMetadata",
        foreign_keys=[file_id],
        back_populates="translations_as_source"
    )
    result_file = relationship(
        "FileMetadata",
        foreign_keys=[result_file_id],
        back_populates="translations_as_result"
    )

    def update_status(self, status: TranslationStatus, error: str = None):
        """Update task status and timestamps."""
        self.status = status
        if status == TranslationStatus.PROCESSING and not self.started_at:
            self.started_at = datetime.utcnow()
        elif status in (TranslationStatus.COMPLETED, TranslationStatus.FAILED, TranslationStatus.CANCELLED):
            self.completed_at = datetime.utcnow()
        if error:
            self.error_message = error

    def update_progress(self, progress: float):
        """Update translation progress."""
        self.progress = min(max(progress, 0.0), 100.0)

    def update_cost(self, cost: float, character_count: int):
        """Update translation cost and character count."""
        self.cost = cost
        self.character_count = character_count

class TranslationSegment(Base, TimestampMixin):
    """Model for tracking individual segments of a translation task."""
    __tablename__ = "translation_segments"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("translation_tasks.id", ondelete="CASCADE"), nullable=False)
    segment_index = Column(Integer, nullable=False)
    original_text = Column(String, nullable=False)
    translated_text = Column(String, nullable=True)
    status = Column(SQLEnum(TranslationStatus), default=TranslationStatus.PENDING, nullable=False)
    error_message = Column(String, nullable=True)
    segment_metadata = Column(JSON, nullable=True)

    # Relationships
    task = relationship("TranslationTask", backref="segments")

    def update_translation(self, translated_text: str, status: TranslationStatus = TranslationStatus.COMPLETED):
        """Update segment translation and status."""
        self.translated_text = translated_text
        self.status = status
