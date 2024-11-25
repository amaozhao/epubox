from typing import Optional, Union

from fastapi import Depends, Request
from fastapi_users import BaseUserManager, FastAPIUsers, IntegerIDMixin
from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    JWTStrategy,
)
from fastapi_users.exceptions import (
    InvalidPasswordException as FastAPIUsersInvalidPasswordException,
)
from fastapi_users.exceptions import UserAlreadyExists
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import InvalidPasswordException
from app.core.logging import auth_logger as logger
from app.core.validation import PasswordValidator, UserValidator
from app.db.base import get_async_session
from app.models.user import User

# Configure structured logging


class UserManager(IntegerIDMixin, BaseUserManager[User, int]):
    """User management logic for authentication and registration."""

    reset_password_token_secret = settings.SECRET_KEY
    verification_token_secret = settings.SECRET_KEY

    async def validate_password(
        self,
        password: str,
        user: Union["UserCreate", User],
    ) -> None:
        """Validate password meets security requirements."""
        try:
            PasswordValidator.validate_password(password, user.email)
        except InvalidPasswordException as e:
            logger.warning(
                "Password validation failed",
                error=str(e),
                email=user.email,
                username=getattr(user, "username", None),
            )
            raise FastAPIUsersInvalidPasswordException(e.detail)

    async def create(
        self,
        user_create: "UserCreate",
        safe: bool = False,
        request: Optional[Request] = None,
    ) -> User:
        """Create a new user with validation."""
        try:
            # Validate unique constraints
            await UserValidator.validate_unique_email(
                self.user_db.session, user_create.email
            )
            await UserValidator.validate_unique_username(
                self.user_db.session, user_create.username
            )

            # Create user
            user = await super().create(user_create, safe, request)
            logger.info(
                "User registration successful",
                email=user.email,
                username=user.username,
                is_active=user.is_active,
                is_verified=user.is_verified,
            )
            return user

        except Exception as e:
            logger.error(
                "User registration failed",
                error=str(e),
                error_type=type(e).__name__,
                email=user_create.email,
                username=user_create.username,
            )
            if isinstance(e, UserAlreadyExists):
                raise e
            raise

    async def on_after_register(self, user: User, request: Optional[Request] = None):
        """Log successful registration."""
        logger.info(
            "User registration completed",
            email=user.email,
            username=user.username,
        )

    async def on_after_forgot_password(
        self, user: User, token: str, request: Optional[Request] = None
    ):
        """Log password reset request."""
        logger.info(
            "Password reset requested",
            email=user.email,
            username=user.username,
            token_length=len(token),
        )

    async def on_after_request_verify(
        self, user: User, token: str, request: Optional[Request] = None
    ):
        """Log verification request."""
        logger.info(
            "Verification requested",
            user_id=user.id,
            email=user.email,
            token_length=len(token),
        )


async def get_user_db(session: AsyncSession = Depends(get_async_session)):
    """Get user database session."""
    yield SQLAlchemyUserDatabase(session, User)


async def get_user_manager(user_db=Depends(get_user_db)):
    """Get user manager instance."""
    yield UserManager(user_db)


# Authentication configuration
bearer_transport = BearerTransport(tokenUrl="auth/jwt/login")


def get_jwt_strategy() -> JWTStrategy:
    """Get JWT authentication strategy with configured lifetime."""
    return JWTStrategy(
        secret=settings.SECRET_KEY,
        lifetime_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


auth_backend = AuthenticationBackend(
    name="jwt",
    transport=bearer_transport,
    get_strategy=get_jwt_strategy,
)

fastapi_users = FastAPIUsers[User, int](
    get_user_manager,
    [auth_backend],
)

# User dependencies
current_active_user = fastapi_users.current_user(active=True)
current_superuser = fastapi_users.current_user(active=True, superuser=True)
