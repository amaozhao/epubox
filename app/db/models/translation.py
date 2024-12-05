"""
Translation models module.
Contains database models for translation records and providers.
"""

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from ..base import Base


class TranslationProvider(Base):
    """Translation provider model."""

    __tablename__ = "translation_providers"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    provider_type = Column(String, nullable=False)
    is_default = Column(Boolean, default=False)
    enabled = Column(Boolean, default=True)
    config = Column(Text, nullable=False)
    rate_limit = Column(Integer, default=3)
    retry_count = Column(Integer, default=3)
    retry_delay = Column(Integer, default=60)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class TranslationRecord(Base):
    """Translation record model."""

    __tablename__ = "translation_records"

    id = Column(Integer, primary_key=True)
    task_id = Column(String, ForeignKey("tasks.id"), nullable=False)
    chapter_index = Column(Integer, nullable=False)
    provider_id = Column(
        Integer, ForeignKey("translation_providers.id"), nullable=False
    )
    source_text = Column(Text, nullable=False)
    translated_text = Column(Text)
    source_lang = Column(String, nullable=False)
    target_lang = Column(String, nullable=False)
    word_count = Column(Integer, nullable=False)
    status = Column(String, nullable=False)
    error_message = Column(Text)
    created_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime)
