import pytest
from sqlalchemy import select
from app.models.user import User
from app.models.storage import EpubFile
import uuid
from datetime import datetime

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def test_user(db):
    """Create a test user."""
    user = User(
        email="test@example.com",
        hashed_password="hashed_password",
        is_active=True,
        is_verified=True,
        is_superuser=False
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    yield user
    await db.delete(user)
    await db.commit()


@pytest.fixture
async def test_epub_file(db):
    """Create a test epub file entry."""
    epub_file = EpubFile(
        filename="test.epub",
        status="pending"
    )
    db.add(epub_file)
    await db.commit()
    await db.refresh(epub_file)
    yield epub_file
    await db.delete(epub_file)
    await db.commit()


async def test_create_user(db):
    """Test user creation."""
    user = User(
        email="new@example.com",
        hashed_password="hashed_password",
        is_active=True
    )
    db.add(user)
    await db.commit()
    
    stmt = select(User).where(User.email == "new@example.com")
    result = await db.execute(stmt)
    found_user = result.scalar_one()
    
    assert found_user is not None
    assert found_user.email == "new@example.com"
    assert found_user.is_active is True
    
    await db.delete(user)
    await db.commit()


async def test_create_epub_file(db):
    """Test epub file creation."""
    epub_file = EpubFile(
        filename="test2.epub",
        status="pending"
    )
    db.add(epub_file)
    await db.commit()
    
    stmt = select(EpubFile).where(EpubFile.filename == "test2.epub")
    result = await db.execute(stmt)
    found_file = result.scalar_one()
    
    assert found_file is not None
    assert found_file.filename == "test2.epub"
    assert found_file.status == "pending"
    assert found_file.deleted is False
    
    await db.delete(epub_file)
    await db.commit()


async def test_update_user(db, test_user):
    """Test updating user information."""
    test_user.is_superuser = True
    await db.commit()
    
    stmt = select(User).where(User.id == test_user.id)
    result = await db.execute(stmt)
    updated_user = result.scalar_one()
    
    assert updated_user.is_superuser is True


async def test_update_epub_file(db, test_epub_file):
    """Test updating epub file information."""
    test_epub_file.status = "completed"
    await db.commit()
    
    stmt = select(EpubFile).where(EpubFile.id == test_epub_file.id)
    result = await db.execute(stmt)
    updated_file = result.scalar_one()
    
    assert updated_file.status == "completed"


async def test_soft_delete_epub_file(db, test_epub_file):
    """Test soft deletion of epub file."""
    test_epub_file.deleted = True
    await db.commit()
    
    stmt = select(EpubFile).where(EpubFile.id == test_epub_file.id)
    result = await db.execute(stmt)
    deleted_file = result.scalar_one()
    
    assert deleted_file.deleted is True


async def test_unique_constraints(db, test_user):
    """Test unique constraints on models."""
    # Try to create user with same email
    with pytest.raises(Exception):  # SQLAlchemy will raise an integrity error
        duplicate_user = User(
            email=test_user.email,
            hashed_password="different_password",
            is_active=True
        )
        db.add(duplicate_user)
        await db.commit()


async def test_required_fields(db):
    """Test required fields validation."""
    with pytest.raises(Exception):
        invalid_user = User(
            # Missing required email field
            hashed_password="password",
            is_active=True
        )
        db.add(invalid_user)
        await db.commit()
