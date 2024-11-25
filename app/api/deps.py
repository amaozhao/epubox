"""Dependencies for FastAPI endpoints."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_active_user
from app.db.session import get_async_session
from app.models.user import User


async def get_db(session: AsyncSession = Depends(get_async_session)) -> AsyncSession:
    """Get database session.

    Args:
        session: Database session from dependency injection

    Returns:
        AsyncSession: Database session
    """
    return session


async def get_current_user(user: User = Depends(current_active_user)) -> User:
    """Get current authenticated user.

    Args:
        user: User from dependency injection

    Returns:
        User: Current authenticated user
    """
    return user


# Type annotations for dependencies
CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_async_session)]
