"""Translation schemas."""

from datetime import datetime
from typing import Dict, Optional

from pydantic import BaseModel


class TranslationRequest(BaseModel):
    """Schema for translation requests."""

    file_id: int
    source_lang: str
    target_lang: str
    provider: Optional[str] = None


class TranslationStatus(BaseModel):
    """Schema for translation task status."""

    task_id: str
    status: str  # queued, processing, completed, failed, cancelled
    progress: float
    file_id: int
    source_lang: str
    target_lang: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    result: Optional[Dict] = None


class TranslationResponse(BaseModel):
    """Schema for translation response."""

    task_id: str
    message: str
