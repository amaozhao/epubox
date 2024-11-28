"""Translation project model."""

from datetime import datetime
from enum import Enum
from typing import List

from sqlalchemy import Column, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.db.base_class import Base


class TranslationStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


class TranslationProject(Base):
    """Translation project model for managing EPUB translations."""

    __tablename__ = "translation_projects"

    id = Column(Integer, primary_key=True, index=True)
    epub_file_id = Column(Integer, ForeignKey("epub_files.id"), nullable=False)
    source_language = Column(String(10), nullable=False)  # ISO language code
    target_language = Column(String(10), nullable=False)  # ISO language code
    status = Column(SQLEnum(TranslationStatus), default=TranslationStatus.PENDING)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    total_words = Column(Integer, default=0)
    translated_words = Column(Integer, default=0)
    total_cost = Column(Float, default=0.0)  # Running total of translation costs
    provider = Column(
        String(50), nullable=False
    )  # Translation provider (e.g., "OpenAI", "Google")

    # Relationships
    epub_file = relationship("EPUBFile", back_populates="translation_projects")
    translation_chunks = relationship("TranslationChunk", back_populates="project")
    progress_records = relationship("TranslationProgress", back_populates="project")
    memory_records = relationship("TranslationMemory", back_populates="project")
    cost_records = relationship("CostRecord", back_populates="project")

    def __repr__(self):
        return f"<TranslationProject(id={self.id}, status={self.status})>"
