from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import api_router
from app.core.config import settings
from app.core.exceptions import EpuBoxException
from app.core.logging import get_logger, setup_logging
from app.middleware import RequestContextMiddleware, RequestLoggingMiddleware

logger = get_logger(__name__)

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
)

# 设置 CORS
if settings.BACKEND_CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(origin) for origin in settings.BACKEND_CORS_ORIGINS],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# 添加中间件
app.add_middleware(RequestContextMiddleware)
app.add_middleware(RequestLoggingMiddleware)


# 异常处理器
@app.exception_handler(EpuBoxException)
async def epubox_exception_handler(request: Request, exc: EpuBoxException):
    logger.error(
        "Exception occurred",
        error_code=exc.error_code,
        message=exc.message,
        details=exc.details,
        path=request.url.path,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error_code": exc.error_code,
            "message": exc.message,
            "details": exc.details,
        },
    )


# 注册路由
app.include_router(api_router, prefix=settings.API_V1_STR)

# 设置日志
setup_logging()


@app.on_event("startup")
async def startup_event():
    logger.info(
        "Starting up application",
        project_name=settings.PROJECT_NAME,
        version=settings.VERSION,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
