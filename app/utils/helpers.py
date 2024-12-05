"""
Helper functions module.
Contains utility functions used across the application.
"""

import uuid
from datetime import datetime
from typing import Optional


def generate_task_id() -> str:
    """Generate a unique task ID."""
    return str(uuid.uuid4())


def format_timestamp(dt: Optional[datetime] = None) -> str:
    """
    Format a timestamp in ISO format.

    Args:
        dt: Datetime object (defaults to current time if not specified)

    Returns:
        str: Formatted timestamp
    """
    if dt is None:
        dt = datetime.now()
    return dt.isoformat()
