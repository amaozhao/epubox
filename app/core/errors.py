from fastapi.responses import JSONResponse
from starlette.requests import Request


class FileUploadError(Exception):
    def __init__(self, detail: str):
        self.detail = detail


class FileSizeError(Exception):
    def __init__(self, detail: str):
        self.detail = detail


class FileTypeError(Exception):
    def __init__(self, detail: str):
        self.detail = detail


# 全局异常处理器
async def file_upload_error_handler(
    request: Request, exc: FileUploadError
) -> JSONResponse:
    return JSONResponse(
        status_code=400, content={"message": f"File upload error: {exc.detail}"}
    )


async def file_size_error_handler(request: Request, exc: FileSizeError) -> JSONResponse:
    return JSONResponse(
        status_code=413, content={"message": f"File size error: {exc.detail}"}
    )


async def file_type_error_handler(request: Request, exc: FileTypeError) -> JSONResponse:
    return JSONResponse(
        status_code=415, content={"message": f"Unsupported file type: {exc.detail}"}
    )


# 统一的错误处理器，可以处理其他错误
async def general_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500, content={"message": f"Internal server error: {str(exc)}"}
    )
