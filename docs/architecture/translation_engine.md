# 翻译引擎管理模块设计

## 1. 概述

翻译引擎管理模块采用Provider模式，统一管理各种翻译服务提供商，实现灵活的配置和切换。

## 2. 数据库设计

```sql
-- 翻译服务提供商配置表
CREATE TABLE IF NOT EXISTS translation_providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                    -- 提供商名称（如 openai, google）
    provider_type TEXT NOT NULL,           -- 提供商类型
    is_default BOOLEAN DEFAULT false,      -- 是否为默认提供商
    enabled BOOLEAN DEFAULT true,          -- 是否启用
    config TEXT NOT NULL,                  -- JSON格式的配置信息
    rate_limit INTEGER DEFAULT 3,          -- 每分钟请求限制
    retry_count INTEGER DEFAULT 3,         -- 重试次数
    retry_delay INTEGER DEFAULT 60,        -- 重试延迟(秒)
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

-- 翻译记录表
CREATE TABLE IF NOT EXISTS translation_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,                -- 关联的任务ID
    chapter_index INTEGER NOT NULL,       -- 章节索引
    provider_id INTEGER NOT NULL,         -- 使用的提供商ID
    source_text TEXT NOT NULL,            -- 源文本
    translated_text TEXT,                 -- 翻译后的文本
    source_lang TEXT NOT NULL,            -- 源语言
    target_lang TEXT NOT NULL,            -- 目标语言
    word_count INTEGER NOT NULL,          -- 源文本字数
    status TEXT NOT NULL,                 -- 状态（pending/success/failed）
    error_message TEXT,                   -- 错误信息
    created_at TIMESTAMP NOT NULL,        -- 开始时间
    completed_at TIMESTAMP,               -- 完成时间
    FOREIGN KEY (provider_id) REFERENCES translation_providers(id),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

-- 提供商统计表
CREATE TABLE IF NOT EXISTS provider_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL,
    date DATE NOT NULL,                    -- 统计日期
    total_requests INTEGER DEFAULT 0,      -- 总请求数
    success_count INTEGER DEFAULT 0,       -- 成功数
    error_count INTEGER DEFAULT 0,         -- 错误数
    rate_limit_hits INTEGER DEFAULT 0,     -- 速率限制触发次数
    avg_response_time REAL DEFAULT 0,      -- 平均响应时间(秒)
    total_words INTEGER DEFAULT 0,         -- 总处理字数
    FOREIGN KEY (provider_id) REFERENCES translation_providers(id)
);

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_records_task ON translation_records(task_id);
CREATE INDEX IF NOT EXISTS idx_records_status ON translation_records(status);
CREATE INDEX IF NOT EXISTS idx_records_chapter ON translation_records(task_id, chapter_index);
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_stats_date ON provider_stats(provider_id, date);
```

## 3. Provider设计

