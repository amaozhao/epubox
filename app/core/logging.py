import json
import logging
import sys
import time
import uuid
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Dict
from contextvars import ContextVar

import structlog
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import Message

from app.core.config import settings

# 创建上下文变量来存储请求ID
REQUEST_ID_CTX_KEY = "request_id"
_request_id_ctx_var: ContextVar[str] = ContextVar(REQUEST_ID_CTX_KEY, default=None)

# 创建日志实例
logger = structlog.get_logger(__name__)


def get_request_id() -> str:
    return _request_id_ctx_var.get()


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = str(uuid.uuid4())
        # 设置请求ID到上下文
        _request_id_ctx_var.set(request_id)

        # 将请求ID添加到响应头
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        start_time = time.time()

        # 获取请求ID
        request_id = get_request_id()
        log = logger.bind(request_id=request_id)

        # 记录请求信息
        request_body = await self._get_request_body(request)
        log.info(
            "incoming_request",
            http={
                "method": request.method,
                "path": request.url.path,
                "query_params": dict(request.query_params),
                "headers": self._filter_headers(dict(request.headers)),
                "body": self._filter_sensitive_data(request_body),
            },
        )

        # 处理响应
        try:
            response = await call_next(request)
            response_body = await self._get_response_body(response)

            # 记录响应信息
            log.info(
                "outgoing_response",
                http={
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "body": self._filter_sensitive_data(response_body),
                },
                processing_time=f"{(time.time() - start_time):.4f}s",
            )

            return response
        except Exception as e:
            log.error(
                "request_failed",
                error=str(e),
                exc_info=True,
                processing_time=f"{(time.time() - start_time):.4f}s",
            )
            raise

    @staticmethod
    def _filter_headers(headers: Dict) -> Dict:
        """过滤敏感header信息"""
        sensitive_headers = {"authorization", "cookie"}
        return {
            k: v if k.lower() not in sensitive_headers else "****"
            for k, v in headers.items()
        }

    @staticmethod
    def _filter_sensitive_data(data: Dict) -> Dict:
        """过滤敏感数据"""
        if not isinstance(data, dict):
            return data

        sensitive_fields = {"password", "token", "access_token", "refresh_token"}
        return {k: "****" if k in sensitive_fields else v for k, v in data.items()}

    async def _get_request_body(self, request: Request) -> Dict:
        """获取请求体"""
        body = await request.body()
        try:
            return json.loads(body)
        except:
            return {}

    async def _get_response_body(self, response: Response) -> Dict:
        """获取响应体"""
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        try:
            return json.loads(body)
        except:
            return {}


def setup_logging_handlers() -> Dict[str, Any]:
    """设置日志处理器"""
    # 创建日志目录结构
    for log_type in ["info", "error", "debug"]:
        log_path = settings.LOG_PATHS[log_type].parent
        log_path.mkdir(parents=True, exist_ok=True)

    # 配置控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(message)s"))

    # 配置INFO级别日志文件处理器
    info_handler = TimedRotatingFileHandler(
        filename=settings.LOG_PATHS["info"],
        when=settings.LOG_ROTATION_TIME,
        interval=1,
        backupCount=settings.LOG_INFO_RETENTION_DAYS,
        encoding="utf-8",
    )
    info_handler.setFormatter(logging.Formatter("%(message)s"))
    info_handler.setLevel(logging.INFO)
    info_handler.addFilter(lambda record: record.levelno == logging.INFO)

    # 配置ERROR级别日志文件处理器
    error_handler = TimedRotatingFileHandler(
        filename=settings.LOG_PATHS["error"],
        when=settings.LOG_ROTATION_TIME,
        interval=1,
        backupCount=settings.LOG_ERROR_RETENTION_DAYS,
        encoding="utf-8",
    )
    error_handler.setFormatter(logging.Formatter("%(message)s"))
    error_handler.setLevel(logging.ERROR)

    handlers = [console_handler, info_handler, error_handler]

    # 如果启用了DEBUG日志，添加DEBUG处理器
    if settings.ENABLE_DEBUG_LOGGING:
        debug_handler = TimedRotatingFileHandler(
            filename=settings.LOG_PATHS["debug"],
            when=settings.LOG_ROTATION_TIME,
            interval=1,
            backupCount=settings.LOG_DEBUG_RETENTION_DAYS,
            encoding="utf-8",
        )
        debug_handler.setFormatter(logging.Formatter("%(message)s"))
        debug_handler.setLevel(logging.DEBUG)
        debug_handler.addFilter(lambda record: record.levelno == logging.DEBUG)
        handlers.append(debug_handler)

    return {"handlers": handlers}


def setup_logging():
    """配置结构化日志"""
    # 获取日志处理器配置
    handlers_config = setup_logging_handlers()

    # 配置标准库日志
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL),
        format="%(message)s",
        handlers=handlers_config["handlers"],
    )

    # 配置structlog
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.CallsiteParameterAdder(
                parameters={"function", "module", "lineno"}
            ),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(indent=None, sort_keys=True),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


# 创建日志记录器
logger = logging.getLogger("epubox")
logger.setLevel(settings.LOG_LEVEL)

# 控制台处理器
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(settings.LOG_LEVEL)

# 日志格式
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
console_handler.setFormatter(formatter)

# 添加处理器
logger.addHandler(console_handler)


def get_logger(name: str) -> logging.Logger:
    """获取日志记录器"""
    return logging.getLogger(f"epubox.{name}")
