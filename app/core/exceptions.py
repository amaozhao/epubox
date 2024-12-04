from typing import Any, Optional

from fastapi import status


class EpuBoxException(Exception):
    """基础异常类"""

    def __init__(
        self,
        message: str,
        error_code: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: Optional[Any] = None,
    ) -> None:
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        self.details = details


class DatabaseException(EpuBoxException):
    """数据库相关异常"""

    def __init__(
        self,
        message: str = "Database error",
        error_code: str = "DATABASE_ERROR",
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        details: Optional[Any] = None,
    ) -> None:
        super().__init__(message, error_code, status_code, details)


class AuthenticationException(EpuBoxException):
    """认证相关异常"""

    def __init__(
        self,
        message: str = "Authentication error",
        error_code: str = "AUTHENTICATION_ERROR",
        status_code: int = status.HTTP_401_UNAUTHORIZED,
        details: Optional[Any] = None,
    ) -> None:
        super().__init__(message, error_code, status_code, details)


class PermissionDeniedException(EpuBoxException):
    """权限相关异常"""

    def __init__(
        self,
        message: str = "Permission denied",
        error_code: str = "PERMISSION_DENIED",
        status_code: int = status.HTTP_403_FORBIDDEN,
        details: Optional[Any] = None,
    ) -> None:
        super().__init__(message, error_code, status_code, details)


class ValidationException(EpuBoxException):
    """数据验证异常"""

    def __init__(
        self,
        message: str = "Validation error",
        error_code: str = "VALIDATION_ERROR",
        status_code: int = status.HTTP_422_UNPROCESSABLE_ENTITY,
        details: Optional[Any] = None,
    ) -> None:
        super().__init__(message, error_code, status_code, details)


class NotFoundException(EpuBoxException):
    """资源不存在异常"""

    def __init__(
        self,
        message: str = "Resource not found",
        error_code: str = "NOT_FOUND",
        status_code: int = status.HTTP_404_NOT_FOUND,
        details: Optional[Any] = None,
    ) -> None:
        super().__init__(message, error_code, status_code, details)
