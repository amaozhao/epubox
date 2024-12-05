"""
Tasks endpoints module.
Contains API endpoints for task management.
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException

from ....schemas.task import TaskCreate, TaskStatus
from ....tasks.manager import TaskManager

router = APIRouter()


@router.post("/", response_model=TaskStatus)
async def create_task(task: TaskCreate):
    """Create a new translation task."""
    pass


@router.get("/{task_id}", response_model=TaskStatus)
async def get_task(task_id: str):
    """Get task status by ID."""
    pass


@router.get("/", response_model=List[TaskStatus])
async def list_tasks():
    """List all tasks."""
    pass
