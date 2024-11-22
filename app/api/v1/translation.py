"""Translation endpoints."""

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from sqlalchemy.orm import declarative_base
from typing import Optional
import os
import logging

from app.db.session import get_session
from app.models.user import User
from app.models.translation import TranslationTask, TranslationStatus, TranslationService
from app.models.file import FileMetadata, FileStatus
from app.services.translation.base import TranslationRequest, TranslationResponse
from app.services.translation.google_translate import GoogleTranslateAdapter
from app.services.epub_processor import EPUBProcessor
from app.api.v1.users import get_current_user
from app.core.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

# Initialize services
translation_service = GoogleTranslateAdapter()
epub_processor = EPUBProcessor(translation_service)

async def update_task_progress(
    task_id: int,
    progress: float,
    session: AsyncSession
):
    """Update translation task progress."""
    await session.execute(
        text("UPDATE translation_tasks SET progress = :progress WHERE id = :task_id"),
        {"progress": progress, "task_id": task_id}
    )
    await session.commit()

async def process_translation(
    task_id: int,
    session: AsyncSession
):
    """Background task for processing translation."""
    try:
        # Get task and file information
        task = await session.get(TranslationTask, task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return

        source_file = await session.get(FileMetadata, task.file_id)
        if not source_file:
            logger.error(f"Source file not found for task {task_id}")
            await session.execute(
                text("""UPDATE translation_tasks 
                SET status = :status, 
                    error_message = :error
                WHERE id = :task_id"""),
                {
                    "status": TranslationStatus.FAILED,
                    "error": "Source file not found",
                    "task_id": task_id
                }
            )
            await session.commit()
            return
        
        # Update task status
        await session.execute(
            text("""UPDATE translation_tasks 
            SET status = :status, started_at = CURRENT_TIMESTAMP
            WHERE id = :task_id"""),
            {
                "status": TranslationStatus.PROCESSING,
                "task_id": task_id
            }
        )
        await session.commit()

        # Process translation
        result_file_path = await epub_processor.translate_epub(
            source_file.file_path,
            task.source_language,
            task.target_language,
            lambda progress: update_task_progress(task_id, progress, session)
        )

        # Create result file metadata
        result_filename = f"translated_{source_file.filename}"
        result_file = FileMetadata(
            user_id=task.user_id,
            filename=result_filename,
            file_path=result_file_path,
            file_size=os.path.getsize(result_file_path),
            status=FileStatus.TRANSLATED
        )
        session.add(result_file)
        await session.flush()
        await session.refresh(result_file)

        # Update task status
        await session.execute(
            text("""UPDATE translation_tasks 
            SET status = :status, 
                completed_at = CURRENT_TIMESTAMP,
                result_file_id = :result_file_id,
                progress = 100
            WHERE id = :task_id"""),
            {
                "status": TranslationStatus.COMPLETED,
                "result_file_id": result_file.id,
                "task_id": task_id
            }
        )
        await session.commit()
        
    except Exception as e:
        logger.error(f"Error processing translation: {str(e)}", exc_info=True)
        # Update task with error status
        await session.execute(
            text("""UPDATE translation_tasks 
            SET status = :status, 
                error_message = :error
            WHERE id = :task_id"""),
            {
                "status": TranslationStatus.FAILED,
                "error": str(e),
                "task_id": task_id
            }
        )
        await session.commit()

@router.post("/translate")
async def translate_epub(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    source_language: str = "en",
    target_language: str = "es",
    service: TranslationService = TranslationService.GOOGLE,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Start EPUB translation task."""
    try:
        logger.info(f"Starting EPUB translation task for user: {current_user.email}")
        logger.debug(f"File: {file.filename}, Source: {source_language}, Target: {target_language}")
        
        # Validate file extension
        if not file.filename.lower().endswith('.epub'):
            logger.warning(f"Invalid file type: {file.filename}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only EPUB files are supported"
            )

        # Save uploaded file
        file_content = await file.read()
        file_path = await epub_processor.save_uploaded_file(file_content, file.filename)
        logger.debug(f"Saving file to: {file_path}")
        
        # Validate EPUB file
        is_valid, error_message = epub_processor.validate_epub(file_path)
        if not is_valid:
            os.remove(file_path)
            logger.warning(f"Invalid EPUB file: {error_message}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_message
            )

        # Create file metadata for source file
        source_file = FileMetadata(
            user_id=current_user.id,
            filename=file.filename,
            file_path=file_path,
            file_size=os.path.getsize(file_path),
            status=FileStatus.UPLOADED
        )
        session.add(source_file)
        await session.flush()
        await session.refresh(source_file)

        # Create translation task
        new_task = TranslationTask(
            user_id=current_user.id,
            file_id=source_file.id,
            source_language=source_language,
            target_language=target_language,
            service=service,
            status=TranslationStatus.PENDING
        )
        session.add(new_task)
        await session.commit()
        await session.refresh(new_task)
        
        logger.info(f"Created translation task with ID: {new_task.id}")
        
        # Start background translation task
        background_tasks.add_task(
            process_translation,
            new_task.id,
            session
        )
        
        return {
            "task_id": new_task.id,
            "status": new_task.status,
            "message": "Translation task started"
        }
        
    except Exception as e:
        logger.error(f"Error starting translation task: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error starting translation task: {str(e)}"
        )

@router.post("/translate/text")
async def translate_text(
    request: TranslationRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Translate text directly."""
    try:
        logger.info(f"Attempting to translate text for user: {current_user.email}")
        logger.debug(f"Translation request: {request}")
        
        response = await translation_service.translate_text(request)
        logger.info("Translation completed successfully")
        
        return {
            "translated_text": response.translated_text,
            "source_language": response.source_language,
            "target_language": response.target_language,
            "confidence": response.confidence
        }
    except Exception as e:
        logger.error(f"Error translating text: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error translating text: {str(e)}"
        )

@router.get("/tasks/{task_id}")
async def get_task_status(
    task_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get translation task status."""
    try:
        logger.info(f"Checking status for task {task_id} by user: {current_user.email}")
        
        # Use SQLAlchemy ORM instead of raw SQL
        stmt = select(TranslationTask).where(
            and_(
                TranslationTask.id == task_id,
                TranslationTask.user_id == current_user.id
            )
        )
        result = await session.execute(stmt)
        task = result.scalar_one_or_none()
        
        if not task:
            logger.warning(f"Task not found: {task_id}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task not found"
            )
        
        return {
            "task_id": task.id,
            "status": task.status.value,  # Convert enum to string value
            "progress": task.progress,
            "error_message": task.error_message if task.status == TranslationStatus.FAILED else None
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting task status: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error getting task status: {str(e)}"
        )

@router.get("/download/{task_id}")
async def download_translated_file(
    task_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Download translated EPUB file."""
    try:
        logger.info(f"Downloading translated file for task {task_id} by user: {current_user.email}")
        
        result = await session.execute(
            text("SELECT * FROM translation_tasks WHERE id = :task_id AND user_id = :user_id"),
            {"task_id": task_id, "user_id": current_user.id}
        )
        task = result.fetchone()
        
        if not task:
            logger.warning(f"Task not found: {task_id}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task not found"
            )

        if task.status != TranslationStatus.COMPLETED:
            logger.warning(f"Translation is not completed. Current status: {task.status}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Translation is not completed. Current status: {task.status}"
            )

        result_file = await session.get(FileMetadata, task.result_file_id)
        if not result_file:
            logger.warning(f"Translated file not found: {task_id}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Translated file not found"
            )

        if not os.path.exists(result_file.file_path):
            logger.warning(f"Translated file not found: {result_file.file_path}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Translated file not found"
            )

        return FileResponse(
            result_file.file_path,
            filename=os.path.basename(result_file.file_path),
            media_type="application/epub+zip"
        )
        
    except Exception as e:
        logger.error(f"Error downloading translated file: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error downloading translated file: {str(e)}"
        )
