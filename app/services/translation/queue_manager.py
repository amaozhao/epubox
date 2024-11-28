"""Translation queue manager for handling asynchronous translation tasks."""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.crud.epub_file import epub_file
from app.models.epub_file import EPUBFile
from app.schemas.translation import TranslationRequest, TranslationStatus

from .epub_translator import EPUBTranslator

logger = logging.getLogger(__name__)


class TranslationQueueManager:
    """Manages asynchronous translation tasks."""

    def __init__(self):
        """Initialize the queue manager."""
        self._tasks: Dict[str, asyncio.Task] = {}
        self._status: Dict[str, TranslationStatus] = {}
        self._retry_configs: Dict[str, Dict] = {}
        self._temp_storage: Dict[str, Dict] = {}

    async def submit_translation(
        self,
        db: AsyncSession,
        file_id: int,
        source_lang: str,
        target_lang: str,
        user_id: int,
        provider: Optional[str] = None,
    ) -> str:
        """Submit a new translation task.

        Args:
            db: Database session
            file_id: ID of the file to translate
            source_lang: Source language code
            target_lang: Target language code
            user_id: ID of the requesting user
            provider: Optional translation provider

        Returns:
            Task ID
        """
        # Generate task ID
        task_id = f"{user_id}_{file_id}_{datetime.now().timestamp()}"

        # Get file info
        db_file = await epub_file.get(db, id=file_id)
        if not db_file:
            raise ValueError(f"File {file_id} not found")

        # Create status entry
        self._status[task_id] = TranslationStatus(
            task_id=task_id,
            status="queued",
            progress=0,
            file_id=file_id,
            source_lang=source_lang,
            target_lang=target_lang,
            created_at=datetime.now(),
        )

        # Create and start translation task
        task = asyncio.create_task(
            self._process_translation(
                task_id, db_file, source_lang, target_lang, provider
            )
        )
        self._tasks[task_id] = task

        return task_id

    async def get_status(self, task_id: str) -> Optional[TranslationStatus]:
        """Get the status of a translation task.

        Args:
            task_id: Task ID to check

        Returns:
            Translation status or None if not found
        """
        return self._status.get(task_id)

    async def cancel_translation(self, task_id: str) -> bool:
        """Cancel a translation task.

        Args:
            task_id: Task ID to cancel

        Returns:
            True if cancelled, False if not found or already completed
        """
        task = self._tasks.get(task_id)
        if task and not task.done():
            task.cancel()
            if task_id in self._status:
                self._status[task_id].status = "cancelled"
            return True
        return False

    async def _process_translation(
        self,
        task_id: str,
        db_file: EPUBFile,
        source_lang: str,
        target_lang: str,
        provider: Optional[str] = None,
    ):
        """Process a translation task with error recovery.

        Args:
            task_id: Task ID
            db_file: Database file object
            source_lang: Source language code
            target_lang: Target language code
            provider: Optional translation provider
        """
        try:
            # Initialize retry configuration
            self._retry_configs[task_id] = {
                "max_retries": 3,
                "retry_delay": 5,
                "current_retry": 0,
            }

            # Initialize temporary storage for partial results
            self._temp_storage[task_id] = {
                "translated_items": [],
                "failed_items": [],
                "current_progress": 0,
                "file": db_file,
                "provider": provider,
            }

            # Update status
            self._status[task_id].status = "processing"

            # Create translator
            translator = await EPUBTranslator.create(
                source_lang,
                target_lang,
                provider=provider,
            )

            # Generate output path
            output_filename = (
                f"{os.path.splitext(db_file.filename)[0]}_{target_lang}.epub"
            )
            output_path = os.path.join(settings.UPLOAD_DIR, output_filename)

            # Check for existing partial translation
            temp_path = f"{output_path}.temp"
            if os.path.exists(temp_path):
                logger.info(f"Found partial translation for task {task_id}")
                self._temp_storage[task_id] = self._load_temp_data(temp_path)

            # Translate file with progress updates
            stats = await translator.translate_epub(
                db_file.file_path,
                output_path,
                progress_callback=lambda p: self._update_progress(task_id, p),
                temp_storage=self._temp_storage[task_id],
            )

            # Update status with success
            self._status[task_id].status = "completed"
            self._status[task_id].progress = 100
            self._status[task_id].result = {
                "output_path": output_path,
                "stats": stats,
            }

            # Clean up temporary files
            if os.path.exists(temp_path):
                os.remove(temp_path)

        except asyncio.CancelledError:
            logger.info(f"Translation task {task_id} was cancelled")
            self._save_temp_data(task_id)
            raise

        except Exception as e:
            logger.error(f"Translation task {task_id} failed: {e}")

            # Check if we should retry
            if self._should_retry(task_id):
                logger.info(f"Retrying translation task {task_id}")
                await self._retry_translation(task_id)
            else:
                self._status[task_id].status = "failed"
                self._status[task_id].error = str(e)
                self._save_temp_data(task_id)

        finally:
            # Clean up task
            if task_id in self._tasks:
                del self._tasks[task_id]
            if task_id in self._retry_configs:
                del self._retry_configs[task_id]

    def _should_retry(self, task_id: str) -> bool:
        """Check if a task should be retried."""
        config = self._retry_configs.get(task_id, {})
        return (
            config.get("current_retry", 0) < config.get("max_retries", 3)
            and self._status[task_id].progress < 100
        )

    async def _retry_translation(self, task_id: str):
        """Retry a failed translation task."""
        config = self._retry_configs[task_id]
        config["current_retry"] += 1

        # Wait before retrying
        await asyncio.sleep(config["retry_delay"])

        # Update status
        self._status[task_id].status = "retrying"
        self._status[task_id].error = None

        # Create new task
        task = asyncio.create_task(
            self._process_translation(
                task_id,
                self._temp_storage[task_id]["file"],
                self._status[task_id].source_lang,
                self._status[task_id].target_lang,
                self._temp_storage[task_id].get("provider"),
            )
        )
        self._tasks[task_id] = task

    def _save_temp_data(self, task_id: str):
        """Save temporary translation data for recovery."""
        temp_data = self._temp_storage.get(task_id)
        if not temp_data:
            return

        output_path = self._status[task_id].result.get("output_path", "")
        if output_path:
            temp_path = f"{output_path}.temp"
            try:
                with open(temp_path, "w") as f:
                    json.dump(temp_data, f)
                logger.info(f"Saved temporary data for task {task_id}")
            except Exception as e:
                logger.error(f"Failed to save temporary data: {e}")

    def _load_temp_data(self, temp_path: str) -> Dict:
        """Load temporary translation data for recovery."""
        try:
            with open(temp_path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load temporary data: {e}")
            return {}

    def _update_progress(self, task_id: str, progress: Dict):
        """Update translation progress."""
        if task_id in self._status:
            self._status[task_id].progress = progress.get("progress", 0)
            self._temp_storage[task_id]["current_progress"] = progress.get(
                "progress", 0
            )
            self._temp_storage[task_id]["translated_items"] = progress.get(
                "translated_items", []
            )
            self._temp_storage[task_id]["failed_items"] = progress.get(
                "failed_items", []
            )


# Global queue manager instance
queue_manager = TranslationQueueManager()
