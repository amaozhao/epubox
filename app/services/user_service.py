from datetime import datetime, timedelta
import jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from fastapi import HTTPException, status

from app.models.user import User, UserRole
from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

class UserService:
    def __init__(self):
        self.pwd_context = pwd_context

    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        """Verify a password against its hash."""
        return self.pwd_context.verify(plain_password, hashed_password)

    def get_password_hash(self, password: str) -> str:
        """Generate password hash."""
        return self.pwd_context.hash(password)

    def create_access_token(self, data: dict, expires_delta: Optional[timedelta] = None) -> str:
        """Create JWT access token."""
        to_encode = data.copy()
        if expires_delta:
            expire = datetime.utcnow() + expires_delta
        else:
            expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        
        to_encode.update({"exp": expire})
        encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
        return encoded_jwt

    async def authenticate_user(
        self,
        db: AsyncSession,
        email: str,
        password: str
    ) -> Optional[User]:
        """Authenticate a user by email and password."""
        user = await self.get_user_by_email(db, email)
        if not user:
            return None
        if not self.verify_password(password, user.hashed_password):
            return None
        return user

    async def get_user_by_email(
        self,
        db: AsyncSession,
        email: str
    ) -> Optional[User]:
        """Get user by email."""
        stmt = select(User).where(User.email == email)
        result = await db.scalar(stmt)
        return result

    async def get_user_by_id(
        self,
        db: AsyncSession,
        user_id: int
    ) -> Optional[User]:
        """Get user by ID."""
        stmt = select(User).where(User.id == user_id)
        result = await db.scalar(stmt)
        return result

    async def create_user(
        self,
        db: AsyncSession,
        email: str,
        password: str,
        role: UserRole = UserRole.FREE
    ) -> User:
        """Create a new user."""
        # Check if user already exists
        existing_user = await self.get_user_by_email(db, email)
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

        # Create new user
        user = User(
            email=email,
            hashed_password=self.get_password_hash(password),
            role=role
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user

    async def update_user(
        self,
        db: AsyncSession,
        user_id: int,
        email: Optional[str] = None,
        password: Optional[str] = None,
        role: Optional[UserRole] = None
    ) -> Optional[User]:
        """Update user information."""
        user = await self.get_user_by_id(db, user_id)
        if not user:
            return None

        if email:
            # Check if email is already taken by another user
            existing_user = await self.get_user_by_email(db, email)
            if existing_user and existing_user.id != user_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Email already registered"
                )
            user.email = email

        if password:
            user.hashed_password = self.get_password_hash(password)

        if role:
            user.role = role

        user.updated_at = datetime.utcnow()
        await db.commit()
        await db.refresh(user)
        return user

    async def delete_user(
        self,
        db: AsyncSession,
        user_id: int
    ) -> bool:
        """Delete a user."""
        user = await self.get_user_by_id(db, user_id)
        if not user:
            return False

        await db.delete(user)
        await db.commit()
        return True

    async def change_password(
        self,
        db: AsyncSession,
        user_id: int,
        current_password: str,
        new_password: str
    ) -> bool:
        """Change user password."""
        user = await self.get_user_by_id(db, user_id)
        if not user:
            return False

        # Verify current password
        if not self.verify_password(current_password, user.hashed_password):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Incorrect password"
            )

        # Update password
        user.hashed_password = self.get_password_hash(new_password)
        user.updated_at = datetime.utcnow()
        await db.commit()
        return True
