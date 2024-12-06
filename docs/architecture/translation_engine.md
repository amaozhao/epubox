# 翻译引擎管理模块设计

## 1. 概述

翻译引擎管理模块采用Provider模式，统一管理各种翻译服务提供商，实现灵活的配置和切换。

## 2. 数据库设计

```sql
-- 翻译服务提供商配置表
CREATE TABLE IF NOT EXISTS translation_providers (
    name TEXT NOT NULL,                    -- 提供商名称（如 openai, google）
    provider_type TEXT NOT NULL,           -- 提供商类型
    is_default BOOLEAN DEFAULT false,      -- 是否为默认提供商
    enabled BOOLEAN DEFAULT true,          -- 是否启用
    config TEXT NOT NULL,                  -- JSON格式的配置信息
    rate_limit INTEGER DEFAULT 3,          -- 每分钟请求限制
    retry_count INTEGER DEFAULT 3,         -- 重试次数
    retry_delay INTEGER DEFAULT 5,         -- 重试延迟(秒)
    limit_type TEXT NOT NULL,              -- 限制类型（chars: 字符数, tokens: token数）
    limit_value INTEGER NOT NULL           -- 具体的限制值
);

-- 提供商统计表
CREATE TABLE IF NOT EXISTS provider_stats (
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
    retry_delay: int = 5,
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
        retry_delay: int = 5
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
    
    @retry_on_error(max_retries=3, retry_delay=5)
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
        
        try:
            # 执行翻译
            translated_text = await provider.translate(
                text, source_lang, target_lang, **kwargs
            )
            
            return translated_text
            
        except Exception as e:
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

## 7. 翻译引擎设计文档

### 概述
翻译引擎负责将文本从源语言翻译为目标语言，支持多种翻译提供者。

### 基类 `TranslationProvider`
`TranslationProvider` 是所有翻译提供者的基类，定义了通用接口和组件。

#### 主要方法
- `get_provider_type()`: 返回提供者类型标识符。
- `validate_config(config: dict)`: 验证提供者配置，确保必需的参数（如 API 密钥）存在。
- `translate(text: str, source_lang: str, target_lang: str, **kwargs)`: 执行翻译操作，应用速率限制和重试机制。

### 速率限制器 `RateLimiter`
`RateLimiter` 控制翻译请求的速率，防止超出 API 限制。支持自定义请求频率和时间窗口。

#### 主要特性
- 支持按秒级别的请求频率控制（默认：每秒1次请求）
- 可配置时间窗口
- 异步锁机制确保并发安全

#### 主要方法
- `acquire()`: 获取一个令牌以进行翻译请求

### 文本限制管理
#### 计量单位
系统支持多种文本计量单位：
- Token：基于模型的分词计数（如 Mistral）
- Character：字符计数（如 Google Translate）

#### 限制配置 `LimitConfig`
统一的限制配置类，包含：
- `requests_per_second`: 每秒请求数限制
- `max_units_per_request`: 每次请求的最大单位数
- `unit_type`: 计量单位类型（TOKEN/CHARACTER）

#### 文本计数器 `TextCounter`
抽象接口，用于实现不同的计数方式：
- `TokenCounter`: 实现基于 token 的计数
- `CharacterCounter`: 实现基于字符的计数

### 具体实现示例

### Mistral Provider
- 计量单位：Token
- 限制：
  - 每秒 1 次请求
  - 每次最多 25000 tokens

### Google Translate Provider
- 计量单位：Character
- 限制：
  - 每秒 10 次请求
  - 每次最多 5000 字符

### 错误处理机制
翻译过程中可能会遇到不同类型的错误，如速率限制错误、无效请求等，系统会抛出相应的异常。

### 使用示例
```python
# 创建 Mistral 提供者
mistral_provider = MistralProvider(config)

