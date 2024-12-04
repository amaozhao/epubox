import contextvars
import time
import uuid
from typing import Any, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from app.core.logging import get_logger

logger = get_logger(__name__)

# 创建上下文变量
request_id_ctx_var = contextvars.ContextVar("request_id", default=None)
user_id_ctx_var = contextvars.ContextVar("user_id", default=None)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """请求上下文中间件，用于在整个请求过程中传递上下文信息"""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # 生成请求ID
        request_id = str(uuid.uuid4())
        # 设置请求ID到上下文
        request_id_ctx_var.set(request_id)

        # 尝试获取用户ID（如果用户已认证）
        try:
            user = getattr(request.state, "user", None)
            if user:
                user_id_ctx_var.set(user.id)
        except:
            pass

        # 处理请求
        response = await call_next(request)
        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        *,
        exclude_paths: set[str] | None = None,
    ) -> None:
        super().__init__(app)
        self.exclude_paths = exclude_paths or {"/health", "/metrics"}

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path in self.exclude_paths:
            return await call_next(request)

        request_id = request_id_ctx_var.get()
        start_time = time.time()

        # 记录请求信息
        body = await self._get_request_body(request)
        logger.info(
            "incoming_request",
            request_id=request_id,
            method=request.method,
            url=str(request.url),
            client_host=request.client.host if request.client else None,
            headers=dict(request.headers),
            body=body,
            user_id=user_id_ctx_var.get(),
        )

        # 处理请求
        response = await call_next(request)

        # 记录响应信息
        response_body = await self._get_response_body(response)
        process_time = (time.time() - start_time) * 1000
        logger.info(
            "outgoing_response",
            request_id=request_id,
            status_code=response.status_code,
            headers=dict(response.headers),
            body=response_body,
            process_time_ms=round(process_time, 2),
            user_id=user_id_ctx_var.get(),
        )

        return response

    async def _get_request_body(self, request: Request) -> str | dict | None:
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                body = await request.json()
                # 处理敏感信息
                if isinstance(body, dict):
                    body = self._mask_sensitive_data(body)
                return body
            except:
                try:
                    body = await request.body()
                    return body.decode()
                except:
                    return None
        return None

    async def _get_response_body(self, response: Response) -> str | dict | None:
        try:
            body = response.body.decode()
            try:
                import json

                body = json.loads(body)
                # 处理敏感信息
                if isinstance(body, dict):
                    body = self._mask_sensitive_data(body)
            except:
                pass
            return body
        except:
            return None

    def _mask_sensitive_data(self, data: dict) -> dict:
        """掩盖敏感信息"""
        sensitive_fields = {
            "password",
            "token",
            "access_token",
            "refresh_token",
            "secret",
            "api_key",
            "private_key",
            "authorization",
        }

        masked_data = data.copy()
        for key in data:
            lower_key = key.lower()
            if any(sensitive in lower_key for sensitive in sensitive_fields):
                masked_data[key] = "***MASKED***"
            elif isinstance(data[key], dict):
                masked_data[key] = self._mask_sensitive_data(data[key])
            elif isinstance(data[key], list):
                masked_data[key] = [
                    self._mask_sensitive_data(item) if isinstance(item, dict) else item
                    for item in data[key]
                ]
        return masked_data


def get_request_id() -> str | None:
    """获取当前请求ID"""
    return request_id_ctx_var.get()


def get_user_id() -> Any | None:
    """获取当前用户ID"""
    return user_id_ctx_var.get()
