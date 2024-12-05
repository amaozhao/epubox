"""
API router module.
Contains FastAPI router configuration.
"""

from fastapi import APIRouter

router = APIRouter()

# Import and include all endpoint routers
from .endpoints import files, tasks

router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
router.include_router(files.router, prefix="/files", tags=["files"])
