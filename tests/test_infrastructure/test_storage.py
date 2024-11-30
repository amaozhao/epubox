import os
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.infrastructure.database import Base
from src.infrastructure.storage import StorageService
from src.models.storage import Storage, StorageStatus
from src.models.users import User
from src.utils.errors import FileError

# 测试数据库URL
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def engine():
    """创建测试数据库引擎"""
    engine = create_async_engine(TEST_DATABASE_URL, echo=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session(engine):
    """创建测试会话"""
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        yield session


@pytest.fixture
async def test_user(session):
    """创建测试用户"""
    user = User(
        id="test-user-id",
        username="testuser",
        email="test@example.com",
        password_hash="dummy_hash",
    )
    session.add(user)
    await session.commit()
    return user


@pytest.fixture
def storage_service(tmp_path, session):
    """创建测试用的存储服务实例"""
    upload_dir = tmp_path / "uploads"
    translation_dir = tmp_path / "translations"
    return StorageService(
        session=session,
        upload_dir=str(upload_dir),
        translation_dir=str(translation_dir),
    )


@pytest.fixture
def sample_file():
    """创建示例文件内容"""
    return b"Sample file content"


@pytest.fixture
def sample_epub():
    """创建示例EPUB文件"""
    buffer = BytesIO()
    with ZipFile(buffer, "w") as zip_file:
        # 添加mimetype文件
        zip_file.writestr("mimetype", "application/epub+zip")
        # 添加container.xml
        zip_file.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0"?>
            <container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
                <rootfiles>
                    <rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>
                </rootfiles>
            </container>""",
        )
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_save_upload(storage_service, sample_file, test_user):
    """测试文件上传"""
    file = BytesIO(sample_file)
    file_id = await storage_service.save_upload(file, "test.txt", test_user.id)

    assert file_id is not None
    assert len(file_id) > 0

    # 验证文件是否正确保存
    content = await storage_service.get_file(file_id, "upload")
    assert content == sample_file

    # 验证数据库记录
    storage = await storage_service.session.get(Storage, file_id)
    assert storage is not None
    assert storage.original_filename == "test.txt"
    assert storage.file_size == len(sample_file)
    assert storage.status == StorageStatus.UPLOADED
    assert storage.user_id == test_user.id


@pytest.mark.asyncio
async def test_create_work_copy(storage_service, sample_file, test_user):
    """测试创建工作副本"""
    # 先上传文件
    file = BytesIO(sample_file)
    file_id = await storage_service.save_upload(file, "test.txt", test_user.id)

    # 创建工作副本
    work_copy_id = await storage_service.create_work_copy(file_id)

    assert work_copy_id is not None
    assert work_copy_id != file_id

    # 验证工作副本内容
    content = await storage_service.get_file(file_id, "translation")
    assert content == sample_file

    # 验证数据库记录
    storage = await storage_service.session.get(Storage, file_id)
    assert storage.status == StorageStatus.PROCESSING
    assert storage.translation_path is not None


@pytest.mark.asyncio
async def test_get_file_not_found(storage_service):
    """测试获取不存在的文件"""
    with pytest.raises(FileError):
        await storage_service.get_file("nonexistent")


@pytest.mark.asyncio
async def test_delete_file(storage_service, sample_file, test_user):
    """测试删除文件"""
    # 先上传文件
    file = BytesIO(sample_file)
    file_id = await storage_service.save_upload(file, "test.txt", test_user.id)

    # 删除文件
    await storage_service.delete_file(file_id, "upload")

    # 验证文件已被删除
    with pytest.raises(FileError):
        await storage_service.get_file(file_id, "upload")

    # 验证数据库记录
    storage = await storage_service.session.get(Storage, file_id)
    assert storage.status == StorageStatus.DELETED


@pytest.mark.asyncio
async def test_verify_epub_valid(storage_service, sample_epub, test_user):
    """测试验证有效的EPUB文件"""
    # 上传EPUB文件
    file = BytesIO(sample_epub)
    file_id = await storage_service.save_upload(file, "test.epub", test_user.id)

    # 验证EPUB
    is_valid = await storage_service.verify_epub(file_id)
    assert is_valid is True


@pytest.mark.asyncio
async def test_verify_epub_invalid(storage_service, sample_file, test_user):
    """测试验证无效的EPUB文件"""
    # 上传非EPUB文件
    file = BytesIO(sample_file)
    file_id = await storage_service.save_upload(file, "test.txt", test_user.id)

    # 验证EPUB
    is_valid = await storage_service.verify_epub(file_id)
    assert is_valid is False


@pytest.mark.asyncio
async def test_cleanup_expired_files(storage_service, sample_file, test_user):
    """测试清理过期文件"""
    # 上传文件并创建工作副本
    file = BytesIO(sample_file)
    file_id = await storage_service.save_upload(file, "test.txt", test_user.id)
    work_copy_id = await storage_service.create_work_copy(file_id)

    # 修改文件记录的创建时间
    storage = await storage_service.session.get(Storage, file_id)
    storage.created_at = datetime.utcnow() - timedelta(hours=25)
    await storage_service.session.commit()

    # 清理过期文件
    await storage_service.cleanup_expired_files(max_age_hours=24)

    # 验证存储记录已被标记为删除
    storage = await storage_service.session.get(Storage, file_id)
    assert storage.status == StorageStatus.DELETED

    # 验证物理文件已被删除
    with pytest.raises(FileError):
        await storage_service.get_file(file_id)