# 执行翻译
translated_text = await mistral_provider.translate("Hello, world!", "en", "fr")
```

### 翻译引擎设计文档

## 1. 系统概述
翻译引擎负责管理和协调多个翻译提供者，提供统一的翻译接口。系统支持动态配置和加载不同的翻译提供者，并对每个提供者实施相应的限制和控制。

## 2. 核心组件

### 2.1 提供者工厂（Provider Factory）
提供者工厂负责创建和管理翻译提供者实例。采用基于配置文件的动态加载机制，支持灵活地添加和管理多个翻译提供者。

#### 2.1.1 配置文件结构
```yaml
# providers.yaml
mistral:
  module: app.translation.providers.mistral
  class: MistralProvider
  description: Mistral AI Translation Provider
  limit_type: tokens
  default_rate_limit: 1
  default_max_units: 25000

google:
  module: app.translation.providers.google
  class: GoogleProvider
  description: Google Cloud Translation
  limit_type: chars
  default_rate_limit: 10
  default_max_units: 5000
```

#### 2.1.2 工厂类职责
- 加载和解析提供者配置文件
- 动态导入提供者类
- 创建提供者实例
- 管理提供者类的缓存

### 2.2 基础提供者类（Base Provider）
所有翻译提供者的基类，定义统一的接口和通用功能。

#### 2.2.1 核心功能
- 速率限制控制
- 单位（字符/token）计数
- 错误处理和重试机制
- 配置验证

#### 2.2.2 主要接口
- 初始化配置和限制
- 翻译方法
- 单位计数方法
- 配置验证方法

### 2.3 限制管理（Limit Management）
管理不同类型的限制，包括请求频率和文本单位限制。

#### 2.3.1 限制类型
- 字符数限制（CHARS）
- Token数限制（TOKENS）

#### 2.3.2 限制配置
- 每秒请求数限制
- 单次请求最大单位数
- 重试次数和间隔

### 2.4 数据模型
数据库模型用于存储提供者配置和统计信息。

#### 2.4.1 提供者配置模型
- 基本信息（名称、类型等）
- 限制配置
- API配置
- 启用状态

#### 2.4.2 统计信息模型
- 请求统计
- 错误统计
- 性能指标

## 3. 工作流程

### 3.1 提供者初始化
1. 系统启动时加载提供者配置文件
2. 按需动态导入提供者类
3. 根据数据库配置创建提供者实例

### 3.2 翻译请求处理
1. 接收翻译请求
2. 检查文本长度限制
3. 应用速率限制
4. 执行翻译
5. 处理错误和重试
6. 更新统计信息

### 3.3 提供者管理
1. 通过配置文件添加新提供者
2. 通过数据库管理提供者配置
3. 监控提供者状态和性能

## 4. 扩展性设计

### 4.1 添加新提供者
1. 创建提供者类实现文件
2. 在配置文件中添加提供者信息
3. 在数据库中添加提供者配置

### 4.2 自定义限制
1. 在提供者配置中定义限制参数
2. 实现自定义的限制检查逻辑

## 5. 错误处理

### 5.1 错误类型
- 配置错误
- 速率限制错误
- API错误
- 网络错误

### 5.2 重试策略
- 可配置的重试次数
- 指数退避
- 错误分类和选择性重试

## 6. 监控和统计

### 6.1 性能指标
- 响应时间
- 成功率
- 错误率
- 速率限制命中率

### 6.2 资源使用
- API 调用量
- 字符/Token 使用量
- 并发请求数

## 7. 配置示例

### 7.1 提供者配置文件
```yaml
# 完整的提供者配置示例
mistral:
  module: app.translation.providers.mistral
  class: MistralProvider
  description: Mistral AI Translation Provider
  limit_type: tokens
  default_rate_limit: 1
  default_max_units: 25000
  retry:
    max_attempts: 3
    initial_delay: 1
    max_delay: 10
  features:
    streaming: true
    batch_translation: false
```

### 7.2 数据库配置
```python
# 提供者配置示例
provider_config = {
    "name": "Mistral",
    "provider_type": "mistral",
    "is_default": True,
    "enabled": True,
    "config": {
        "api_key": "xxx",
        "model": "mistral-tiny"
    },
    "rate_limit": 1,
    "limit_type": LimitType.TOKENS,
    "limit_value": 25000
}
```
