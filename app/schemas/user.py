from typing import Optional

from fastapi_users import schemas
from pydantic import EmailStr, validator

from app.core.logging import schemas_logger as logger


class UserRead(schemas.BaseUser[int]):
    """User read schema."""

    username: str
    phone: Optional[str] = None
    full_name: Optional[str] = None
    is_active: bool = True
    is_superuser: bool = False
    is_verified: bool = False

    class Config:
        """Pydantic config."""

        orm_mode = True

    @validator("phone")
    def phone_validator(cls, v: Optional[str]) -> Optional[str]:
        """Validate phone number."""
        if v is None:
            return v
        if not v.startswith("+"):
            logger.warning("phone_validation_failed", reason="invalid_format", phone=v)
            raise ValueError("Phone number must start with '+'")
        logger.debug("phone_validation_passed", phone=v)
        return v


class UserCreate(schemas.BaseUserCreate):
    """User creation schema."""

    username: str
    phone: Optional[str] = None
    is_active: bool = True
    is_superuser: bool = False
    is_verified: bool = False

    @validator("username")
    def username_validator(cls, v: str) -> str:
        """Validate username."""
        if len(v) < 3:
            logger.warning(
                "username_validation_failed", reason="too_short", length=len(v)
            )
            raise ValueError("Username must be at least 3 characters long")
        logger.debug("username_validation_passed", username=v)
        return v

    @validator("phone")
    def phone_validator(cls, v: Optional[str]) -> Optional[str]:
        """Validate phone number."""
        if v is None:
            return v
        if not v.startswith("+"):
            logger.warning("phone_validation_failed", reason="invalid_format", phone=v)
            raise ValueError("Phone number must start with '+'")
        logger.debug("phone_validation_passed", phone=v)
        return v


class UserUpdate(schemas.BaseUserUpdate):
    """User update schema."""

    username: Optional[str] = None
    phone: Optional[str] = None
    full_name: Optional[str] = None

    @validator("username")
    def username_validator(cls, v: Optional[str]) -> Optional[str]:
        """Validate username."""
        if v is None:
            return v
        if len(v) < 3:
            logger.warning(
                "username_validation_failed", reason="too_short", length=len(v)
            )
            raise ValueError("Username must be at least 3 characters long")
        logger.debug("username_validation_passed", username=v)
        return v

    @validator("phone")
    def phone_validator(cls, v: Optional[str]) -> Optional[str]:
        """Validate phone number."""
        if v is None:
            return v
        if not v.startswith("+"):
            logger.warning("phone_validation_failed", reason="invalid_format", phone=v)
            raise ValueError("Phone number must start with '+'")
        logger.debug("phone_validation_passed", phone=v)
        return v
