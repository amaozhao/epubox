"""Logging configuration for the application."""

import logging
import sys
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import Dict, Optional, List, Any, Union
from functools import lru_cache

import structlog
from structlog.types import Processor

# Constants
LOGS_DIR = Path("logs")
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_JSON_FORMAT = True
DEFAULT_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
MAX_BYTES = 10 * 1024 * 1024  # 10MB
BACKUP_COUNT = 5
LOG_FORMATS = {
    "json": "%(message)s",
    "text": "[%(asctime)s] %(levelname)s [%(name)s:%(lineno)s] %(message)s",
}

# Create logs directory if it doesn't exist
LOGS_DIR.mkdir(exist_ok=True)


class LogConfig:
    """Centralized logging configuration."""

    def __init__(self):
        self.log_level = DEFAULT_LOG_LEVEL
        self.json_format = DEFAULT_JSON_FORMAT
        self.timestamp_format = DEFAULT_TIMESTAMP_FORMAT
        self._configured = False

    @property
    def configured(self) -> bool:
        return self._configured

    @configured.setter
    def configured(self, value: bool):
        self._configured = value


# Global configuration instance
log_config = LogConfig()


@lru_cache(maxsize=None)
def get_log_file_path(name: Optional[str] = None) -> str:
    """Get the log file path based on the logger name.

    Args:
        name: Optional logger name

    Returns:
        str: Path to the log file
    """
    if name:
        return str(LOGS_DIR / f"{name}.log")
    return str(LOGS_DIR / "app.log")


def create_rotating_handler(
    log_file: str,
    max_bytes: int = MAX_BYTES,
    backup_count: int = BACKUP_COUNT,
    formatter: Optional[logging.Formatter] = None,
) -> RotatingFileHandler:
    """Create a size-based rotating file handler.

    Args:
        log_file: Path to log file
        max_bytes: Maximum size of each log file
        backup_count: Number of backup files to keep
        formatter: Optional custom formatter

    Returns:
        RotatingFileHandler: Configured handler
    """
    handler = RotatingFileHandler(
        filename=log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )

    if formatter:
        handler.setFormatter(formatter)
    else:
        format_str = LOG_FORMATS["json" if log_config.json_format else "text"]
        handler.setFormatter(logging.Formatter(format_str))

    return handler


def create_timed_rotating_handler(
    log_file: str,
    when: str = "midnight",
    interval: int = 1,
    backup_count: int = BACKUP_COUNT,
    formatter: Optional[logging.Formatter] = None,
) -> TimedRotatingFileHandler:
    """Create a time-based rotating file handler.

    Args:
        log_file: Path to log file
        when: Type of interval (S/M/H/D/midnight/W0-W6)
        interval: Number of intervals
        backup_count: Number of backup files to keep
        formatter: Optional custom formatter

    Returns:
        TimedRotatingFileHandler: Configured handler
    """
    handler = TimedRotatingFileHandler(
        filename=log_file,
        when=when,
        interval=interval,
        backupCount=backup_count,
        encoding="utf-8",
    )

    if formatter:
        handler.setFormatter(formatter)
    else:
        format_str = LOG_FORMATS["json" if log_config.json_format else "text"]
        handler.setFormatter(logging.Formatter(format_str))

    return handler


def setup_file_handler(
    name: Optional[str] = None, rotation_type: str = "size"
) -> Union[RotatingFileHandler, TimedRotatingFileHandler]:
    """Setup a rotating file handler for logging.

    Args:
        name: Optional logger name
        rotation_type: Type of rotation ('size' or 'time')

    Returns:
        Union[RotatingFileHandler, TimedRotatingFileHandler]: Configured handler
    """
    log_file = get_log_file_path(name)

    if rotation_type == "time":
        return create_timed_rotating_handler(log_file)
    return create_rotating_handler(log_file)


def get_logger(name: Optional[str] = None) -> structlog.BoundLogger:
    """Get a logger instance for the given name.

    Args:
        name: Optional logger name

    Returns:
        structlog.BoundLogger: Configured logger instance
    """
    logger = structlog.get_logger(name)
    stdlib_logger = logging.getLogger(name if name else "")

    # Add file handler if not already added
    if not any(
        isinstance(h, (RotatingFileHandler, TimedRotatingFileHandler))
        for h in stdlib_logger.handlers
    ):
        stdlib_logger.addHandler(setup_file_handler(name))

    return logger


def setup_logging(
    log_level: str = DEFAULT_LOG_LEVEL,
    json_format: bool = DEFAULT_JSON_FORMAT,
    timestamp_format: str = DEFAULT_TIMESTAMP_FORMAT,
) -> None:
    """Configure structured logging for the application.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        json_format: Whether to output logs in JSON format
        timestamp_format: Format for timestamp in logs
    """
    if log_config.configured:
        return

    # Update global config
    log_config.log_level = log_level
    log_config.json_format = json_format
    log_config.timestamp_format = timestamp_format

    # Set logging level
    logging.basicConfig(
        format=LOG_FORMATS["json" if json_format else "text"],
        stream=sys.stdout,
        level=getattr(logging, log_level.upper()),
    )

    # Configure processors
    processors: List[Processor] = [
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt=timestamp_format),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.CallsiteParameterAdder(
            additional_ignores=["logging"],
        ),
    ]

    if json_format:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Log startup information
    logger = get_logger("app.core.logging")
    logger.info(
        "logging_initialized",
        log_level=log_level,
        json_format=json_format,
        logs_dir=str(LOGS_DIR),
        timestamp_format=timestamp_format,
    )

    log_config.configured = True


# Module-level loggers
app_logger = get_logger("app")  # Root application logger
auth_logger = get_logger("app.core.auth")
validation_logger = get_logger("app.core.validation")
schemas_logger = get_logger("app.schemas")
crud_logger = get_logger("app.crud")
db_logger = get_logger("app.db")
migrations_logger = get_logger("alembic")
exceptions_logger = get_logger("app.core.exceptions")
models_logger = get_logger("app.models")
epub_logger = get_logger("app.core.epub")  # Logger for EPUB operations
api_logger = get_logger("app.api")
test_logger = get_logger("tests")

# Initialize logging with default settings
setup_logging()
