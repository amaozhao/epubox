from datetime import datetime
from typing import Optional, List, Dict
from pydantic import BaseModel, EmailStr, Field
from enum import Enum

from app.models.user import UserRole

# User schemas
class UserBase(BaseModel):
    email: EmailStr = Field(..., description="User's email address")

class UserCreate(UserBase):
    password: str = Field(..., description="User's password", min_length=8)
    role: UserRole = Field(default=UserRole.FREE, description="User's role")

class UserLogin(UserBase):
    password: str = Field(..., description="User's password")

class UserResponse(UserBase):
    id: int = Field(..., description="User's unique identifier")
    role: UserRole = Field(..., description="User's role")
    is_active: bool = Field(..., description="Whether the user account is active")
    created_at: datetime = Field(..., description="When the user account was created")
    updated_at: Optional[datetime] = Field(None, description="When the user account was last updated")
    
    model_config = {"from_attributes": True}

# Authentication schemas
class Token(BaseModel):
    access_token: str = Field(..., description="JWT access token")
    token_type: str = Field(..., description="Token type (bearer)")

class TokenData(BaseModel):
    sub: Optional[str] = Field(None, description="Subject identifier (user ID)")

# Error response schema
class ErrorResponse(BaseModel):
    detail: str = Field(..., description="Error message")

# Rate limit schema
class RateLimitInfo(BaseModel):
    limit: int = Field(..., description="Rate limit (requests per minute)")
    remaining: int = Field(..., description="Remaining requests in current window")
    reset: int = Field(..., description="Seconds until rate limit resets")

# File metadata schemas
class FileMetadataBase(BaseModel):
    filename: str = Field(..., description="Original filename")
    file_size: int = Field(..., description="File size in bytes")
    file_hash: str = Field(..., description="SHA-256 hash of the file")
    mime_type: str = Field(..., description="MIME type of the file")

class FileMetadataCreate(FileMetadataBase):
    user_id: int = Field(..., description="ID of the user who uploaded the file")

class FileMetadataResponse(FileMetadataBase):
    id: int = Field(..., description="File metadata unique identifier")
    user_id: int = Field(..., description="ID of the user who uploaded the file")
    status: str = Field(..., description="Current status of the file")
    created_at: datetime = Field(..., description="When the file was uploaded")
    updated_at: Optional[datetime] = Field(None, description="When the file metadata was last updated")
    deleted_at: Optional[datetime] = Field(None, description="When the file was soft deleted")
    
    model_config = {"from_attributes": True}

# File upload response schema
class FileUploadResponse(BaseModel):
    file_id: str = Field(..., description="Unique identifier for the uploaded file")
    filename: str = Field(..., description="Original filename")
    status: str = Field(..., description="Upload status (success/failure)")
    message: Optional[str] = Field(None, description="Additional status message")

# File list response schema
class FileListResponse(BaseModel):
    files: List[FileMetadataResponse] = Field(..., description="List of file metadata")
    total: int = Field(..., description="Total number of files")
    page: int = Field(..., description="Current page number")
    per_page: int = Field(..., description="Number of items per page")

# Translation status enum
class TranslationStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"

# Translation schemas
class TranslationRequest(BaseModel):
    text: str = Field(..., description="Text to translate")
    source_language: str = Field(..., description="Source language code")
    target_language: str = Field(..., description="Target language code")
    context: Optional[Dict] = Field(None, description="Additional context for translation")

class TranslationResponse(BaseModel):
    translated_text: str = Field(..., description="Translated text")
    source_language: str = Field(..., description="Source language code")
    target_language: str = Field(..., description="Target language code")
    confidence: Optional[float] = Field(None, description="Translation confidence score")
    service_metadata: Optional[Dict] = Field(None, description="Additional metadata from translation service")
