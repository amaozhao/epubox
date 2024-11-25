from typing import Optional

from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTable
from sqlalchemy import Boolean, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.logging import models_logger as logger
from app.db.base_class import Base


class User(SQLAlchemyBaseUserTable[int], Base):
    """User model with additional fields."""

    __tablename__ = "user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(
        String(length=320), unique=True, index=True, nullable=False
    )
    username: Mapped[str] = mapped_column(
        String(length=50), unique=True, index=True, nullable=False
    )
    hashed_password: Mapped[str] = mapped_column(String(length=1024), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(length=100), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(length=20), nullable=True)
    epub_files = relationship(
        "EPUBFile", back_populates="user", cascade="all, delete-orphan"
    )

    def __str__(self) -> str:
        """Return string representation of user."""
        return f"User(id={self.id}, email={self.email}, username={self.username})"

    def __repr__(self) -> str:
        """Return string representation of user for debugging."""
        return self.__str__()

    @classmethod
    async def get_by_email(cls, session, email: str) -> Optional["User"]:
        """Get user by email."""
        logger.debug("getting_user_by_email", email=email)
        stmt = select(cls).where(cls.email == email)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        if user:
            logger.info("user_found_by_email", user_id=user.id, email=email)
        else:
            logger.warning("user_not_found_by_email", email=email)
        return user

    @classmethod
    async def get_by_username(cls, session, username: str) -> Optional["User"]:
        """Get user by username."""
        logger.debug("getting_user_by_username", username=username)
        stmt = select(cls).where(cls.username == username)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        if user:
            logger.info("user_found_by_username", user_id=user.id, username=username)
        else:
            logger.warning("user_not_found_by_username", username=username)
        return user
