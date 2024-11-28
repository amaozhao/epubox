"""Translation progress model."""

from datetime import datetime
from enum import Enum

from sqlalchemy import JSON, Column, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db.base_class import Base


class ProgressType(str, Enum):
    INITIALIZATION = "initialization"
    PROCESSING = "processing"
    TRANSLATION = "translation"
    VALIDATION = "validation"
    ASSEMBLY = "assembly"
    ERROR = "error"


class TranslationProgress(Base):
    """Model for tracking translation progress and events."""

    __tablename__ = "translation_progress"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("translation_projects.id"), nullable=False)
    progress_type = Column(SQLEnum(ProgressType), nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    message = Column(Text, nullable=False)
    details = Column(JSON, nullable=True)  # Additional structured information
    chunks_total = Column(Integer, default=0)
    chunks_completed = Column(Integer, default=0)
    words_total = Column(Integer, default=0)
    words_completed = Column(Integer, default=0)
    estimated_remaining_time = Column(Integer, nullable=True)  # in seconds

    # Relationships
    project = relationship("TranslationProject", back_populates="progress_records")

    class Config:
        orm_mode = True

    def __repr__(self):
        return f"<TranslationProgress(id={self.id}, type={self.progress_type})>"
