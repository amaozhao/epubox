# EPUB处理方案设计文档

## 1. 概述

本文档详细描述了EPUB翻译服务的整体架构和处理流程，包括任务管理、EPUB处理、HTML内容处理、翻译服务集成等关键技术点。

## 2. 系统架构

### 2.1 核心组件

1. **任务管理器（TranslationTaskManager）**
   - 创建和管理翻译任务
   - 协调各组件工作
   - 跟踪任务状态和进度
   - 错误处理和恢复机制

2. **EPUB处理器（EpubProcessor）**
   - 复制和管理EPUB文件
   - 提取和更新EPUB内容
   - 保持原始EPUB结构

3. **HTML内容处理器（HTMLContentProcessor）**
   - 处理HTML内容
   - 管理占位符机制
   - 文本分块处理

4. **翻译服务（TranslationService）**
   - 对接翻译API
   - 处理翻译请求和响应
   - 管理API限制和错误

5. **进度追踪器（ProgressTracker）**
   - 更新和获取任务进度
   - 提供进度统计信息
   - 支持进度持久化

## 3. 处理流程

```
创建翻译任务
   ↓
初始化任务
   ├─► 验证输入文件
   ├─► 创建工作目录
   └─► 初始化进度追踪
   ↓
EPUB处理（提取）
   ├─► 复制原始EPUB
   ├─► 加载工作文件
   └─► 提取HTML内容
   ↓
HTML处理（翻译准备）
   ├─► 识别不翻译标签
   ├─► 创建占位符
   ├─► 提取可翻译文本
   └─► 文本分块
   ↓
翻译处理
   ├─► 发送翻译请求
   └─► 接收翻译结果
   ↓
HTML处理（还原）
   ├─► 还原占位符
   └─► 验证HTML结构
   ↓
EPUB处理（更新）
   ├─► 更新EPUB内容
   └─► 保存工作文件
   ↓
完成任务
   ├─► 验证结果
   ├─► 清理工作文件
   └─► 更新任务状态
```

### 3.1 详细步骤说明

1. **创建翻译任务**
   - 接收翻译请求
   - 生成唯一任务ID
   - 初始化任务配置
   - 设置目标语言

2. **初始化任务**
   - **验证输入**
     * 检查EPUB文件格式
     * 验证文件完整性
     * 检查文件大小限制

   - **工作环境准备**
     * 创建任务工作目录
     * 初始化日志系统
     * 设置任务参数

   - **进度追踪**
     * 初始化进度记录
     * 设置检查点机制
     * 准备状态报告

3. **EPUB处理（提取）**
   - **文件准备**
     * 复制原始EPUB文件
     * 生成工作文件名
     * 保存原始路径信息

   - **内容提取**
     * 加载EPUB文件
     * 识别HTML内容
     * 提取待翻译内容

4. **HTML处理（翻译准备）**
   - **标签处理**
     * 识别SKIP_TAGS
     * 分析HTML结构
     * 保存标签信息

   - **占位符处理**
     * 生成唯一占位符
     * 保存原始内容
     * 建立映射关系

   - **文本处理**
     * 提取待翻译文本
     * 分析文本长度
     * 执行文本分块

5. **翻译处理**
   - **请求管理**
     * 控制请求频率
     * 处理API限制
     * 记录翻译进度

   - **错误处理**
     * 请求重试机制
     * 错误日志记录
     * 状态更新

6. **HTML处理（还原）**
   - **内容还原**
     * 还原占位符内容
     * 检查标签完整性
     * 验证HTML结构

7. **EPUB处理（更新）**
   - **内容更新**
     * 更新HTML内容
     * 保持原始结构
     * 更新元数据

   - **文件保存**
     * 保存修改内容
     * 验证文件完整性
     * 更新文件信息

8. **完成任务**
   - **结果验证**
     * 检查翻译完整性
     * 验证文件结构
     * 生成完成报告

   - **资源清理**
     * 清理临时文件
     * 归档工作文件
     * 更新系统状态

   - **状态更新**
     * 更新任务状态
     * 记录完成时间
     * 生成任务报告

### 3.2 错误处理策略

1. **任务级错误**
   - 输入文件无效
   - 系统资源不足
   - 配置错误

2. **EPUB处理错误**
   - 文件损坏
   - 结构异常
   - 权限问题

3. **HTML处理错误**
   - 解析失败
   - 结构不完整
   - 编码问题

4. **翻译错误**
   - API限制
   - 网络问题
   - 内容限制

### 3.3 进度追踪

1. **检查点机制**
   - 定期保存状态
   - 支持断点恢复
   - 任务进度统计

2. **状态报告**
   - 实时进度更新
   - 错误状态通知
   - 完成度统计

{{ ... }}