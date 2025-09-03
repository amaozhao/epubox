"""
Logging configuration for the Epubox application using structlog.
"""

import logging
import os
import sys
from typing import List

import structlog
from structlog.types import Processor

from .config import settings


def configure_logging() -> None:
    """Configure structured logging for the application using structlog.

    Sets up logging processors, formatters,
    and handlers based on application settings.
    Configures structlog to work with
    standard library logging for third-party libraries.
    """
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    # Configure handlers based on settings
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    # Add file handler if LOG_FILE is specified
    if settings.LOG_FILE:
        os.makedirs(os.path.dirname(settings.LOG_FILE), exist_ok=True)
        file_handler = logging.FileHandler(settings.LOG_FILE)
        handlers.append(file_handler)

    # Configure standard library logging
    logging.basicConfig(level=log_level, format="%(message)s", handlers=handlers)

    # Set levels for third-party loggers to reduce noise
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Define shared processors for structlog
    shared_processors: List[Processor] = [
        # Add timestamps to log entries
        structlog.processors.TimeStamper(fmt="iso"),
        # Add log level to log entries
        structlog.processors.add_log_level,
        # Add logger name to log entries
        structlog.processors.StackInfoRenderer(),
    ]

    # Try to add callsite information
    # if supported by the installed structlog version
    try:
        # For newer structlog versions
        shared_processors.append(
            structlog.processors.CallsiteParameterAdder(
                parameters=["filename", "lineno", "func_name"]  # type: ignore
            )
        )
    except (KeyError, AttributeError):
        # For older structlog versions or if parameters aren't supported
        # Just skip this processor
        pass

    # Configure structlog based on the log format setting
    if settings.LOG_FORMAT.lower() == "json":
        # JSON formatter for production use
        structlog.configure(
            processors=shared_processors
            + [
                # Process any exceptions and include traceback info
                structlog.processors.format_exc_info,
                # Convert log entry to JSON format
                structlog.processors.JSONRenderer(ensure_ascii=False),
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
    else:
        # Console formatter for development use
        structlog.configure(
            processors=shared_processors
            + [
                # Process any exceptions and include traceback info
                structlog.processors.format_exc_info,
                # Add colors to console output
                structlog.dev.ConsoleRenderer(colors=True),
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a structured logger with the specified name.

    Args:
        name: The name of the logger, typically __name__ of the calling module.

    Returns:
        structlog.stdlib.BoundLogger: A configured structlog logger instance.
    """
    return structlog.get_logger(name)


configure_logging()
engine_logger = get_logger("engine")
