from pathlib import Path
from fastapi import UploadFile
import os

ALLOWED_FILE_TYPES = ["application/epub+zip", "text/plain"]
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB 限制

UPLOAD_DIR = Path("uploads")  # 存储文件的目录

# 确保上传目录存在
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


async def save_file(file: UploadFile, file_id: int) -> str:
    file_path = UPLOAD_DIR / f"{file_id}_{file.filename}"
    with open(file_path, "wb") as buffer:
        content = await file.read()
        buffer.write(content)
    return str(file_path)


def validate_file_size(file) -> bool:
    # 使用 file.file 来获取文件对象，并读取文件的大小
    file_size = os.fstat(file.file.fileno()).st_size  # 获取文件大小
    return file_size <= MAX_FILE_SIZE


def validate_file_type(file) -> bool:
    return file.content_type in ALLOWED_FILE_TYPES
