from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Optional, Callable
import functools

from app.services.rate_limiter import RateLimiter

rate_limiter = RateLimiter()

def rate_limit(
    action: str = "upload",
    check_file_size: bool = True
):
    """
    Rate limiting decorator for API endpoints.
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Get user from dependencies
            user = kwargs.get("current_user")
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User not authenticated"
                )

            # Get file size if needed
            file_size = None
            if check_file_size:
                file = kwargs.get("file")
                if file and hasattr(file, "size"):
                    file_size = file.size

            # Check rate limit
            is_allowed, error_message = await rate_limiter.check_rate_limit(
                user_id=user.id,
                user_role=user.role,
                action=action,
                file_size=file_size
            )

            if not is_allowed:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=error_message
                )

            return await func(*args, **kwargs)
        return wrapper
    return decorator

class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware to add rate limit headers to responses."""
    
    def __init__(self, app):
        super().__init__(app)
    
    async def dispatch(self, request: Request, call_next):
        """Add rate limit headers to response."""
        response = await call_next(request)
        
        # Get user from request state (set by authentication middleware)
        user = getattr(request.state, "user", None)
        if user:
            # Get rate limit info
            remaining, reset_time = await rate_limiter.get_rate_limit_info(
                user_id=user.id,
                user_role=user.role
            )
            
            # Add rate limit headers
            response.headers["X-RateLimit-Remaining"] = str(remaining)
            response.headers["X-RateLimit-Reset"] = str(reset_time)
        
        return response
