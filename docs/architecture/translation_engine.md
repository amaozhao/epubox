# 翻译引擎管理模块设计

## 1. 概述

翻译引擎管理模块采用Provider模式，统一管理各种翻译服务提供商，实现灵活的配置和切换。同时提供专业词汇映射功能，确保翻译的一致性和准确性。

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

-- 专业词汇映射表
CREATE TABLE IF NOT EXISTS translation_terms (
    source_term TEXT NOT NULL,             -- 源词汇
    target_term TEXT NOT NULL,             -- 目标词汇
    language_pair TEXT NOT NULL,           -- 语言对（如 en-zh）
    domain TEXT,                           -- 领域（如 技术、医学）
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_term, language_pair)
);

-- 创建索引
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_stats_date ON provider_stats(provider_id, date);
CREATE INDEX IF NOT EXISTS idx_translation_terms_domain ON translation_terms(domain);
```

## 3. Provider设计

### 3.1 基础组件

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
        self.translations = {}  # 专业词汇映射
    
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

    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        **kwargs
    ) -> str:
        """翻译接口，包含专业词汇处理"""
        # 应用专业词汇映射
        result = text
        for source, target in self.translations.items():
            result = result.replace(source, target)
            
        # 调用实际翻译
        result = await self._translate(result, source_lang, target_lang, **kwargs)
        
        return result
```

### 3.2 专业词汇管理

```python
class TranslatorProvider:
    def __init__(self, limit_value):
        self.limit_value = limit_value
        self.translations = {
            # 技术文档相关
            "Preface": "前言",
            "Chapter": "章节",
            "FastAPI": "FastAPI",
            "Python": "Python",
            "API": "API",
            "RESTful": "RESTful",
            "SQL": "SQL",
            "NoSQL": "NoSQL",
            "MongoDB": "MongoDB",
            "Redis": "Redis",
            "WebSocket": "WebSocket",
            "OAuth2": "OAuth2",
            "JWT": "JWT",
            "ORM": "ORM",
            "CRUD": "增删改查",
            "LLM": "大语言模型",
            "RAG": "检索增强生成"
        }

    async def translate(self, content: str, source_lang: str, target_lang: str) -> str:
        """翻译内容，保持专业词汇一致性"""
        result = content
        
        # 应用专业词汇映射
        for en, zh in self.translations.items():
            result = result.replace(en, zh)
            
        return result
```

## 4. 翻译流程

### 4.1 基本流程

1. **初始化**
   - 加载提供商配置
   - 初始化专业词汇映射
   - 创建翻译提供者实例

2. **预处理**
   - 检查内容格式
   - 分析文本结构
   - 识别特殊标记

3. **翻译处理**
   - 应用专业词汇映射
   - 调用翻译服务
   - 处理翻译结果

4. **后处理**
   - 还原特殊标记
   - 格式化输出
   - 更新统计信息

### 4.2 树结构处理

1. **节点分类**
   - 识别叶节点和非叶节点
   - 确定可翻译内容
   - 保护特殊节点

2. **内容合并**
   - 合并相邻文本节点
   - 优化翻译单元大小
   - 维护节点关系

3. **翻译处理**
   - 翻译叶节点内容
   - 保持树结构完整
   - 重建文档结构

## 5. 错误处理

### 5.1 基本错误类型

1. **配置错误**
   - 无效的提供商配置
   - 缺失必要参数
   - 权限验证失败

2. **服务错误**
   - API调用失败
   - 网络连接问题
   - 响应超时

3. **内容错误**
   - 无效的输入格式
   - 超出长度限制
   - 不支持的语言

### 5.2 错误恢复策略

1. **自动重试**
   - 指数退避重试
   - 错误阈值控制
   - 备用提供商切换

2. **错误报告**
   - 详细错误日志
   - 统计信息更新
   - 状态通知

## 6. 性能优化

### 6.1 请求优化

1. **批量处理**
   - 合并相邻请求
   - 优化请求大小
   - 控制并发数量

2. **缓存机制**
   - 专业词汇缓存
   - 翻译结果缓存
   - 配置信息缓存

### 6.2 资源管理

1. **连接池**
   - 复用HTTP连接
   - 管理并发限制
   - 自动释放资源

2. **内存优化**
   - 及时清理缓存
   - 控制数据结构大小
   - 避免内存泄漏
