import pytest
import asyncio
import os
from pathlib import Path
from datetime import datetime, timedelta
from app.utils.cleanup import FileCleanup

@pytest.mark.asyncio
async def test_cleanup_old_files(tmp_path):
    """Test the file cleanup utility."""
    # Create test files with different timestamps
    old_file = tmp_path / "old.epub"
    new_file = tmp_path / "new.epub"
    
    # Create files
    old_file.write_text("old content")
    new_file.write_text("new content")
    
    # Set old file's modification time to 8 days ago
    old_time = datetime.now() - timedelta(days=8)
    old_timestamp = old_time.timestamp()
    os.utime(old_file, (old_timestamp, old_timestamp))
    
    # Run cleanup
    cleanup = FileCleanup(str(tmp_path), max_age_hours=24)
    await cleanup.cleanup_old_files()
    
    # Check results
    assert not old_file.exists()  # Old file should be deleted
    assert new_file.exists()      # New file should remain

@pytest.mark.asyncio
async def test_cleanup_empty_directory(tmp_path):
    """Test cleanup on empty directory."""
    # Run cleanup on empty directory
    cleanup = FileCleanup(str(tmp_path))
    await cleanup.cleanup_old_files()
    assert tmp_path.exists()  # Directory should still exist

@pytest.mark.asyncio
async def test_cleanup_nonexistent_directory(tmp_path):
    """Test cleanup on non-existent directory."""
    nonexistent_dir = tmp_path / "nonexistent"
    # Should not raise an exception
    cleanup = FileCleanup(str(nonexistent_dir))
    await cleanup.cleanup_old_files()

# Add more utility tests as needed based on your utils/ directory content
