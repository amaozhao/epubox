from fastapi import APIRouter

from app.api.v1 import api_router as api_v1_router

api_router = APIRouter()
api_router.include_router(api_v1_router, prefix="/api/v1")

__all__ = ["api_router"]
