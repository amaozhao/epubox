from fastapi import APIRouter, File, UploadFile, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.core.database import get_async_session
from app.models.storage import EpubFile
from app.utils.save import save_file
from app.storages.upload import epub_upload

from fastapi.responses import FileResponse


router = APIRouter(prefix="/storages")


@router.post("/upload")
async def upload(
    file: UploadFile = File(...), db: AsyncSession = Depends(get_async_session)
):
    # 创建文件记录
    epub_file = EpubFile(filename=file.filename)
    db.add(epub_file)
    await db.commit()
    await db.refresh(epub_file)

    # 保存文件
    await epub_upload(file, epub_file.id)

    return {
        "epub": {
            "id": epub_file.id,
            "filename": file.filename,
            "status": epub_file.status,
        },
        "code": 200,
    }


@router.get("/download/{epub_id}")
async def download_file(epub_id: int, db: AsyncSession = Depends(get_async_session)):
    # 查询文件记录
    query = select(EpubFile).filter(EpubFile.id == epub_id)
    result = await db.execute(query)
    db_file = result.scalars().first()

    if db_file is None:
        raise HTTPException(status_code=404, detail="File not found")

    file_path = f"uploads/{epub_id}_{db_file.filename}"
    return FileResponse(
        file_path, media_type="application/octet-stream", filename=db_file.filename
    )