```python
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Dict, Optional, Any, TypeVar, Generic
from enum import Enum
import asyncio
import json
import time
from functools import wraps

T = TypeVar('T')

class TranslationError(Exception):
    """翻译错误基类"""
    pass

class RateLimitError(TranslationError):
    """速率限制错误"""
    pass

class ConfigurationError(TranslationError):
    """配置错误"""
    pass

class ProviderError(TranslationError):
    """提供商错误"""
    pass

class RateLimiter:
    """速率限制器"""
    
    def __init__(self, rate_limit: int, time_window: int = 60):
        self.rate_limit = rate_limit
        self.time_window = time_window
        self.tokens = rate_limit
        self.last_update = time.time()
        self.lock = asyncio.Lock()
    
    async def acquire(self):
        async with self.lock:
            now = time.time()
            time_passed = now - self.last_update
            self.tokens = min(
                self.rate_limit,
                self.tokens + time_passed * (self.rate_limit / self.time_window)
            )
            self.last_update = now
            
            if self.tokens < 1:
                raise RateLimitError(
                    f"Rate limit exceeded. Please wait {self.time_window / self.rate_limit:.1f} seconds."
                )
            
            self.tokens -= 1

def retry_on_error(
    max_retries: int = 3,
    retry_delay: int = 1,
    exceptions: tuple = (TranslationError,)
):
    """重试装饰器"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        delay = retry_delay * (2 ** attempt)  # 指数退避
                        await asyncio.sleep(delay)
                    continue
            raise last_error
        return wrapper
    return decorator

class AsyncContextManager(Generic[T]):
    """异步上下文管理器"""
    
    async def __aenter__(self) -> T:
        await self.initialize()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup()
    
    async def initialize(self):
        """初始化资源"""
        pass
    
    async def cleanup(self):
        """清理资源"""
        pass

class TranslationProvider(AsyncContextManager['TranslationProvider'], ABC):
    """翻译服务提供商基类"""
    
    def __init__(
        self,
        config: dict,
        rate_limit: int = 3,
        retry_count: int = 3,
        retry_delay: int = 1
    ):
        self.config = config
        self.rate_limiter = RateLimiter(rate_limit)
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        self._initialized = False
    
    async def initialize(self):
        """初始化提供商"""
        if not self._initialized:
            if not self.validate_config(self.config):
                raise ConfigurationError(f"Invalid configuration for {self.get_provider_type()}")
            await self._initialize()
            self._initialized = True
    
    async def _initialize(self):
        """具体初始化逻辑，由子类实现"""
        pass
    
    async def cleanup(self):
        """清理资源"""
        self._initialized = False
        await self._cleanup()
    
    async def _cleanup(self):
        """具体清理逻辑，由子类实现"""
        pass
    
    @abstractmethod
    def get_provider_type(self) -> str:
        """获取提供商类型"""
        pass
    
    @abstractmethod
    def get_supported_languages(self) -> list:
        """获取支持的语言列表"""
        pass
    
    @abstractmethod
    async def _translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        **kwargs
    ) -> str:
        """实际的翻译实现"""
        pass
    
    @abstractmethod
    def validate_config(self, config: dict) -> bool:
        """验证配置是否有效"""
        pass
    
    @retry_on_error()
    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        **kwargs
    ) -> str:
        """执行翻译，包含重试和速率限制逻辑"""
        if not self._initialized:
            raise ProviderError("Provider not initialized")
        
        await self.rate_limiter.acquire()
        return await self._translate(text, source_lang, target_lang, **kwargs)

class OpenAIProvider(TranslationProvider):
    """OpenAI翻译提供商"""
    
    def __init__(self, config: dict, **kwargs):
        super().__init__(config, **kwargs)
        self.client = None
    
    async def _initialize(self):
        """初始化OpenAI客户端"""
        # 初始化OpenAI客户端
        pass
    
    async def _cleanup(self):
        """清理OpenAI客户端"""
        if self.client:
            await self.client.close()
            self.client = None
    
    def get_provider_type(self) -> str:
        return "openai"
    
    def get_supported_languages(self) -> list:
        return ["en", "zh", "ja", "ko", "fr", "de", "es"]
    
    async def _translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        **kwargs
    ) -> str:
        if not self.client:
            raise ProviderError("OpenAI client not initialized")
        
        try:
            # 实现OpenAI翻译逻辑
            pass
        except Exception as e:
            raise ProviderError(f"OpenAI translation failed: {str(e)}")
    
    def validate_config(self, config: dict) -> bool:
        required_fields = {'api_key', 'model'}
        return all(field in config for field in required_fields)

class GoogleProvider(TranslationProvider):
    """Google翻译提供商"""
    
    def __init__(self, config: dict, **kwargs):
        super().__init__(config, **kwargs)
        self.client = None
    
    async def _initialize(self):
        """初始化Google客户端"""
        # 初始化Google Translation客户端
        pass
    
    async def _cleanup(self):
        """清理Google客户端"""
        if self.client:
            await self.client.close()
            self.client = None
    
    def get_provider_type(self) -> str:
        return "google"
    
    def get_supported_languages(self) -> list:
        return ["en", "zh", "ja", "ko", "fr", "de", "es"]
    
    async def _translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        **kwargs
    ) -> str:
        if not self.client:
            raise ProviderError("Google client not initialized")
        
        try:
            # 实现Google翻译逻辑
            pass
        except Exception as e:
            raise ProviderError(f"Google translation failed: {str(e)}")
    
    def validate_config(self, config: dict) -> bool:
        required_fields = {'api_key', 'project_id'}
        return all(field in config for field in required_fields)

class ProviderFactory:
    """提供商工厂"""
    
    _providers: Dict[str, type] = {
        "openai": OpenAIProvider,
        "google": GoogleProvider
    }
    
    @classmethod
    def register_provider(cls, provider_type: str, provider_class: type):
        """注册新的提供商"""
        if not issubclass(provider_class, TranslationProvider):
            raise ValueError("Provider class must inherit from TranslationProvider")
        cls._providers[provider_type] = provider_class
    
    @classmethod
    async def create_provider(
        cls,
        provider_type: str,
        config: dict,
        **kwargs
    ) -> TranslationProvider:
        """创建并初始化提供商实例"""
        if provider_type not in cls._providers:
            raise ValueError(f"Unknown provider type: {provider_type}")
        
        provider_class = cls._providers[provider_type]
        provider = provider_class(config, **kwargs)
        
        try:
            await provider.initialize()
            return provider
        except Exception as e:
            await provider.cleanup()
            raise ConfigurationError(f"Failed to initialize provider: {str(e)}")

class TranslationManager(AsyncContextManager['TranslationManager']):
    """翻译管理器"""
    
    def __init__(self, db):
        self.db = db
        self.providers: Dict[int, TranslationProvider] = {}
        self.default_provider_id = None
    
    async def initialize(self):
        """初始化启用的提供商"""
        async with self.db.connection() as conn:
            rows = await conn.fetch("""
                SELECT * FROM translation_providers 
                WHERE enabled = true
            """)
            
            for row in rows:
                try:
                    config = json.loads(row['config'])
                    provider = await ProviderFactory.create_provider(
                        row['provider_type'],
                        config,
                        rate_limit=row['rate_limit'],
                        retry_count=row['retry_count'],
                        retry_delay=row['retry_delay']
                    )
                    self.providers[row['id']] = provider
                    
                    if row['is_default']:
                        self.default_provider_id = row['id']
                except Exception as e:
                    # 记录错误但继续初始化其他提供商
                    print(f"Failed to initialize provider {row['name']}: {str(e)}")
    
    async def cleanup(self):
        """清理所有提供商资源"""
        for provider in self.providers.values():
            await provider.cleanup()
        self.providers.clear()
        self.default_provider_id = None
    
    @retry_on_error(max_retries=3, retry_delay=1)
    async def translate(
        self,
        task_id: str,
        chapter_index: int,
        text: str,
        source_lang: str,
        target_lang: str,
        provider_id: Optional[int] = None,
        **kwargs
    ) -> str:
        """执行翻译"""
        provider_id = provider_id or self.default_provider_id
        if not provider_id or provider_id not in self.providers:
            raise ValueError("No valid translation provider specified")
        
        provider = self.providers[provider_id]
        
        # 记录翻译请求
        async with self.db.connection() as conn:
            record_id = await conn.fetchval("""
                INSERT INTO translation_records (
                    task_id, chapter_index, provider_id,
                    source_text, source_lang, target_lang,
                    word_count, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
            """, task_id, chapter_index, provider_id,
                text, source_lang, target_lang,
                len(text.split()), 'pending',
                datetime.now())
        
        try:
            # 执行翻译
            translated_text = await provider.translate(
                text, source_lang, target_lang, **kwargs
            )
            
            # 更新成功记录
            async with self.db.connection() as conn:
                await conn.execute("""
                    UPDATE translation_records
                    SET translated_text = ?,
                        status = ?,
                        completed_at = ?
                    WHERE id = ?
                """, 
                translated_text,
                'success',
                datetime.now(),
                record_id
                )
            
            return translated_text
            
        except Exception as e:
            # 更新失败记录
            async with self.db.connection() as conn:
                await conn.execute("""
                    UPDATE translation_records
                    SET status = ?,
                        error_message = ?,
                        completed_at = ?
                    WHERE id = ?
                """, 
                'failed',
                str(e),
                datetime.now(),
                record_id
                )
            raise

## 4. 使用示例

```python
# 初始化翻译管理器
translation_manager = TranslationManager(db)
await translation_manager.initialize()

