"""Translation memory model."""

from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db.base_class import Base


class TranslationMemory(Base):
    """Model for storing and managing translation memory entries."""

    __tablename__ = "translation_memory"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("translation_projects.id"), nullable=False)
    chunk_id = Column(Integer, ForeignKey("translation_chunks.id"), nullable=True)
    source_text = Column(Text, nullable=False)
    translated_text = Column(Text, nullable=False)
    source_language = Column(String(10), nullable=False)  # ISO language code
    target_language = Column(String(10), nullable=False)  # ISO language code
    context_key = Column(String(255), nullable=True)  # For context-aware matching
    similarity_key = Column(String(255), nullable=True)  # For fuzzy matching
    quality_score = Column(Float, default=1.0)  # Score between 0 and 1
    usage_count = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_used_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    project = relationship("TranslationProject", back_populates="memory_records")
    chunk = relationship("TranslationChunk", back_populates="memory_entries")

    # Indexes for efficient querying
    __table_args__ = (
        Index("idx_memory_source_target", source_language, target_language),
        Index("idx_memory_context", context_key),
        Index("idx_memory_similarity", similarity_key),
    )

    def __repr__(self):
        return f"<TranslationMemory(id={self.id}, source_lang={self.source_language}, target_lang={self.target_language})>"
