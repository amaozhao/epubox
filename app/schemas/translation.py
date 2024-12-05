"""
Translation schemas module.
Contains Pydantic models for translation-related operations.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ProviderCreate(BaseModel):
    """Schema for creating a new translation provider."""

    name: str
    provider_type: str
    config: dict
    is_default: bool = False
    rate_limit: int = 3
    retry_count: int = 3
    retry_delay: int = 60


class ProviderUpdate(BaseModel):
    """Schema for updating a translation provider."""

    name: Optional[str] = None
    config: Optional[dict] = None
    is_default: Optional[bool] = None
    enabled: Optional[bool] = None
    rate_limit: Optional[int] = None
    retry_count: Optional[int] = None
    retry_delay: Optional[int] = None


class TranslationResponse(BaseModel):
    """Schema for translation response."""

    task_id: str
    chapter_index: int
    status: str
    translated_text: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True
