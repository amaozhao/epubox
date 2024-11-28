"""API v1 endpoints."""

from fastapi import APIRouter

from . import epub_files, translation

api_router = APIRouter()

api_router.include_router(
    epub_files.router,
    prefix="/files",
    tags=["files"],
)

api_router.include_router(
    translation.router,
    prefix="/translation",
    tags=["translation"],
)
