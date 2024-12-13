"""
Database models for translation service."""

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Date, Enum, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base


class LimitType(enum.Enum):
    """限制类型"""

    CHARS = "chars"  # 按字符数限制
    TOKENS = "tokens"  # 按token数限制


class TranslationProvider(Base):
    """Translation provider configuration."""

    __tablename__ = "translation_providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    provider_type: Mapped[str] = mapped_column(String, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, nullable=False)
    rate_limit: Mapped[int] = mapped_column(default=3)
    retry_count: Mapped[int] = mapped_column(default=3)
    retry_delay: Mapped[int] = mapped_column(default=5)
    limit_type: Mapped[LimitType] = mapped_column(Enum(LimitType), nullable=False)
    limit_value: Mapped[int] = mapped_column(nullable=False, default=4000)
    model: Mapped[str] = mapped_column(String, nullable=True)

    stats = relationship("ProviderStats", back_populates="provider")


class ProviderStats(Base):
    """Provider statistics."""

    __tablename__ = "provider_stats"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(
        ForeignKey("translation_providers.id"), nullable=False
    )
    date: Mapped[datetime] = mapped_column(Date, nullable=False)
    total_requests: Mapped[int] = mapped_column(default=0)
    success_count: Mapped[int] = mapped_column(default=0)
    error_count: Mapped[int] = mapped_column(default=0)
    rate_limit_hits: Mapped[int] = mapped_column(default=0)
    avg_response_time: Mapped[float] = mapped_column(Float, default=0)
    total_words: Mapped[int] = mapped_column(default=0)

    provider = relationship("TranslationProvider", back_populates="stats")


class TranslationRecord(Base):
    """Translation record model."""

    __tablename__ = "translation_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    chapter_index: Mapped[int] = mapped_column(nullable=False)
    provider_id: Mapped[int] = mapped_column(
        ForeignKey("translation_providers.id"), nullable=False
    )
    source_text: Mapped[str] = mapped_column(String, nullable=False)
    translated_text: Mapped[Optional[str]] = mapped_column(String)
    source_lang: Mapped[str] = mapped_column(String, nullable=False)
    target_lang: Mapped[str] = mapped_column(String, nullable=False)
    word_count: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
