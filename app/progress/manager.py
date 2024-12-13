"""Progress management module."""

from datetime import datetime
from typing import Dict, Optional

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from app.db.base import get_async_session
from app.db.models import TranslationProgress, TranslationStatus


class ProgressManager:
    """Manager for handling translation progress updates."""

    async def create_progress(
        self, book_id: str, chapters: Dict
    ) -> TranslationProgress:
        """Create a new progress record.

        Args:
            book_id: Identifier of the book
            chapters: Dictionary containing chapter information

        Returns:
            TranslationProgress: Created progress record
        """
        async for session in get_async_session():
            progress = TranslationProgress(
                book_id=book_id,
                total_chapters=chapters,
                completed_chapters={},
                status=TranslationStatus.PENDING,
                started_at=datetime.now(),
            )
            session.add(progress)
            await session.commit()
            await session.refresh(progress)
            return progress

    async def get_progress(self, book_id: str) -> Optional[TranslationProgress]:
        """Get progress record for a book.

        Args:
            book_id: Identifier of the book

        Returns:
            Optional[TranslationProgress]: Progress record for the book, or None if not found
        """
        async for session in get_async_session():
            stmt = select(TranslationProgress).where(
                TranslationProgress.book_id == book_id
            )
            result = await session.execute(stmt)
            try:
                return result.scalar_one()
            except NoResultFound:
                return None

    async def update_chapter(self, book_id: str, chapter_id: str) -> None:
        """Update chapter completion status.

        Args:
            book_id: Identifier of the book
            chapter_id: Identifier of the chapter
        """
        async for session in get_async_session():
            progress = await self._get_progress(session, book_id)
            if chapter_id in progress.total_chapters:
                progress.update_chapter_status(chapter_id, "completed", datetime.now())
                await session.commit()
                await session.refresh(progress)

    async def start_translation(self, book_id: str) -> None:
        """Mark translation as started.

        Args:
            book_id: Identifier of the book
        """
        async for session in get_async_session():
            progress = await self._get_progress(session, book_id)
            progress.status = TranslationStatus.PROCESSING
            progress.started_at = datetime.now()
            await session.commit()
            await session.refresh(progress)

    async def complete_translation(self, book_id: str) -> None:
        """Mark translation as completed.

        Args:
            book_id: Identifier of the book
        """
        async for session in get_async_session():
            progress = await self._get_progress(session, book_id)
            progress.status = TranslationStatus.COMPLETED
            progress.completed_at = datetime.now()
            await session.commit()
            await session.refresh(progress)

    @staticmethod
    async def _get_progress(session, book_id: str) -> TranslationProgress:
        """Internal method to get progress with an existing session.

        Args:
            session: SQLAlchemy async session
            book_id: Identifier of the book

        Returns:
            TranslationProgress: Progress record for the book
        """
        stmt = select(TranslationProgress).where(TranslationProgress.book_id == book_id)
        result = await session.execute(stmt)
        return result.scalar_one()
