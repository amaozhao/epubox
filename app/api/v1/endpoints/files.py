"""
Files endpoints module.
Contains API endpoints for file upload and download.
"""

from typing import List

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import FileResponse

router = APIRouter()


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload an EPUB file for translation."""
    pass


@router.get("/download/{task_id}")
async def download_file(task_id: str):
    """Download translated EPUB file."""
    pass
