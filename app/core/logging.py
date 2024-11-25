"""Logging configuration for the application."""

import logging
import sys
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Optional, List, Any

import structlog
from structlog.types import Processor

# Create logs directory if it doesn't exist
LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)


def get_log_file_path(name: Optional[str] = None) -> str:
    """Get the log file path based on the logger name."""
    if name:
        return str(LOGS_DIR / f"{name}.log")
    return str(LOGS_DIR / "app.log")


def setup_file_handler(name: Optional[str] = None) -> RotatingFileHandler:
    """Setup a rotating file handler for logging."""
    log_file = get_log_file_path(name)

    handler = RotatingFileHandler(
        filename=log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def get_logger(name: Optional[str] = None) -> structlog.BoundLogger:
    """Get a logger instance for the given name."""
    logger = structlog.get_logger(name)
    # Add file handler if not already added
    stdlib_logger = logging.getLogger(name if name else "")
    if not any(isinstance(h, RotatingFileHandler) for h in stdlib_logger.handlers):
        stdlib_logger.addHandler(setup_file_handler(name))
    return logger


def setup_logging(
    log_level: str = "INFO",
    json_format: bool = True,
    timestamp_format: str = "%Y-%m-%dT%H:%M:%S.%fZ",
) -> None:
    """
    Configure structured logging for the application.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        json_format: Whether to output logs in JSON format
        timestamp_format: Format for timestamp in logs
    """
    # Set logging level
    logging.basicConfig(
        format="%(message)s",
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
        # Add file and line number
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
