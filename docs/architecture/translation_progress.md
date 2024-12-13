# EPUB 翻译进度管理设计

## 1. 功能概述

为 EPUB 翻译过程添加进度管理功能，支持：
- 记录每个章节的翻译状态
- 支持中断后继续翻译
- 提供翻译进度查询和更新
- 计算整体翻译进度百分比

## 2. 数据模型设计

### 2.1 TranslationProgress 模型

```python
class TranslationProgress(Base):
    """Translation progress tracking."""
    
    __tablename__ = "translation_progress"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[str] = mapped_column(String, nullable=False)
    total_chapters: Mapped[Dict] = mapped_column(JSON, nullable=False)
    completed_chapters: Mapped[Dict] = mapped_column(JSON, nullable=False, default_factory=dict)
    status: Mapped[TranslationStatus] = mapped_column(Enum(TranslationStatus), nullable=False)
    started_at: Mapped[datetime] = mapped_column(nullable=True)
    completed_at: Mapped[datetime] = mapped_column(nullable=True)
```

### 2.2 章节信息结构

total_chapters 和 completed_chapters 的数据结构：
```python
{
    "chapter_id": {
        "id": "chapter_id",        # 章节ID
        "type": "chapter",         # 类型
        "name": "chapter1.xhtml"   # 章节文件名
    }
}
```

### 2.3 翻译状态枚举

```python
class TranslationStatus(str, Enum):
    """Translation status enum."""
    
    PENDING = "pending"         # 等待翻译
    PROCESSING = "processing"   # 翻译进行中
    COMPLETED = "completed"     # 翻译完成
    FAILED = "failed"          # 翻译失败
```

## 3. 进度管理器设计

### 3.1 ProgressManager 类

```python
class ProgressManager:
    """Manager for handling translation progress updates."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_progress(self, book_id: str, chapters: Dict) -> TranslationProgress:
        """创建新的进度记录"""
        
    async def get_progress(self, book_id: str) -> TranslationProgress:
        """获取指定书籍的进度记录"""
        
    async def update_chapter(self, book_id: str, chapter_id: str) -> None:
        """更新章节完成状态"""
        
    async def start_translation(self, book_id: str) -> None:
        """标记翻译开始"""
        
    async def complete_translation(self, book_id: str) -> None:
        """标记翻译完成"""
```

## 4. 与 EpubProcessor 的集成方案

### 4.1 初始化阶段

在 EpubProcessor 的 prepare 方法中：
1. 加载 EPUB 文件并提取章节信息
2. 使用 ProgressManager 创建进度记录
3. 初始化 total_chapters 信息

### 4.2 翻译阶段

在 process 方法中：
1. 调用 start_translation 标记开始
2. 对每个章节翻译完成后，调用 update_chapter 更新进度
3. 所有章节完成后，调用 complete_translation 标记完成

## 5. 进度查询

TranslationProgress 模型提供了以下方法：
- get_progress_percentage(): 获取当前翻译进度百分比
- is_completed(): 检查是否所有章节都已完成
- get_pending_chapters(): 获取待翻译的章节列表
- get_completed_chapters(): 获取已完成的章节列表
