from fastapi import APIRouter

from app.api.v1 import auth

api_router = APIRouter(prefix="/api/v1")

# 添加各模块的路由
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
