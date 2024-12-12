# EPUB 翻译进度管理设计

## 1. 功能概述

为 EPUB 翻译过程添加进度管理功能，支持：
- 记录每个章节的翻译状态
- 支持中断后继续翻译
- 提供翻译进度查询

## 2. 数据模型设计

```python
class TranslationProgress(Base):
    """Translation progress tracking."""
    
    __tablename__ = "translation_progress"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[str] = mapped_column(String, nullable=False)  # epub 文件的唯一标识
    chapters: Mapped[list] = mapped_column(JSON, nullable=False)  # 所有章节信息列表
    provider_id: Mapped[int] = mapped_column(ForeignKey("translation_providers.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)  # pending/processing/completed
    created: Mapped[datetime] = mapped_column(nullable=False)
    updated: Mapped[datetime] = mapped_column(nullable=False)
```

### 章节信息结构 (JSON)
```python
{
    "id": "chapter1.xhtml",     # 章节文件名
    "type": "html",             # 类型：html/ncx
    "name": "Chapter 1",        # 章节名称
    "status": "pending",        # 状态：pending/completed
    "completed_at": null        # 完成时间
}
```

## 3. 状态定义

- pending: 等待翻译
- processing: 翻译进行中
- completed: 翻译完成

## 4. 与 EpubProcessor 的集成方案

### 4.1 初始化阶段
```python
async def prepare(self):
    # 1. 复制原始文件到工作目录
    # 2. 加载 EPUB 文件
    # 3. 提取章节信息
    # 4. 创建进度记录
```

### 4.2 翻译处理流程
```python
async def process(self):
    # 1. 准备工作（prepare）
    # 2. 遍历处理 HTML 内容
    #    - 检查章节状态
    #    - 翻译未完成章节
    #    - 更新进度
    # 3. 遍历处理 NCX 内容
    #    - 检查章节状态
    #    - 翻译未完成章节
    #    - 更新进度
    # 4. 更新整体状态
```

### 4.3 进度查询
```python
async def get_progress(self):
    # 返回：
    # - 总章节数
    # - 已完成章节数
    # - 完成百分比
    # - 当前状态
```

## 5. 工作流程

1. 开始翻译：
   - 创建进度记录
   - 初始化所有章节状态为 pending

2. 翻译过程：
   - 检查章节状态
   - 翻译未完成章节
   - 更新章节状态和时间戳

3. 中断处理：
   - 保存当前进度
   - 下次启动时从未完成章节继续

4. 完成处理：
   - 所有章节完成后更新整体状态
   - 记录完成时间

## 6. 优点

1. 简单清晰的数据结构
2. 支持断点续传
3. 可追踪每个章节的状态
4. 易于集成到现有代码
5. 支持进度查询

## 7. 后续扩展可能

1. 添加失败重试机制
2. 添加翻译时间估算
3. 支持暂停/继续功能
4. 添加更详细的进度报告
