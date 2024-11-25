from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.core.exceptions import (
    InvalidPasswordException,
    UserValidationError,
    UserAlreadyExists,
)
from app.core.logging import validation_logger as logger
from app.models.user import User


class PasswordValidator:
    """Password validation rules."""

    MIN_LENGTH = 8

    @classmethod
    def validate_password(cls, password: str, email: Optional[str] = None) -> None:
        """
        Validate password against security rules.

        Args:
            password: Password to validate
            email: Optional email to check password against

        Raises:
            InvalidPasswordException: If password fails validation
        """
        # Check minimum length
        if len(password) < cls.MIN_LENGTH:
            logger.warning(
                "password_validation_failed", reason="too_short", length=len(password)
            )
            raise InvalidPasswordException(
                f"Password should be at least {cls.MIN_LENGTH} characters"
            )

        # Check if email is contained in password
        if email and email.lower() in password.lower():
            logger.warning("password_validation_failed", reason="contains_email")
            raise InvalidPasswordException("Password should not contain email")

        logger.info("password_validation_passed")


class UserValidator:
    """User data validation rules."""

    @staticmethod
    async def validate_unique_email(session: AsyncSession, email: str) -> None:
        """
        Validate that email is unique.

        Args:
            session: Database session
            email: Email to validate

        Raises:
            UserAlreadyExists: If email already exists
        """
        try:
            query = select(User).where(User.email == email)
            result = await session.execute(query)
            if result.scalar_one_or_none() is not None:
                logger.warning(
                    "email_validation_failed", reason="already_exists", email=email
                )
                raise UserAlreadyExists()

            logger.debug("email_validation_passed", email=email)

        except Exception as e:
            logger.error("email_validation_error", error=str(e))
            raise

    @staticmethod
    async def validate_unique_username(session: AsyncSession, username: str) -> None:
        """
        Validate that username is unique.

        Args:
            session: Database session
            username: Username to validate

        Raises:
            UserAlreadyExists: If username already exists
        """
        try:
            query = select(User).where(User.username == username)
            result = await session.execute(query)
            if result.scalar_one_or_none() is not None:
                logger.warning(
                    "username_validation_failed",
                    reason="already_exists",
                    username=username,
                )
                raise UserAlreadyExists()

            logger.debug("username_validation_passed", username=username)

        except Exception as e:
            logger.error("username_validation_error", error=str(e))
            raise
