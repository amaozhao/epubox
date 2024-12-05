# 进度管理模块设计

## 1. 概述

进度管理模块专注于翻译性能分析，作为任务管理的补充组件。

## 2. 数据库设计

```sql
-- 翻译性能记录表
CREATE TABLE IF NOT EXISTS translation_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,              -- 任务ID
    words_count INTEGER NOT NULL,        -- 此次翻译的字数
    time_spent INTEGER NOT NULL,         -- 耗时（秒）
    recorded_at TIMESTAMP NOT NULL,      -- 记录时间
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_metrics_task ON translation_metrics(task_id);
CREATE INDEX IF NOT EXISTS idx_metrics_time ON translation_metrics(recorded_at);
```

## 3. 性能分析设计

```python
from datetime import datetime, timedelta
from typing import Dict, List, Optional

class PerformanceAnalyzer:
    """翻译性能分析"""
    
    def __init__(self, task_manager):
        self.task_manager = task_manager

    async def record_metrics(
        self,
        task_id: str,
        words_count: int,
        time_spent: int
    ) -> None:
        """
        记录翻译性能指标
        
        Args:
            task_id: 任务ID
            words_count: 此次翻译的字数
            time_spent: 耗时（秒）
        """
        async with self.task_manager.db.connection() as db:
            await db.execute("""
                INSERT INTO translation_metrics (
                    task_id, words_count, time_spent, recorded_at
                ) VALUES (?, ?, ?, ?)
            """, (task_id, words_count, time_spent, datetime.now()))

    async def get_translation_speed(self, task_id: str, window: str = '1h') -> float:
        """
        计算翻译速度（字/分钟）
        
        Args:
            task_id: 任务ID
            window: 时间窗口（1h, 24h, 7d, all）
        """
        async with self.task_manager.db.connection() as db:
            start_time = datetime.now()
            if window == '1h':
                start_time -= timedelta(hours=1)
            elif window == '24h':
                start_time -= timedelta(days=1)
            elif window == '7d':
                start_time -= timedelta(days=7)
            
            rows = await db.fetch("""
                SELECT SUM(words_count) as total_words,
                       SUM(time_spent) as total_time
                FROM translation_metrics
                WHERE task_id = ? AND recorded_at >= ?
            """, task_id, start_time)
            
            if not rows or not rows[0]['total_time']:
                return 0.0
                
            return rows[0]['total_words'] / (rows[0]['total_time'] / 60)

    async def estimate_completion_time(self, task_id: str) -> Optional[datetime]:
        """
        预估完成时间
        """
        task_info = await self.task_manager.get_task_info(task_id)
        speed = await self.get_translation_speed(task_id, window='1h')
        
        if speed <= 0:
            # 如果最近一小时没有数据，使用24小时的平均速度
            speed = await self.get_translation_speed(task_id, window='24h')
            if speed <= 0:
                # 如果24小时内没有数据，使用所有历史数据
                speed = await self.get_translation_speed(task_id, window='all')
                if speed <= 0:
                    return None
        
        remaining_words = task_info['total_words'] - task_info['translated_words']
        estimated_minutes = remaining_words / speed
        
        return datetime.now() + timedelta(minutes=estimated_minutes)

    async def get_performance_trends(self, task_id: str) -> dict:
        """
        获取性能趋势分析
        """
        async with self.task_manager.db.connection() as db:
            # 按小时统计
            hourly = await db.fetch("""
                SELECT 
                    strftime('%Y-%m-%d %H:00:00', recorded_at) as hour,
                    AVG(words_count * 60.0 / time_spent) as avg_speed,
                    SUM(words_count) as total_words
                FROM translation_metrics
                WHERE task_id = ?
                GROUP BY strftime('%Y-%m-%d %H', recorded_at)
                ORDER BY hour DESC
                LIMIT 24
            """, task_id)
            
            # 按天统计
            daily = await db.fetch("""
                SELECT 
                    DATE(recorded_at) as date,
                    AVG(words_count * 60.0 / time_spent) as avg_speed,
                    SUM(words_count) as total_words,
                    COUNT(*) as sessions
                FROM translation_metrics
                WHERE task_id = ?
                GROUP BY DATE(recorded_at)
                ORDER BY date DESC
                LIMIT 7
            """, task_id)
            
        return {
            'hourly_trends': [
                {
                    'hour': row['hour'],
                    'speed': row['avg_speed'],
                    'words': row['total_words']
                }
                for row in hourly
            ],
            'daily_trends': [
                {
                    'date': row['date'],
                    'speed': row['avg_speed'],
                    'words': row['total_words'],
                    'sessions': row['sessions']
                }
                for row in daily
            ]
        }
```

## 4. 使用示例

```python
# 初始化性能分析器
analyzer = PerformanceAnalyzer(task_manager)

# 记录翻译性能
await analyzer.record_metrics(
    task_id='task-123',
    words_count=500,  # 此次翻译500字
    time_spent=300    # 耗时300秒
)

# 获取不同时间窗口的翻译速度
speed_1h = await analyzer.get_translation_speed('task-123', window='1h')
speed_24h = await analyzer.get_translation_speed('task-123', window='24h')
print(f"最近1小时速度: {speed_1h:.1f} 字/分钟")
print(f"最近24小时速度: {speed_24h:.1f} 字/分钟")

# 预估完成时间
completion_time = await analyzer.estimate_completion_time('task-123')
if completion_time:
    print(f"预计完成时间: {completion_time}")

# 查看性能趋势
trends = await analyzer.get_performance_trends('task-123')
print("\n每小时趋势:")
for trend in trends['hourly_trends']:
    print(
        f"{trend['hour']}: "
        f"速度 {trend['speed']:.1f} 字/分钟, "
        f"完成 {trend['words']} 字"
    )

print("\n每日趋势:")
for trend in trends['daily_trends']:
    print(
        f"{trend['date']}: "
        f"速度 {trend['speed']:.1f} 字/分钟, "
        f"完成 {trend['words']} 字, "
        f"工作 {trend['sessions']} 次"
    )
```

## 5. 特性

1. **性能记录**
   - 记录每次翻译的字数和耗时
   - 专注于性能数据，避免重复
   - 支持细粒度分析

2. **速度分析**
   - 多时间窗口的速度计算
   - 实时性能监控
   - 趋势分析支持

3. **预估功能**
   - 智能时间窗口选择
   - 基于历史性能预测
   - 动态更新预估

4. **数据优化**
   - 轻量级设计
   - 高效索引支持
   - 避免数据冗余

5. **实用特性**
   - 小时级统计
   - 每日性能报告
   - 工作量分析
