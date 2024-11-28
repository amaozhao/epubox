"""Translation chunk model."""

from datetime import datetime
from enum import Enum

from sqlalchemy import Column, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db.base_class import Base


class ChunkStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    VERIFIED = "verified"


class TranslationChunk(Base):
    """Model for managing chunks of content for translation."""

    __tablename__ = "translation_chunks"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("translation_projects.id"), nullable=False)
    sequence_number = Column(Integer, nullable=False)  # Order in the document
    content_type = Column(String(50), nullable=False)  # e.g., "text", "heading", "list"
    original_content = Column(Text, nullable=False)
    translated_content = Column(Text, nullable=True)
    context = Column(Text, nullable=True)  # Additional context for translation
    status = Column(SQLEnum(ChunkStatus), default=ChunkStatus.PENDING)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    word_count = Column(Integer, default=0)
    token_count = Column(Integer, default=0)

    # Relationships
    project = relationship("TranslationProject", back_populates="translation_chunks")
    memory_entries = relationship("TranslationMemory", back_populates="chunk")

    def __repr__(self):
        return f"<TranslationChunk(id={self.id}, status={self.status})>"
