import logging
import sys
from pathlib import Path
from typing import Any

import structlog

from .config import settings

# 创建日志目录
log_dir = Path(settings.LOG_DIR)
log_dir.mkdir(parents=True, exist_ok=True)

# 设置标准库日志级别
logging.basicConfig(format=settings.LOG_FORMAT, level=settings.LOG_LEVEL)


def add_app_context(
    logger: structlog.types.BindableLogger, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """添加应用上下文信息"""
    event_dict["app"] = settings.APP_NAME
    return event_dict


def setup_logging() -> None:
    """配置结构化日志"""
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso"),
        add_app_context,
        structlog.processors.dict_tracebacks,
        structlog.processors.JSONRenderer(),
    ]

    if settings.DEBUG:
        # 在开发环境使用更友好的格式
        processors[-1] = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=processors,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(settings.LOG_LEVEL),
        cache_logger_on_first_use=True,
    )


# 创建日志记录器
def get_logger(name: str = None) -> structlog.BoundLogger:
    """获取结构化日志记录器"""
    logger = structlog.get_logger(name)
    return logger.bind(module=name)


# 初始化日志配置
setup_logging()

# 创建应用logger
app_logger = get_logger("app")

# 创建访问日志logger
access_logger = get_logger("access")

# 创建任务logger
task_logger = get_logger("task")

# 示例用法:
# app_logger.info("application_start", version="1.0.0")
# access_logger.info("request_received", path="/api/v1/translate", method="POST")
# task_logger.info("task_started", task_id="123", task_type="translation")
