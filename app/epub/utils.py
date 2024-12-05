"""
EPUB utility functions.
Contains utility functions for EPUB processing.
"""

import os
from pathlib import Path


def ensure_directory(directory: str | Path) -> None:
    """
    Ensure a directory exists, create if it doesn't.

    Args:
        directory: Path to the directory
    """
    if isinstance(directory, str):
        directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)


def get_file_extension(filename: str | Path) -> str:
    """
    Get the extension of a file.

    Args:
        filename: Name or path of the file

    Returns:
        str: File extension (lowercase)
    """
    if isinstance(filename, Path):
        return filename.suffix.lower()
    return os.path.splitext(filename)[1].lower()
