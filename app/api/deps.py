from typing import Annotated, AsyncGenerator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_active_user
from app.db.base import get_async_session
from app.models.user import User


# Reusable dependencies
async def get_current_user(
    user: User = Depends(current_active_user),
) -> User:
    """
    Get current authenticated user.
    """
    return user


# Type annotations for dependencies
CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_async_session)]
