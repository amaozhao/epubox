"""User schema models for request/response validation."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, constr

from app.core.types import UserRole

class UserBase(BaseModel):
    """Base schema for user data."""
    email: EmailStr
    full_name: Optional[str] = None
    role: Optional[UserRole] = UserRole.FREE
    is_active: Optional[bool] = True

class UserCreate(UserBase):
    """Schema for creating a new user."""
    password: constr(min_length=8)  # type: ignore
    confirm_password: str

    def validate_passwords_match(self):
        """Validate that password and confirm_password match."""
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match")

class UserUpdate(BaseModel):
    """Schema for updating a user."""
    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    is_active: Optional[bool] = None
    role: Optional[UserRole] = None

class UserPasswordUpdate(BaseModel):
    """Schema for updating user password."""
    current_password: str
    new_password: constr(min_length=8)  # type: ignore
    confirm_password: str

    def validate_passwords_match(self):
        """Validate that new_password and confirm_password match."""
        if self.new_password != self.confirm_password:
            raise ValueError("New passwords do not match")

class UserInDBBase(UserBase):
    """Base schema for user in database."""
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        """Pydantic configuration."""
        from_attributes = True

class User(UserInDBBase):
    """Schema for user response."""
    pass

class UserInDB(UserInDBBase):
    """Schema for user in database with hashed password."""
    hashed_password: str
