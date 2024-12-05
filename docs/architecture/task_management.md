# 任务管理模块设计文档

## 1. 概述

任务管理模块负责管理EPUB翻译任务的生命周期和进度跟踪。

## 2. 数据库设计

### 2.1 表结构

```sql
-- 任务表
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,           -- 任务ID
    epub_path TEXT NOT NULL,       -- EPUB文件路径
    source_language TEXT,          -- 源语言（可选，为空时自动检测）
    target_language TEXT NOT NULL, -- 目标语言
    priority INTEGER DEFAULT 0,    -- 任务优先级（0-9，越大优先级越高）
    status TEXT NOT NULL,          -- 任务状态: created, running, paused, completed, failed, cancelled
    current_chapter INTEGER DEFAULT 0,  -- 当前处理的章节索引
    total_chapters INTEGER DEFAULT 0,   -- 总章节数
    percentage INTEGER DEFAULT 0,       -- 完成百分比
    error TEXT,                        -- 错误信息（如果失败）
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- 创建时间
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP   -- 更新时间
);

-- 已完成章节表
CREATE TABLE IF NOT EXISTS completed_chapters (
    task_id TEXT NOT NULL,         -- 任务ID
    chapter_index INTEGER NOT NULL, -- 章节索引
    chapter_name TEXT,             -- 章节名称
    completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- 完成时间
    PRIMARY KEY (task_id, chapter_index),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks(status, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority DESC);
CREATE INDEX IF NOT EXISTS idx_completed_chapters_task ON completed_chapters(task_id);
```

### 2.2 任务管理器设计

