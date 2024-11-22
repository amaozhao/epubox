"""User model for authentication and authorization."""

from sqlalchemy import Column, Integer, String, Boolean, Enum as SQLEnum
from sqlalchemy.orm import relationship

from app.db.base import Base, TimestampMixin
from app.core.types import UserRole

class User(Base, TimestampMixin):
    """User model representing application users.
    
    Attributes:
        id: Unique identifier for the user
        email: User's email address (unique)
        hashed_password: Securely hashed password
        full_name: User's full name
        role: User's role for authorization
        is_active: Whether the user account is active
        is_superuser: Whether the user has superuser privileges
        files: Related file metadata records
        translation_tasks: Related translation tasks
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String)
    role = Column(SQLEnum(UserRole), default=UserRole.FREE, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    is_superuser = Column(Boolean, default=False, nullable=False)

    # Relationships
    files = relationship(
        "FileMetadata",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin"
    )
    translation_tasks = relationship(
        "TranslationTask",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin"
    )

    def has_permission(self, required_role: UserRole) -> bool:
        """Check if user has required role permission.
        
        Args:
            required_role: The role level required for access
            
        Returns:
            bool: True if user has sufficient permissions
        """
        if self.is_superuser:
            return True
        return self.role.has_permission(required_role)

    class Config:
        """Pydantic configuration."""
        orm_mode = True
