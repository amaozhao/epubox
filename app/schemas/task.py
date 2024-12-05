"""
Task schemas module.
Contains Pydantic models for task-related operations.
"""

from datetime import datetime

from pydantic import BaseModel


class TaskCreate(BaseModel):
    """Schema for creating a new task."""

    source_lang: str
    target_lang: str
    file_path: str


class TaskStatus(BaseModel):
    """Schema for task status."""

    id: str
    status: str
    progress: float
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
