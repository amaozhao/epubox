import os
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import List
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

class FileCleanup:
    def __init__(self, temp_dir: str, max_age_hours: int = 24):
        self.temp_dir = Path(temp_dir)
        self.max_age = timedelta(hours=max_age_hours)

    async def cleanup_old_files(self) -> List[str]:
        """
        Remove temporary files older than max_age.
        Returns list of removed files.
        """
        removed_files = []
        current_time = datetime.now()

        try:
            for file_path in self.temp_dir.glob("*"):
                if not file_path.is_file():
                    continue

                file_age = current_time - datetime.fromtimestamp(file_path.stat().st_mtime)
                if file_age > self.max_age:
                    try:
                        file_path.unlink()
                        removed_files.append(str(file_path))
                        logger.info(f"Removed old temporary file: {file_path}")
                    except Exception as e:
                        logger.error(f"Failed to remove file {file_path}: {str(e)}")

        except Exception as e:
            logger.error(f"Error during cleanup: {str(e)}")

        return removed_files

async def cleanup_task():
    """
    Background task to periodically clean up old temporary files.
    """
    temp_dir = Path(settings.UPLOAD_DIR) / "temp"
    cleanup = FileCleanup(temp_dir)

    while True:
        try:
            removed_files = await cleanup.cleanup_old_files()
            if removed_files:
                logger.info(f"Cleaned up {len(removed_files)} old temporary files")
        except Exception as e:
            logger.error(f"Error in cleanup task: {str(e)}")

        # Wait for next cleanup cycle (every 1 hour)
        await asyncio.sleep(3600)

def start_cleanup_task():
    """
    Start the cleanup task in the background.
    """
    asyncio.create_task(cleanup_task())
