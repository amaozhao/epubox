import logging
import os
import sys
from typing import Optional

from agno.utils.log import (
    agent_logger,
    logger,
    team_logger,
    workflow_logger,
)

from .config import settings

# 用于跟踪已配置的logger，避免重复配置
_configured_loggers = set()


def _get_json_formatter() -> logging.Formatter:
    """创建 JSON 格式化器"""
    return logging.Formatter(
        '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "name": "%(name)s", "message": "%(message)s"}',
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _get_text_formatter() -> logging.Formatter:
    """创建文本格式化器"""
    return logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


def _setup_logger_handlers(logger_instance: logging.Logger, level_str: str) -> None:
    """为 logger 实例设置 handlers 和 formatter"""
    # 移除所有现有的 handlers
    for handler in logger_instance.handlers[:]:
        logger_instance.removeHandler(handler)

    # 设置日志级别
    try:
        log_level = getattr(logging, level_str.upper())
    except AttributeError:
        log_level = logging.INFO

    # 根据设置选择 formatter
    log_format = getattr(settings, "LOG_FORMAT", "text")
    formatter = _get_json_formatter() if log_format == "json" else _get_text_formatter()

    # 添加控制台 handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    logger_instance.addHandler(console_handler)

    # 添加文件 handler（如果配置了）
    log_file = getattr(settings, "LOG_FILE", None)
    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        logger_instance.addHandler(file_handler)

    logger_instance.setLevel(log_level)
    logger_instance.propagate = False


def setup_agno_logging():
    """配置 Agno 框架的日志记录器，使用统一的 JSON 格式

    注意：不能直接替换 logger 对象，因为其他模块已经导入了本地引用。
    因此我们修改已有 logger 的 handlers。
    """
    level_str = getattr(settings, "LOG_LEVEL", "INFO") or "INFO"

    # 修改 Agno 的各个 logger 的 handlers
    for agno_logger in (logger, agent_logger, team_logger, workflow_logger):
        _setup_logger_handlers(agno_logger, level_str)


def _create_logger(name: str, level: Optional[str] = None) -> logging.Logger:
    """创建配置好的日志记录器"""
    logger = logging.getLogger(name)

    # 如果logger已经配置过，重置它以应用新设置
    if name in _configured_loggers:
        # 移除所有现有的handler
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        # 重新设置级别
        logger.setLevel(logging.NOTSET)

    # 设置日志级别
    level_str = level or getattr(settings, "LOG_LEVEL", "INFO") or "INFO"
    try:
        log_level = getattr(logging, level_str.upper())
    except AttributeError:
        # 如果日志级别无效，使用INFO作为默认值
        log_level = logging.INFO

    logger.setLevel(log_level)

    # 创建格式化器
    log_format = getattr(settings, "LOG_FORMAT", "text")
    if log_format == "json":
        formatter = logging.Formatter(
            '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "name": "%(name)s", "message": "%(message)s"}',
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    else:
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )

    # 创建控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 创建文件处理器（如果需要）
    log_file = getattr(settings, "LOG_FILE", None)
    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    # 防止日志传播到根日志记录器
    logger.propagate = False

    # 标记此logger已配置
    _configured_loggers.add(name)

    return logger


def get_logger(name: str, level: Optional[str] = None) -> logging.Logger:
    """获取配置好的应用程序日志记录器（用于非 Agno 组件）"""
    return _create_logger(name, level)


# 在模块加载时自动设置 Agno 日志
setup_agno_logging()
engine_logger = get_logger("engine")
