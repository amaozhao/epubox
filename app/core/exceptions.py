"""Custom exception classes."""

from typing import Any, Dict, Optional
from fastapi import HTTPException, status

class EpuBoxException(HTTPException):
    """Base exception for EPUBox application."""
    
    def __init__(
        self,
        status_code: int,
        detail: str,
        headers: Optional[Dict[str, Any]] = None
    ):
        super().__init__(status_code=status_code, detail=detail, headers=headers)

class AuthenticationException(EpuBoxException):
    """Exception raised for authentication failures."""
    
    def __init__(self, detail: str = "Invalid credentials"):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"}
        )

class DatabaseException(EpuBoxException):
    """Exception raised for database errors."""
    
    def __init__(self, detail: str = "Database error occurred"):
        super().__init__(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=detail
        )