```python
from typing import Optional, List, Dict
from datetime import datetime
import uuid
import asyncio
import sqlite3
import aiosqlite

class TaskManager:
    """任务管理器"""
    
    def __init__(self, db_path: str = ".epubox/tasks.db"):
        """
        初始化任务管理器
        
        Args:
            db_path: SQLite数据库路径
        """
        self.db_path = db_path
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._init_db()
        
    def _init_db(self):
        """初始化数据库表"""
        with sqlite3.connect(self.db_path) as conn:
            with open('schema.sql') as f:
                conn.executescript(f.read())
                
    async def create_task(
        self, 
        epub_path: str, 
        target_language: str,
        source_language: Optional[str] = None,
        priority: int = 0
    ) -> str:
        """
        创建翻译任务
        
        Args:
            epub_path: EPUB文件路径
            target_language: 目标语言代码
            source_language: 源语言代码（可选）
            priority: 任务优先级（0-9）
            
        Returns:
            str: 任务ID
        """
        task_id = str(uuid.uuid4())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO tasks (
                    id, epub_path, source_language, target_language, 
                    priority, status
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (task_id, epub_path, source_language, target_language, priority, 'created'))
            await db.commit()
        return task_id
        
    async def start_task(self, task_id: str) -> None:
        """
        启动任务
        
        Args:
            task_id: 任务ID
        """
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
            row = await cursor.fetchone()
            if not row:
                raise ValueError(f"Task {task_id} not found")
                
            if task_id in self._running_tasks:
                raise ValueError(f"Task {task_id} is already running")
                
            await db.execute("""
                UPDATE tasks 
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, ('running', task_id))
            await db.commit()
            
        self._running_tasks[task_id] = asyncio.create_task(
            self._process_task(task_id)
        )
        
    async def _process_task(self, task_id: str) -> None:
        """处理任务"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                while True:
                    cursor = await db.execute("""
                        SELECT status, current_chapter, total_chapters 
                        FROM tasks 
                        WHERE id = ?
                    """, (task_id,))
                    row = await cursor.fetchone()
                    if not row:
                        return
                        
                    status, current_chapter, total_chapters = row
                    if status == 'paused':
                        return
                        
                    if current_chapter >= total_chapters:
                        await db.execute("""
                            UPDATE tasks 
                            SET status = ?, percentage = 100, 
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, ('completed', task_id))
                        await db.commit()
                        return
                        
                    # TODO: 翻译当前章节
                    chapter_name = f"Chapter {current_chapter + 1}"  # 实际应从EPUB中获取
                    
                    # 记录已完成章节
                    await db.execute("""
                        INSERT INTO completed_chapters (task_id, chapter_index, chapter_name)
                        VALUES (?, ?, ?)
                    """, (task_id, current_chapter, chapter_name))
                    
                    # 更新任务进度
                    await db.execute("""
                        UPDATE tasks 
                        SET current_chapter = current_chapter + 1,
                            percentage = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (int((current_chapter + 1) / max(1, total_chapters) * 100), task_id))
                    
                    await db.commit()
                    await asyncio.sleep(0.1)
                    
        except Exception as e:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    UPDATE tasks 
                    SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, ('failed', str(e), task_id))
                await db.commit()
                
        finally:
            if task_id in self._running_tasks:
                del self._running_tasks[task_id]
                
    async def pause_task(self, task_id: str) -> None:
        """暂停任务"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT status FROM tasks WHERE id = ?", 
                (task_id,)
            )
            row = await cursor.fetchone()
            if not row:
                raise ValueError(f"Task {task_id} not found")
                
            if row[0] != 'running':
                raise ValueError(f"Task {task_id} is not running")
                
            await db.execute("""
                UPDATE tasks 
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, ('paused', task_id))
            await db.commit()
            
    async def resume_task(self, task_id: str) -> None:
        """恢复任务"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT status FROM tasks WHERE id = ?", 
                (task_id,)
            )
            row = await cursor.fetchone()
            if not row:
                raise ValueError(f"Task {task_id} not found")
                
            if row[0] != 'paused':
                raise ValueError(f"Task {task_id} is not paused")
                
            await self.start_task(task_id)
            
    async def get_task_info(self, task_id: str) -> dict:
        """获取任务信息"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                SELECT t.*, COUNT(c.chapter_index) as completed_count,
                       GROUP_CONCAT(c.chapter_name, '|') as chapter_names
                FROM tasks t
                LEFT JOIN completed_chapters c ON t.id = c.task_id
                WHERE t.id = ?
                GROUP BY t.id
            """, (task_id,))
            row = await cursor.fetchone()
            if not row:
                raise ValueError(f"Task {task_id} not found")
                
            chapter_names = row[12].split('|') if row[12] else []
            return {
                'id': row[0],
                'epub_path': row[1],
                'source_language': row[2],
                'target_language': row[3],
                'priority': row[4],
                'status': row[5],
                'current_chapter': row[6],
                'total_chapters': row[7],
                'percentage': row[8],
                'error': row[9],
                'created_at': row[10],
                'updated_at': row[11],
                'completed_chapters': row[12],  # completed_count
                'chapter_names': chapter_names
            }
            
    async def list_tasks(
        self, 
        status: Optional[str] = None,
        priority: Optional[int] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[dict]:
        """
        获取任务列表
        
        Args:
            status: 可选的状态过滤
            priority: 可选的优先级过滤
            limit: 返回结果数量限制
            offset: 分页偏移量
        """
        async with aiosqlite.connect(self.db_path) as db:
            query = """
                SELECT t.*, COUNT(c.chapter_index) as completed_count
                FROM tasks t
                LEFT JOIN completed_chapters c ON t.id = c.task_id
                WHERE 1=1
            """
            params = []
            
            if status:
                query += " AND t.status = ?"
                params.append(status)
                
            if priority is not None:
                query += " AND t.priority = ?"
                params.append(priority)
                
            query += """
                GROUP BY t.id
                ORDER BY t.priority DESC, t.created_at DESC
                LIMIT ? OFFSET ?
            """
            params.extend([limit, offset])
            
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
            
            return [{
                'id': row[0],
                'epub_path': row[1],
                'source_language': row[2],
                'target_language': row[3],
                'priority': row[4],
                'status': row[5],
                'current_chapter': row[6],
                'total_chapters': row[7],
                'percentage': row[8],
                'error': row[9],
                'created_at': row[10],
                'updated_at': row[11],
                'completed_chapters': row[12]
            } for row in rows]

### 2.3 任务状态

- created: 已创建
- running: 运行中
- paused: 已暂停
- completed: 已完成
- failed: 失败
- cancelled: 已取消

### 2.4 使用示例

```python
# 创建任务管理器
task_manager = TaskManager()

# 创建任务
task_id = await task_manager.create_task(
    epub_path="/path/to/book.epub",
    target_language="zh",
    source_language="en",  # 可选
    priority=5  # 可选，0-9
)

# 启动任务
await task_manager.start_task(task_id)

# 暂停任务
await task_manager.pause_task(task_id)

# 恢复任务
await task_manager.resume_task(task_id)

# 获取任务信息
task_info = await task_manager.get_task_info(task_id)
print(f"Progress: {task_info['percentage']}%")
print(f"Completed chapters: {task_info['completed_chapters']}/{task_info['total_chapters']}")
print(f"Chapter names: {', '.join(task_info['chapter_names'])}")

# 获取高优先级的运行中任务
running_tasks = await task_manager.list_tasks(
    status='running',
    priority=5,
    limit=10,
    offset=0
)
