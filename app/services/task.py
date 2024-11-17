def update_task_status(task_id: int, status: str):
    task = (
        db_session.query(TranslationTask).filter(TranslationTask.id == task_id).first()
    )
    if task:
        task.status = status
        db_session.commit()


def update_chapter_status(task_id: int, chapter_id: int, status: str):
    chapter = (
        db_session.query(Chapter)
        .filter(Chapter.id == chapter_id, Chapter.task_id == task_id)
        .first()
    )
    if chapter:
        chapter.status = status
        db_session.commit()
