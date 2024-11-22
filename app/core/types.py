"""Common types and enumerations used across the application."""

from enum import Enum

class UserRole(str, Enum):
    """User role enumeration for authorization."""
    FREE = "free"
    PREMIUM = "premium"
    ADMIN = "admin"

    def has_permission(self, required_role: "UserRole") -> bool:
        """Check if the role has permission for the required role level.
        
        Args:
            required_role: The role level required for access
            
        Returns:
            bool: True if this role has sufficient permissions
        """
        role_hierarchy = {
            UserRole.FREE: 0,
            UserRole.PREMIUM: 1,
            UserRole.ADMIN: 2
        }
        return role_hierarchy[self] >= role_hierarchy[required_role]

class TranslationStatus(str, Enum):
    """Status enumeration for translation tasks."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    def is_terminal(self) -> bool:
        """Check if this is a terminal status.
        
        Returns:
            bool: True if the status is terminal (completed, failed, or cancelled)
        """
        return self in (
            TranslationStatus.COMPLETED,
            TranslationStatus.FAILED,
            TranslationStatus.CANCELLED
        )

class TranslationService(str, Enum):
    """Available translation service providers."""
    GOOGLE = "google"
    DEEPL = "deepl"
