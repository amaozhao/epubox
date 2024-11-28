"""Translation API endpoints."""

from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud
from app.api import deps
from app.models.user import User
from app.schemas.translation import (
    TranslationRequest,
    TranslationResponse,
    TranslationStatus,
)
from app.services.translation import TranslationProvider
from app.services.translation.factory import create_translator
from app.services.translation.queue_manager import queue_manager

router = APIRouter()


@router.get("/languages")
def get_supported_languages(
    current_user: User = Depends(deps.get_current_user),
) -> Dict[str, List[Dict[str, str]]]:
    """Get list of supported languages for translation."""
    # Create a mock translator to get supported languages
    translator = create_translator(
        TranslationProvider.MOCK,
        "",  # No API key needed for mock
        "en",  # Source language doesn't matter
        "es",  # Target language doesn't matter
    )
    return translator.get_supported_languages()


@router.post("/translate", response_model=TranslationResponse)
async def translate_file(
    *,
    request: TranslationRequest,
    db: AsyncSession = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
) -> TranslationResponse:
    """Submit a file for translation."""
    # First check if file exists
    file = await crud.epub_file.get(db=db, id=request.file_id)
    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    # Then check ownership
    if not crud.epub_file.is_owner(file, current_user.id):
        raise HTTPException(status_code=403, detail="Not enough permissions")

    try:
        task_id = await queue_manager.submit_translation(
            db=db,
            file_id=request.file_id,
            source_lang=request.source_lang,
            target_lang=request.target_lang,
            user_id=current_user.id,
            provider=request.provider,
        )

        return TranslationResponse(
            task_id=task_id,
            message="Translation task submitted successfully",
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status/{task_id}", response_model=TranslationStatus)
async def get_translation_status(
    task_id: str,
    current_user: User = Depends(deps.get_current_user),
) -> TranslationStatus:
    """Get the status of a translation task."""
    # First check if task exists
    status = await queue_manager.get_status(task_id)
    if not status:
        raise HTTPException(status_code=404, detail="Translation task not found")

    # Then verify ownership
    if not task_id.startswith(f"{current_user.id}_"):
        raise HTTPException(
            status_code=403,
            detail="Not authorized to access this translation task",
        )

    return status


@router.delete("/cancel/{task_id}", response_model=TranslationResponse)
async def cancel_translation(
    task_id: str,
    current_user: User = Depends(deps.get_current_user),
) -> TranslationResponse:
    """Cancel a translation task."""
    # First check if task exists
    status = await queue_manager.get_status(task_id)
    if not status:
        raise HTTPException(
            status_code=404,
            detail="Translation task not found or already completed",
        )

    # Then verify ownership
    if not task_id.startswith(f"{current_user.id}_"):
        raise HTTPException(
            status_code=403,
            detail="Not authorized to cancel this translation task",
        )

    # Try to cancel the task
    cancelled = await queue_manager.cancel_translation(task_id)
    if not cancelled:
        # Task might have completed between our check and cancel attempt
        status = await queue_manager.get_status(task_id)
        if status and status.status in ["completed", "failed", "cancelled"]:
            return TranslationResponse(
                task_id=task_id,
                message=f"Translation task already {status.status}",
            )
        raise HTTPException(
            status_code=404,
            detail="Translation task not found or already completed",
        )

    return TranslationResponse(
        task_id=task_id,
        message="Translation task cancelled successfully",
    )
