import logging
import os
import sys
from typing import Optional

from agno.utils.log import configure_agno_logging

from .config import settings

# 用于跟踪已配置的logger，避免重复配置
_configured_loggers = set()


def setup_agno_logging():
    """配置 Agno 框架的日志记录器"""
    # 创建各个组件的日志记录器
    agent_logger = _create_logger("agno.agent", settings.LOG_LEVEL)
    team_logger = _create_logger("agno.team", settings.LOG_LEVEL)
    workflow_logger = _create_logger("agno.workflow", settings.LOG_LEVEL)

    # 配置 Agno 使用自定义日志记录器
    configure_agno_logging(
        custom_agent_logger=agent_logger, custom_team_logger=team_logger, custom_workflow_logger=workflow_logger
    )


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
    level_str = level or getattr(settings, "LOG_LEVEL", "INFO")
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
