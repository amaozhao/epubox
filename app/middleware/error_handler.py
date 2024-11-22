"""Error handling middleware."""

import logging
from typing import Callable
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.exceptions import EPUBoxException, DatabaseException

logger = logging.getLogger(__name__)

class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    """Middleware for handling exceptions globally."""
    
    async def dispatch(
        self, request: Request, call_next: Callable
    ) -> Response:
        """Process the request and handle any exceptions.
        
        Args:
            request: The incoming request
            call_next: The next middleware or route handler
            
        Returns:
            Response: The processed response
        """
        try:
            return await call_next(request)
            
        except EPUBoxException as exc:
            # Handle our custom exceptions
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers
            )
            
        except SQLAlchemyError as exc:
            # Handle database errors
            logger.error(f"Database error: {str(exc)}")
            return JSONResponse(
                status_code=500,
                content={"detail": "An error occurred while processing your request"}
            )
            
        except Exception as exc:
            # Handle unexpected errors
            logger.exception("Unexpected error occurred")
            return JSONResponse(
                status_code=500,
                content={"detail": "An unexpected error occurred"}
            )
