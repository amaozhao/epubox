import logging
import sys
import structlog
from typing import Optional

def setup_logging(log_level: Optional[str] = None) -> None:
    """Configure structured logging for the application."""
    # Set log level
    level = getattr(logging, log_level.upper()) if log_level else logging.INFO
    
    # Configure standard logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    # Configure structlog
    structlog.configure(
        processors=[
            # Add extra context from contextvars
            structlog.contextvars.merge_contextvars,
            # Add timestamps
            structlog.processors.TimeStamper(fmt="iso"),
            # Add log level
            structlog.processors.add_log_level,
            # Add caller info
            structlog.processors.CallsiteParameterAdder(
                parameters={
                    "func_name": structlog.processors.CALLSITE_NAMES.FUNC_NAME,
                    "module": structlog.processors.CALLSITE_NAMES.MODULE,
                    "lineno": structlog.processors.CALLSITE_NAMES.LINENO,
                }
            ),
            # Add stack info for errors
            structlog.processors.StackInfoRenderer(),
            # Format exceptions
            structlog.processors.format_exc_info,
            # Handle any non-JSON-serializable values
            structlog.processors.UnicodeDecoder(),
            # Convert to JSON format
            structlog.processors.JSONRenderer(indent=None, sort_keys=True)
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

def get_logger(name: str = None) -> structlog.BoundLogger:
    """Get a structured logger instance with optional name binding."""
    logger = structlog.get_logger()
    if name:
        logger = logger.bind(logger_name=name)
    return logger
