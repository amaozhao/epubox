"""User service implementation."""

from typing import Optional
from passlib.context import CryptContext

from app.models.user import User
from app.repositories.user import UserRepository
from app.services.base import BaseService
from app.core.types import UserRole

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

class UserService(BaseService[User, UserRepository]):
    """Service for user-related business logic."""

    async def create_user(
        self,
        *,
        email: str,
        password: str,
        full_name: Optional[str] = None,
        role: UserRole = UserRole.FREE
    ) -> User:
        """Create a new user with hashed password.
        
        Args:
            email: User's email address
            password: Plain text password
            full_name: User's full name
            role: User's role
            
        Returns:
            User: Created user instance
        """
        hashed_password = pwd_context.hash(password)
        return await self.repository.create(
            email=email,
            hashed_password=hashed_password,
            full_name=full_name,
            role=role
        )

    async def authenticate(self, *, email: str, password: str) -> Optional[User]:
        """Authenticate a user.
        
        Args:
            email: User's email address
            password: Plain text password
            
        Returns:
            Optional[User]: Authenticated user if credentials are valid
        """
        user = await self.repository.get_by_email(email)
        if not user:
            return None
        if not pwd_context.verify(password, user.hashed_password):
            return None
        return user

    async def get_active_users(self, *, skip: int = 0, limit: int = 100):
        """Get list of active users."""
        return await self.repository.get_active_users(skip=skip, limit=limit)

    async def update_password(self, *, user_id: int, new_password: str) -> Optional[User]:
        """Update user's password.
        
        Args:
            user_id: ID of user to update
            new_password: New plain text password
            
        Returns:
            Optional[User]: Updated user if successful
        """
        hashed_password = pwd_context.hash(new_password)
        return await self.repository.update(user_id, hashed_password=hashed_password)

    async def deactivate_user(self, user_id: int) -> Optional[User]:
        """Deactivate a user account.
        
        Args:
            user_id: ID of user to deactivate
            
        Returns:
            Optional[User]: Deactivated user if successful
        """
        return await self.repository.update(user_id, is_active=False)
