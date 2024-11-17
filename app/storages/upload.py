from app.core.errors import FileUploadError, FileSizeError, FileTypeError
from app.utils.save import validate_file_size, validate_file_type, save_file


async def epub_upload(file, file_id):
    # 文件大小检查
    if not validate_file_size(file):
        raise FileSizeError("File size exceeds the limit.")

    # 文件类型检查
    if not validate_file_type(file):
        raise FileTypeError(f"Unsupported file type: {file.content_type}")

    try:
        # 执行文件保存的逻辑
        await save_file(file, file_id)
    except Exception as e:
        raise FileUploadError(f"Failed to upload file: {str(e)}")
