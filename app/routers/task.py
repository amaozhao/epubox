@router.get("/tasks/{task_id}/status")
def get_task_status(task_id: int, db: Session = Depends(get_db)):
    task = db.query(TranslationTask).filter(TranslationTask.id == task_id).first()
    return {"task_status": task.status, "progress": task.progress}
