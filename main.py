from contextlib import asynccontextmanager
from typing import Union

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import api_router
from app.core.config import settings
from app.core.logging import (
    get_logger,
    RequestContextMiddleware,
    RequestLoggingMiddleware,
)
from app.core.exceptions import (
    EpuBoxException,
    DatabaseException,
    AuthenticationException,
)

# 获取日志记录器
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理
    """
    # 启动时执行
    logger.info("Starting up...")
    # 这里可以添加数据库连接、缓存初始化等
    yield
    # 关闭时执行
    logger.info("Shutting down...")
    # 这里可以添加资源清理代码


# 异常处理器
async def epubox_exception_handler(request: Request, exc: EpuBoxException):
    """处理自定义异常"""
    logger.error(
        "Application error",
        error_type=type(exc).__name__,
        error_code=exc.error_code,
        message=exc.message,
        details=exc.details,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "message": exc.message,
            "error_code": exc.error_code,
            "details": exc.details,
        },
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """处理 HTTP 异常"""
    logger.warning(
        "HTTP error",
        status_code=exc.status_code,
        detail=str(exc.detail),
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "message": str(exc.detail),
            "error_code": exc.status_code,
        },
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """处理请求验证异常"""
    logger.warning(
        "Validation error",
        errors=exc.errors(),
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "message": "Validation error",
            "error_code": status.HTTP_422_UNPROCESSABLE_ENTITY,
            "details": exc.errors(),
        },
    )


# 创建应用实例
app = FastAPI(
    title=settings.PROJECT_NAME,
    description=settings.PROJECT_DESCRIPTION,
    version=settings.VERSION,
    docs_url="/api/v1/docs",
    openapi_url="/api/v1/openapi.json",
    lifespan=lifespan,
)

# 注册异常处理器
app.add_exception_handler(EpuBoxException, epubox_exception_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)

# 添加中间件
app.add_middleware(RequestContextMiddleware)  # 请求上下文中间件
app.add_middleware(RequestLoggingMiddleware)  # 请求日志中间件

# 设置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 健康检查
@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "healthy", "version": settings.VERSION}


# API 版本路由
app.include_router(api_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
        log_level="debug" if settings.DEBUG else "info",
    )
