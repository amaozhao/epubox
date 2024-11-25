from fastapi import HTTPException, status
from fastapi_users.exceptions import UserAlreadyExists

from app.core.logging import exceptions_logger as logger


class UserValidationError(HTTPException):
    """Base exception for user validation errors."""

    def __init__(self, detail: str):
        logger.error("user_validation_exception", detail=detail)
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


class DuplicateUserError(UserAlreadyExists):
    """Exception raised when a user with the same unique field already exists."""

    pass


class InvalidPasswordException(HTTPException):
    """Exception raised when password validation fails."""

    def __init__(self, detail: str):
        logger.error("invalid_password_exception", detail=detail)
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
