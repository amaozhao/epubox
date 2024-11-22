import asyncio
import uvicorn
import os
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.api.v1 import translation, users, files
from app.db.session import engine, init_db
from app.core.config import settings
from app.utils.cleanup import start_cleanup_task
from app.middleware.rate_limit import RateLimitMiddleware
from app.docs.api import custom_openapi

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    """
    Context manager for FastAPI app. It will run all code before `yield`
    on app startup, and will run code after `yield` on app shutdown.
    """
    try:
        # Create upload directories if they don't exist
        logger.info("Creating upload directories...")
        os.makedirs(settings.TEMP_UPLOAD_DIR, exist_ok=True)
        os.makedirs(settings.PROCESSED_FILES_DIR, exist_ok=True)
        
        # Initialize database
        logger.info("Initializing database...")
        await init_db()
        
        # Start cleanup task on application startup
        start_cleanup_task()
        logger.info("Application startup completed")
        yield
        
    except Exception as e:
        logger.error(f"Error during startup: {str(e)}", exc_info=True)
        raise
    finally:
        logger.info("Application shutdown")

app = FastAPI(
    title="EPUBox",
    description="A scalable EPUB translation service",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)

# CORS middleware configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Add session middleware
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY
)

# Add rate limiting middleware
app.add_middleware(RateLimitMiddleware)

# Include routers
app.include_router(users.router, prefix="/api/v1/users", tags=["users"])
app.include_router(translation.router, prefix="/api/v1/translation", tags=["translation"])
app.include_router(files.router, prefix="/api/v1/files", tags=["files"])

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    logger.error(f"HTTP Exception: {exc.status_code} - {exc.detail}")
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )

# Health check endpoint
@app.get("/health")
async def health_check():
    logger.debug("Health check requested")
    return {"status": "ok"}

# Use custom OpenAPI schema
app.openapi = lambda: custom_openapi(app)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