# 添加OpenAI提供商
openai_config = {
    "api_key": "your-api-key",
    "model": "gpt-3.5-turbo",
    "temperature": 0.3
}
openai_id = await translation_manager.add_provider(
    name="OpenAI Translator",
    provider_type="openai",
    config=openai_config,
    is_default=True
)

# 添加Google提供商
google_config = {
    "api_key": "your-api-key",
    "project_id": "your-project-id"
}
google_id = await translation_manager.add_provider(
    name="Google Translator",
    provider_type="google",
    config=google_config
)

# 执行翻译
try:
    translated_text = await translation_manager.translate(
        task_id="task-123",
        chapter_index=0,
        text="Hello, world!",
        source_lang="en",
        target_lang="zh",
        provider_id=openai_id  # 可选，不指定则使用默认提供商
    )
    print(f"翻译结果: {translated_text}")
except Exception as e:
    print(f"翻译失败: {e}")

# 查看提供商信息
providers = await translation_manager.get_provider_info()
for provider in providers:
    print(f"\n提供商: {provider['name']}")
    print(f"类型: {provider['provider_type']}")
    print(f"默认: {provider['is_default']}")
    print(f"启用: {provider['enabled']}")
```

## 5. 特性

1. **错误处理**
   - 自定义错误类型
   - 重试机制
   - 错误记录和追踪
   - 优雅的资源清理

2. **资源管理**
   - 异步上下文管理
   - 自动资源清理
   - 连接池管理
   - 初始化和清理机制

3. **可靠性**
   - 速率限制保护
   - 重试策略
   - 错误恢复
   - 状态监控

4. **扩展性**
   - 提供商注册机制
   - 统一的接口约束
   - 配置验证
   - 运行时管理

## 6. 注意事项

1. **资源管理**
   - 使用异步上下文管理器
   - 及时清理资源
   - 处理初始化失败
   - 优雅关闭服务

2. **错误处理**
   - 捕获所有异常
   - 适当的重试策略
   - 记录详细错误信息
   - 监控错误模式

3. **性能优化**
   - 连接池复用
   - 并发请求控制
   - 资源限制管理
   - 缓存机制
