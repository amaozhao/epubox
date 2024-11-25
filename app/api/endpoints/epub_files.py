from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, models, schemas
from app.api import deps
from app.core.epub_utils import remove_epub_file, save_epub_file, validate_epub_file
from app.core.logging import epub_logger

router = APIRouter()


@router.post("/upload/", response_model=schemas.EPUBFile)
async def upload_epub_file(
    *,
    db: deps.DbSession,
    current_user: deps.CurrentUser,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
) -> models.EPUBFile:
    """Upload a new EPUB file."""
    epub_logger.info(
        "epub_upload_started",
        user_id=current_user.id,
        filename=file.filename,
        content_type=file.content_type,
    )

    # Validate file
    await validate_epub_file(file)

    # Save file to disk in background
    filename, file_path, file_size = await save_epub_file(file, current_user.id)
    background_tasks.add_task(process_epub_metadata, file_path)

    # Create database record
    epub_obj = schemas.EPUBFileCreate(
        filename=filename,
        file_size=file_size,
        original_filename=file.filename,
    )

    db_file = await crud.epub_file.create_with_owner(
        db=db,
        obj_in=epub_obj,
        owner_id=current_user.id,
        file_path=file_path,
    )

    epub_logger.info(
        "epub_upload_success",
        user_id=current_user.id,
        file_id=db_file.id,
        filename=filename,
        file_size=file_size,
    )

    return db_file


@router.get("/", response_model=List[schemas.EPUBFile])
async def list_epub_files(
    db: deps.DbSession,
    current_user: deps.CurrentUser,
    skip: int = 0,
    limit: int = 100,
) -> List[models.EPUBFile]:
    """List all EPUB files owned by the current user."""
    epub_logger.info(
        "epub_list_started",
        user_id=current_user.id,
        skip=skip,
        limit=limit,
    )

    files = await crud.epub_file.get_multi_by_owner(
        db=db,
        owner_id=current_user.id,
        skip=skip,
        limit=limit,
    )

    epub_logger.info(
        "epub_list_success",
        user_id=current_user.id,
        count=len(files),
    )

    return files


@router.get("/{file_id}", response_model=schemas.EPUBFile)
async def get_epub_file(
    *,
    file_id: int,
    db: deps.DbSession,
    current_user: deps.CurrentUser,
) -> models.EPUBFile:
    """Get a specific EPUB file by ID."""
    epub_logger.info(
        "epub_get_started",
        user_id=current_user.id,
        file_id=file_id,
    )

    file = await crud.epub_file.get(db=db, id=file_id)
    if not file:
        epub_logger.warning(
            "epub_get_not_found",
            user_id=current_user.id,
            file_id=file_id,
        )
        raise HTTPException(status_code=404, detail="File not found")
    if file.user_id != current_user.id:
        epub_logger.warning(
            "epub_get_unauthorized",
            user_id=current_user.id,
            file_id=file_id,
            owner_id=file.user_id,
        )
        raise HTTPException(status_code=403, detail="Not enough permissions")

    epub_logger.info(
        "epub_get_success",
        user_id=current_user.id,
        file_id=file_id,
    )

    return file


@router.get("/{file_id}/download")
async def download_epub_file(
    *,
    file_id: int,
    db: deps.DbSession,
    current_user: deps.CurrentUser,
):
    """Download an EPUB file."""
    epub_logger.info(
        "epub_download_started",
        user_id=current_user.id,
        file_id=file_id,
    )

    file = await crud.epub_file.get(db=db, id=file_id)
    if not file:
        epub_logger.warning(
            "epub_download_not_found",
            user_id=current_user.id,
            file_id=file_id,
        )
        raise HTTPException(status_code=404, detail="File not found")
    if file.user_id != current_user.id:
        epub_logger.warning(
            "epub_download_unauthorized",
            user_id=current_user.id,
            file_id=file_id,
            owner_id=file.user_id,
        )
        raise HTTPException(status_code=403, detail="Not enough permissions")

    epub_logger.info(
        "epub_download_success",
        user_id=current_user.id,
        file_id=file_id,
    )

    return StreamingResponse(
        file.file_path,
        media_type="application/epub+zip",
        headers={"Content-Disposition": f"attachment; filename={file.filename}"},
    )


@router.delete("/{file_id}", response_model=schemas.EPUBFile)
async def delete_epub_file(
    *,
    file_id: int,
    db: deps.DbSession,
    current_user: deps.CurrentUser,
) -> models.EPUBFile:
    """Delete an EPUB file."""
    epub_logger.info(
        "epub_delete_started",
        user_id=current_user.id,
        file_id=file_id,
    )

    file = await crud.epub_file.get(db=db, id=file_id)
    if not file:
        epub_logger.warning(
            "epub_delete_not_found",
            user_id=current_user.id,
            file_id=file_id,
        )
        raise HTTPException(status_code=404, detail="File not found")
    if file.user_id != current_user.id:
        epub_logger.warning(
            "epub_delete_unauthorized",
            user_id=current_user.id,
            file_id=file_id,
            owner_id=file.user_id,
        )
        raise HTTPException(status_code=403, detail="Not enough permissions")

    # Remove file from filesystem
    if not await remove_epub_file(file.file_path):
        epub_logger.error(
            "epub_delete_failed",
            user_id=current_user.id,
            file_id=file_id,
            reason="filesystem_error",
        )
        raise HTTPException(status_code=500, detail="Error removing file from disk")

    file = await crud.epub_file.remove(db=db, id=file_id)

    epub_logger.info(
        "epub_delete_success",
        user_id=current_user.id,
        file_id=file_id,
    )

    return file


async def process_epub_metadata(file_path: str):
    """Process EPUB metadata in the background."""
    try:
        # Add metadata extraction logic here
        pass
    except Exception as e:
        epub_logger.error(
            "epub_metadata_processing_failed", error=str(e), file_path=file_path
        )
