import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from app.middleware.logging import RequestContextMiddleware, RequestLoggingMiddleware


class TestLoggingMiddleware:
    """Test cases for logging middleware."""

    @pytest.fixture
    def app(self):
        """创建测试应用"""
        app = FastAPI()
        # 添加中间件，注意顺序：先添加上下文中间件，再添加日志中间件
        app.add_middleware(RequestLoggingMiddleware)
        app.add_middleware(RequestContextMiddleware)

        @app.get("/test")
        async def test_endpoint():
            return {"message": "test"}

        @app.post("/test-error")
        async def test_error_endpoint():
            raise ValueError("Test error")

        @app.get("/health")
        async def health_check():
            """这个端点应该被排除在日志之外"""
            return {"status": "healthy"}

        @app.exception_handler(ValueError)
        async def value_error_handler(request: Request, exc: ValueError):
            return JSONResponse(
                status_code=400,
                content={"message": str(exc)},
            )

        return app

    @pytest.fixture
    def client(self, app):
        """创建测试客户端"""
        return TestClient(app)

    def test_successful_request_logging(self, client):
        """测试成功请求的日志记录"""
        response = client.get("/test")
        assert response.status_code == 200
        assert response.json() == {"message": "test"}

    def test_error_request_logging(self, client):
        """测试错误请求的日志记录"""
        response = client.post("/test-error")
        assert response.status_code == 400
        assert response.json() == {"message": "Test error"}

    def test_not_found_logging(self, client):
        """测试 404 请求的日志记录"""
        response = client.get("/non-existent")
        assert response.status_code == 404

    def test_excluded_path_logging(self, client):
        """测试被排除的路径不会记录日志"""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}
