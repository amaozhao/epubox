"""User repository implementation."""

from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.repositories.base import BaseRepository

class UserRepository(BaseRepository[User]):
    """Repository for user-specific database operations."""
    
    def __init__(self, session: AsyncSession):
        super().__init__(User, session)

    async def get_by_email(self, email: str) -> Optional[User]:
        """Get user by email address.
        
        Args:
            email: User's email address
            
        Returns:
            Optional[User]: User if found, None otherwise
        """
        return await self.get_by_attribute("email", email)

    async def get_active_users(self, *, skip: int = 0, limit: int = 100):
        """Get list of active users.
        
        Args:
            skip: Number of records to skip
            limit: Maximum number of records to return
            
        Returns:
            List[User]: List of active users
        """
        query = (
            select(self.model_cls)
            .where(self.model_cls.is_active == True)
            .offset(skip)
            .limit(limit)
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())
