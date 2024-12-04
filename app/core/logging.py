import logging
import sys
from typing import Any

import structlog
from structlog.types import Processor

from app.core.config import settings


def setup_logging() -> None:
    """配置结构化日志"""

    # 设置日志级别
    logging.basicConfig(
        level=settings.LOG_LEVEL,
        stream=sys.stdout,
        format="%(message)s",
    )

    # 定义共享处理器
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    if settings.LOG_FORMAT == "json":
        # JSON格式的日志
        shared_processors.extend(
            [
                structlog.processors.format_exc_info,
                structlog.processors.UnicodeDecoder(),
                structlog.processors.JSONRenderer(),  # 移除 serializer=str
            ]
        )
    else:
        # 控制台格式的日志
        shared_processors.extend(
            [
                structlog.dev.ConsoleRenderer(
                    colors=True, exception_formatter=structlog.dev.plain_traceback
                )
            ]
        )

    structlog.configure(
        processors=shared_processors,
        logger_factory=structlog.PrintLoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.LOG_LEVEL)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(*args: Any, **kwargs: Any) -> structlog.BoundLogger:
    """获取结构化日志记录器"""
    return structlog.get_logger(*args, **kwargs)
