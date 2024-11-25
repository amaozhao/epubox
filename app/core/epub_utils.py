import io
import os
import uuid
import zipfile
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import HTTPException, UploadFile

from app.core.config import settings


async def validate_epub_file(file: UploadFile) -> bool:
    """Validate if the uploaded file is a valid EPUB."""
    # Check content type
    if not file.content_type == "application/epub+zip":
        raise HTTPException(
            status_code=400, detail="Invalid file type: must be application/epub+zip"
        )

    # Check file size
    content = await file.read()
    if len(content) > settings.MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=400, detail="File too large")

    # Reset file position after reading
    await file.seek(0)

    # Check if it's a valid ZIP file with EPUB structure
    try:
        zip_file = io.BytesIO(content)
        with zipfile.ZipFile(zip_file) as zf:
            # Check for required EPUB files
            file_list = zf.namelist()

            # EPUB requirements:
            # 1. Must contain mimetype file
            # 2. Must contain META-INF/container.xml
            # 3. mimetype should contain "application/epub+zip"

            if "mimetype" not in file_list:
                raise HTTPException(
                    status_code=400, detail="Invalid EPUB format: missing mimetype file"
                )

            if "META-INF/container.xml" not in file_list:
                raise HTTPException(
                    status_code=400, detail="Invalid EPUB format: missing container.xml"
                )

            # Check mimetype content
            try:
                mimetype_content = zf.read("mimetype").decode("utf-8").strip()
                if mimetype_content != "application/epub+zip":
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid EPUB format: incorrect mimetype",
                    )
            except Exception:
                raise HTTPException(
                    status_code=400, detail="Invalid EPUB format: cannot read mimetype"
                )

    except zipfile.BadZipFile:
        raise HTTPException(
            status_code=400, detail="Invalid EPUB format: not a valid ZIP file"
        )
    finally:
        await file.seek(0)

    return True


def sanitize_filename(filename: str) -> str:
    """Sanitize the filename to prevent directory traversal attacks."""
    return Path(filename).name


async def save_epub_file(file: UploadFile, user_id: int) -> tuple[str, str, int]:
    """
    Save an EPUB file to the uploads directory.
    Returns: (filename, file_path, file_size)
    """
    # Create unique filename
    ext = Path(file.filename).suffix
    if ext.lower() != ".epub":
        raise HTTPException(status_code=400, detail="File must have .epub extension")

    unique_filename = f"{uuid.uuid4()}{ext}"

    # Create user-specific upload directory
    user_upload_dir = Path(settings.UPLOAD_DIR) / str(user_id)
    user_upload_dir.mkdir(parents=True, exist_ok=True)

    file_path = user_upload_dir / unique_filename

    # Save file
    async with aiofiles.open(file_path, "wb") as f:
        content = await file.read()
        await f.write(content)

    return unique_filename, str(file_path), len(content)


async def remove_epub_file(file_path: str) -> bool:
    """Remove an EPUB file from the filesystem."""
    try:
        os.remove(file_path)
        return True
    except (FileNotFoundError, PermissionError):
        return False
