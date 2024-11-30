# EPUBox Translation Engine Requirements

## 1. 系统概述

EPUBox 是一个专门用于 EPUB 电子书翻译的服务系统。它通过复制原有 EPUB 文件的结构，专注于翻译内容文件（HTML和目录），同时保持电子书的其他部分（样式、资源等）不变。系统支持断点续译，可以从上次中断的位置继续翻译任务。

## 1.5 系统架构

### 1.5.1 整体架构

EPUBox采用模块化的分层架构设计，主要包含以下层次：

```
┌─────────────────────────────────────┐
│            表现层（CLI/API）         │
├─────────────────────────────────────┤
│            应用服务层               │
│  ┌─────────────┐    ┌────────────┐  │
│  │  任务管理器  │    │ 状态管理器 │  │
│  └─────────────┘    └────────────┘  │
├─────────────────────────────────────┤
│            核心服务层               │
│  ┌─────────────┐    ┌────────────┐  │
│  │  EPUB解析器  │    │ 翻译服务   │  │
│  └─────────────┘    └────────────┘  │
│  ┌─────────────┐    ┌────────────┐  │
│  │ HTML处理器   │    │ 内容分析器 │  │
│  └─────────────┘    └────────────┘  │
├─────────────────────────────────────┤
│            基础设施层               │
│  ┌─────────────┐    ┌────────────┐  │
│  │  存储服务   │    │ 日志服务   │  │
│  └─────────────┘    └────────────┘  │
└─────────────────────────────────────┘
```

### 1.5.2 核心组件说明

1. **表现层**
   - CLI界面：提供命令行交互
   - API接口：提供RESTful API服务

2. **应用服务层**
   - 任务管理器：负责翻译任务的创建、调度和监控
   - 状态管理器：管理翻译任务的状态和进度

3. **核心服务层**
   - EPUB解析器：处理EPUB文件的解压、解析和重构
   - 翻译服务：对接不同的翻译引擎（OpenAI、DeepL等）
   - HTML处理器：处理HTML文件的解析、标记和重组
   - 内容分析器：分析文本内容，计算token，实现分段策略

4. **基础设施层**
   - 存储服务：持久化任务状态和中间结果
   - 日志服务：记录系统运行日志和错误信息

### 1.5.3 部署架构

EPUBox支持以下两种部署模式：

1. **单机部署**
```
┌─────────────────────────────────────┐
│             本地环境                │
│  ┌─────────────┐    ┌────────────┐  │
│  │  EPUBox CLI │    │ 本地存储   │  │
│  └─────────────┘    └────────────┘  │
└─────────────────────────────────────┘
            ↓             ↑
┌─────────────────────────────────────┐
│           外部翻译服务              │
│  (OpenAI API / DeepL API / 其他)    │
└─────────────────────────────────────┘
```

2. **服务器部署**
```
┌─────────────────────────────────────┐
│             客户端                  │
│  ┌─────────────┐    ┌────────────┐  │
│  │  EPUBox CLI │    │ HTTP客户端 │  │
│  └─────────────┘    └────────────┘  │
└─────────────────────────────────────┘
            ↓             ↑
┌─────────────────────────────────────┐
│             服务器                  │
│  ┌─────────────┐    ┌────────────┐  │
│  │ EPUBox API  │    │ 数据库存储 │  │
│  └─────────────┘    └────────────┘  │
└─────────────────────────────────────┘
            ↓             ↑
┌─────────────────────────────────────┐
│           外部翻译服务              │
│  (OpenAI API / DeepL API / 其他)    │
└─────────────────────────────────────┘
```

### 1.5.4 组件交互流程

1. **基本翻译流程**
```
┌──────────┐    ┌──────────┐    ┌──────────┐
│  用户    │    │任务管理器│    │EPUB解析器│
└────┬─────┘    └────┬─────┘    └────┬─────┘
     │              │               │
     │ 提交翻译任务 │               │
     │─────────────>│               │
     │              │ 解析EPUB文件  │
     │              │──────────────>│
     │              │               │
     │              │   返回文件列表│
     │              │<──────────────│
     │              │               │
┌────┴─────┐    ┌──┴─────┐    ┌────┴─────┐
│HTML处理器│    │翻译服务 │    │状态管理器│
└────┬─────┘    └────┬────┘    └────┬─────┘
     │              │               │
     │ 处理HTML文件 │               │
     │<─────────────│               │
     │              │               │
     │ 返回处理结果 │               │
     │─────────────>│               │
     │              │ 更新任务状态  │
     │              │──────────────>│
     │              │               │
```

2. **断点续传流程**
```
┌──────────┐    ┌──────────┐    ┌──────────┐
│  用户     │    │状态管理器 │    │任务管理器  │
└────┬─────┘    └────┬─────┘    └────┬─────┘
     │              │               │
     │ 请求恢复任务   │               │
     │─────────────>│               │
     │              │ 获取任务状态    │
     │              │──────────────>│
     │              │               │
     │              │ 返回断点信息    │
     │              │<──────────────│
     │              │               │
     │ 返回恢复结果   │               │
     │<─────────────│               │
     │              │               │
```

3. **错误处理流程**
```
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│  组件    │    │错误处理器│    │状态管理器│    │日志服务  │
└────┬─────┘    └────┬─────┘    └────┬─────┘    └────┬─────┘
     │              │               │               │
     │ 发生错误     │               │               │
     │─────────────>│               │               │
     │              │ 记录错误状态   │               │
     │              │──────────────>│               │
     │              │               │               │
     │              │ 写入错误日志   │               │
     │              │────────────────────────────-->│
     │              │               │               │
     │              │ 执行重试策略   │               │
     │<─────────────│               │               │
     │              │               │               │
     │              │ 更新最终状态   │               │
     │              │──────────────>│               │
     │              │               │               │
```

## 2. 核心功能需求

### 2.1 EPUB 处理
- **文件处理**
  - 复制原始 EPUB 文件结构
  - 保持 OPF 文件不变
  - 保留原有资源文件（图片、样式等）
  - 识别需要翻译的内容文件（HTML和目录）

- **翻译文件识别**
  - HTML 内容文件
  - 目录文件（toc.ncx, nav.xhtml）
  - 保持其他文件不变

### 2.2 任务管理
- **任务状态跟踪**
  - 整体翻译进度
  - 当前处理的文件
  - 文件内的翻译位置
  - 已完成的文件列表

- **断点续译支持**
  - 记录翻译断点信息
    * 当前处理的文件路径
    * 文件内的处理位置
    * 已翻译的内容缓存
  - 断点恢复机制
    * 加载上次的处理状态
    * 验证断点有效性
    * 从断点位置继续处理
  - 中间状态存储
    * 使用持久化存储（如数据库）
    * 定期保存处理状态
    * 支持手动保存断点

- **进度报告**
  - 总体翻译进度
    * 已处理文件数/总文件数
    * 已翻译字数/总字数
    * 预估剩余时间
  - 当前文件进度
    * 文件翻译百分比
    * 当前处理位置
    * 剩余内容量
  - 详细状态信息
    * 已完成的文件列表
    * 待处理的文件列表
    * 翻译错误记录

### 2.3 HTML 内容处理

#### 2.3.1 预处理阶段
- **不可翻译内容识别**
  - 识别并标记代码相关
    * 代码块（`<code>`, `<pre>`）
    * 脚本（`<script>`）
    * 样式（`<style>`）
  - 识别并标记多媒体内容
    * 图片（`<img>`）
    * 图形（`<figure>`, `<figcaption>`）
    * 音频（`<audio>`）
    * 视频（`<video>`）
    * SVG（`<svg>`）
  - 识别并标记数学和公式
    * 数学公式（`<math>`）
    * LaTeX 内容
  - 识别并标记交互元素
    * Canvas（`<canvas>`）
    * 嵌入内容（`<embed>`, `<object>`, `<iframe>`）
  - 识别并标记特殊属性
    * `data-*` 属性
    * `aria-*` 属性
    * `role` 属性
  - 生成唯一占位符（如 `[[NO_TRANSLATE_1]]`）
  - 维护占位符与原始内容的映射关系

- **占位符规范**
  - **基本格式**
    * 使用双方括号作为分隔符：`[[` 和 `]]`
    * 格式模式：`[[TYPE_NAME_INDEX]]`
    * 示例：`[[CODE_BLOCK_1]]`, `[[SCRIPT_1]]`, `[[STYLE_1]]`
  
  - **类型前缀规则**
    * 代码相关
      - `CODE_BLOCK`: 代码块内容 (`<code>`, `<pre>`)
      - `SCRIPT`: 脚本内容 (`<script>`)
      - `STYLE`: 样式内容 (`<style>`)
    * 多媒体内容
      - `IMAGE`: 图片内容 (`<img>`)
      - `FIGURE`: 图形内容 (`<figure>`)
      - `FIGCAPTION`: 图形说明 (`<figcaption>`)
      - `AUDIO`: 音频内容 (`<audio>`)
      - `VIDEO`: 视频内容 (`<video>`)
      - `SVG`: SVG图形内容 (`<svg>`)
    * 数学和公式
      - `MATH`: 数学公式 (`<math>`, LaTeX)
      - `LATEX`: LaTeX 内容
    * 交互元素
      - `CANVAS`: Canvas内容 (`<canvas>`)
      - `EMBED`: 嵌入内容 (`<embed>`)
      - `OBJECT`: 对象内容 (`<object>`)
      - `IFRAME`: 框架内容 (`<iframe>`)
    * 特殊属性
      - `DATA_ATTR`: data-* 属性
      - `ARIA_ATTR`: aria-* 属性
      - `ROLE_ATTR`: role 属性
    * 其他
      - `CUSTOM`: 自定义不翻译内容
      - `MIXED`: 混合内容
      - `MALFORMED`: 损坏的HTML
  
  - **编号规则**
    * 每种类型独立编号
    * 从1开始递增
    * 格式：`TYPE_名称_数字`
    * 示例：对于代码块 `CODE_BLOCK_1`, `CODE_BLOCK_2`
  
  - **嵌套规则**
    * 从内到外进行编号
    * 内部占位符优先处理
    * 示例：
      ```html
      <div>
        <pre>code1</pre>
        <div>
          <pre>code2</pre>
        </div>
      </div>
      <!-- 处理为 -->
      <div>
        [[CODE_BLOCK_1]]
        <div>
          [[CODE_BLOCK_2]]
        </div>
      </div>
      ```
  
  - **特殊情况处理**
    * 空内容标签：添加 `EMPTY` 标记，如 `[[CODE_BLOCK_EMPTY_1]]`
    * 混合内容：使用 `MIXED` 标记，如 `[[MIXED_CONTENT_1]]`
    * 损坏的HTML：使用 `MALFORMED` 标记，如 `[[MALFORMED_HTML_1]]`
  
  - **映射表结构**
    ```python
    {
        "[[CODE_BLOCK_1]]": {
            "type": "code_block",
            "content": "原始内容",
            "tag": "pre",
            "attributes": {原始标签属性},
            "index": 1
        }
    }
    ```

- **HTML 结构分析**
  - 解析 HTML 树形结构
  - 识别标签的嵌套关系
  - 确定可翻译的文本节点

#### 2.3.2 内容分析
- **Token 计算**
  - **目的**
    * 准确评估文本长度，确保不超过翻译API的限制
    * 优化分割策略，提高翻译质量和效率
  
  - **计算规则**
    * 基于OpenAI Tokenizer的分词规则
    * 中文：每个字符算一个token
    * 英文：基于BPE (Byte Pair Encoding) 算法
    * 标点符号：大部分单个符号算一个token
    * 空格和换行：每个都算一个token
  
  - **特殊处理**
    * HTML标签：不计入token计算
    * 占位符：作为一个整体计算（如 `[[CODE_BLOCK_1]]` 算一个token）
    * 数字和特殊字符：根据具体编码规则计算
    * 混合语言文本：分别计算后求和

- **分割策略**
  - **基本原则**
    * 优先在自然段落边界分割（如 `<p>`, `<div>`, `<section>` 等）
    * 其次在句子边界分割（如句号、问号、感叹号等）
    * 最后在其他合适的位置分割（如逗号、分号等）
    * 永不在词语中间分割
  
  - **HTML结构保护**
    * 完全保持原有的HTML标签结构
    * 只在需要翻译的文本内容处进行分割
    * 不添加任何新的HTML标签
    * 示例：
      ```html
      <!-- 原始HTML -->
      <div class="content">
        <p>这是第一段很长的内容，token数量超过限制...</p>
        <p>这是第二段很长的内容，token数量也超过限制...</p>
        <p>这是第三段内容...</p>
      </div>

      <!-- 正确的分割方式（保持原有HTML结构）-->
      <div class="content">
        <!-- 第一部分 -->
        <p>这是第一段很长的内容，token数量超过限制...</p>
        <!-- 第二部分 -->
        <p>这是第二段很长的内容，token数量也超过限制...</p>
        <!-- 第三部分 -->
        <p>这是第三段内容...</p>
      </div>
      ```

    * 更复杂的嵌套结构示例：
      ```html
      <!-- 原始HTML -->
      <div class="content">
        <section>
          <h2>章节标题</h2>
          <p>这是第一段很长的内容...</p>
          <p>这是第二段很长的内容...</p>
          <ul>
            <li>列表项1很长...</li>
            <li>列表项2很长...</li>
          </ul>
        </section>
      </div>

      <!-- 正确的分割方式（保持原有HTML结构）-->
      <div class="content">
        <section>
          <!-- 第一部分 -->
          <h2>章节标题</h2>
          <p>这是第一段很长的内容...</p>
          <!-- 第二部分 -->
          <p>这是第二段很长的内容...</p>
          <!-- 第三部分 -->
          <ul>
            <!-- 如果列表内容也超过限制，继续分割 -->
            <!-- 第三部分-1 -->
            <li>列表项1很长...</li>
            <!-- 第三部分-2 -->
            <li>列表项2很长...</li>
          </ul>
        </section>
      </div>
      ```

    * 分割原则：
      1. 严格保持原有的HTML结构不变
      2. 只在文本内容处进行分割
      3. 使用注释标记分割的部分
      4. 不添加任何新的HTML标签
      5. 保持标签的嵌套层级和属性不变
  
  - **语义完整性保护**
    * 列表项（`<li>`）保持完整
    * 表格行（`<tr>`）不被拆分
    * 标题（`<h1>` - `<h6>`）保持完整
    * 链接文本（`<a>`）不被拆分
  
  - **分割大小控制**
    * 最大分割大小：不超过翻译API的token限制（如OpenAI API为4096 tokens）
    * 最小分割大小：不小于指定阈值（如100 tokens），避免过度分割
    * 动态调整：根据内容复杂度和上下文关联性调整分割大小
  
  - **上下文保持**
    * 记录分割点的上下文信息
    * 在必要时添加上下文提示
    * 保持专有名词和术语的一致性
    * 维护文档的整体连贯性

### 2.4 错误处理

- **异常处理**
  - HTML 解析错误
  - Token 计算异常
  - 翻译服务异常
  - 内容重组错误

- **日志记录**
  - 详细的错误信息记录
  - 处理过程的追踪
  - 性能监控指标

## 3. 技术要求

### 3.1 性能要求
- 支持并发翻译请求
- 翻译响应时间控制
- 内存使用优化
- 大文档处理能力

### 3.2 可扩展性
- 支持多种翻译服务集成
- 可配置的分割策略
- 可自定义的预处理规则
- 灵活的后处理扩展

### 3.3 可维护性
- 模块化设计
- 完整的单元测试
- 清晰的代码文档
- 标准的错误处理

## 4. 接口设计

### 4.1 核心接口

#### 4.1.1 类关系图
```
EPUBTranslator (协调器)
    ↓
    ├── TaskManager (任务管理)
    │   ├── TaskState (任务状态)
    │   └── ProgressTracker (进度跟踪)
    │
    ├── EPUBProcessor (EPUB处理)
    │   ├── EPUBReader (读取)
    │   └── EPUBWriter (写入)
    │
    ├── HTMLProcessor (HTML处理)
    │   ├── ContentExtractor (内容提取)
    │   ├── PlaceholderManager (占位符管理)
    │   ├── TokenCalculator (Token计算)
    │   ├── ContentSplitter (内容分割)
    │   ├── ContentMerger (内容合并)
    │   └── HTMLRebuilder (重建HTML)
    │
    └── TranslationService (翻译服务)
```

#### 4.1.2 核心类职责

1. **EPUBTranslator**
   - 作为整个翻译流程的协调器
   - 管理其他组件的生命周期
   - 提供统一的对外接口
   ```python
   class EPUBTranslator:
       def translate_book(
           self, 
           epub_path: str, 
           target_lang: str,
           translation_config: TranslationConfig
       ) -> str:
           """
           翻译电子书
           translation_config: 包含翻译服务选择和配置
           """
           
       def resume_translation(self, task_id: str) -> str:
           """继续未完成的翻译任务"""
           
       def get_progress(self, task_id: str) -> TranslationProgress:
           """获取翻译进度"""
   ```

2. **TaskManager**
   - 管理翻译任务的生命周期
   - 处理断点续译逻辑
   - 跟踪翻译进度
   ```python
   class TaskManager:
       def create_task(self, epub_path: str) -> str:
           """创建翻译任务"""
           
       def save_checkpoint(self, task_id: str) -> bool:
           """保存任务断点"""
           
       def load_checkpoint(self, task_id: str) -> TaskState:
           """加载任务断点"""
           
       def update_progress(self, task_id: str, progress: ProgressInfo):
           """更新任务进度"""
   ```

3. **EPUBProcessor**
   - 复制原始EPUB文件
   - 处理EPUB文件的读写
   - 管理文件结构
   - 协调内容处理
   ```python
   class EPUBProcessor:
       def copy_epub(self, source_path: str, target_path: str) -> str:
           """复制原始EPUB文件到目标路径"""
           
       def get_content_files(self, epub_path: str) -> list[ContentFile]:
           """获取需要翻译的内容文件（HTML和目录）列表"""
           
       def update_content_file(self, epub_path: str, file_path: str, translated_content: str) -> bool:
           """更新EPUB中的内容文件"""
   ```

4. **HTMLProcessor**
   - HTML内容的预处理和后处理
   - 管理不可翻译内容
   - 计算和分割基于token的内容
   - 合并翻译后的内容
   - 重建处理后的HTML
   ```python
   class HTMLProcessor:
       def prepare_content(self, html: str) -> ProcessedContent:
           """预处理HTML内容"""
           
       def calculate_tokens(self, content: str, translation_service: TranslationService) -> int:
           """根据指定翻译服务的规则计算token数量"""
           
       def split_content(self, content: ProcessedContent, translation_service: TranslationService) -> list[HTMLSegment]:
           """根据翻译服务的token限制分割内容"""
           
       def merge_translated_segments(self, segments: list[TranslatedSegment]) -> ProcessedContent:
           """合并翻译后的片段"""
           
       def rebuild_content(self, processed: ProcessedContent) -> str:
           """重建HTML内容"""
   ```

5. **TranslationService**
   - 对接翻译API
   - 管理翻译质量
   - 处理翻译请求
   - 提供服务特定的配置
   ```python
   class TranslationService:
       def get_token_limit(self) -> int:
           """获取服务支持的最大token限制"""
           
       def get_token_calculator(self) -> TokenCalculator:
           """获取服务特定的token计算器"""
           
       def translate_content(self, content: str, source_lang: str, target_lang: str) -> str:
           """翻译内容"""
   ```

6. **TranslationConfig**
   - 配置翻译任务的参数
   - 选择翻译服务
   - 设置服务特定参数
   - 配置翻译质量偏好
   ```python
   class TranslationConfig:
       service_type: str  # 'openai', 'google', 'azure' 等
       service_config: dict  # 服务特定的配置参数
       source_lang: str
       target_lang: str
       quality_preference: str  # 'speed', 'quality', 'balanced'
       retry_strategy: RetryStrategy
       timeout: int
   ```

7. **TranslationServiceFactory**
   - 管理和创建翻译服务实例
   - 处理服务配置
   ```python
   class TranslationServiceFactory:
       _services: dict[str, type[TranslationService]] = {
           "openai": OpenAITranslationService,
           "mistral": MistralTranslationService,
           "google": GoogleTranslationService
       }
       
       @classmethod
       def create_service(
           cls,
           service_type: str,
           config: TranslationServiceConfig
       ) -> TranslationService:
           """根据配置创建对应的翻译服务实例"""
   ```

8. **具体翻译服务实现**
   ```python
   class OpenAITranslationService(TranslationService):
       def __init__(self, config: dict):
           self.model = config.get('model', 'gpt-3.5-turbo')
           self.api_key = config['api_key']
           # 其他OpenAI特定配置
           
   class GoogleTranslationService(TranslationService):
       def __init__(self, config: dict):
           self.project_id = config['project_id']
           self.credentials = config['credentials']
           # 其他Google特定配置
           
   class AzureTranslationService(TranslationService):
       def __init__(self, config: dict):
           self.region = config['region']
           self.api_key = config['api_key']
           # 其他Azure特定配置
   ```

#### 4.1.3 调用流程

1. **初始化翻译任务**
   ```
   用户 → EPUBTranslator.translate_book(config)
     ↓
   TranslationServiceFactory.create_service(config)
     ↓
   TaskManager.create_task()
     ↓
   EPUBProcessor.copy_epub()
     ↓
   EPUBProcessor.get_content_files()
   ```

2. **内容处理流程**
   ```
   对每个内容文件:
   ContentFile → HTMLProcessor.prepare_content()
              → HTMLProcessor.calculate_tokens(translation_service)
              → HTMLProcessor.split_content(translation_service)
              → TranslationService.translate_content()
              → HTMLProcessor.merge_translated_segments()
              → HTMLProcessor.rebuild_content()
              → EPUBProcessor.update_content_file()
   ```

3. **进度跟踪**
   ```
   各处理组件 → TaskManager.update_progress()
              → TaskManager.save_checkpoint()
   ```

4. **断点续译**
   ```
   用户 → EPUBTranslator.resume_translation()
     ↓
   TaskManager.load_checkpoint()
     ↓
   继续未完成的处理流程
   ```

#### 4.1.4 关键接口说明

1. **任务管理接口**
   - 创建任务时返回唯一的task_id
   - 支持查询任务状态和进度
   - 提供断点续译的能力

2. **翻译配置接口**
   - 选择翻译服务类型
   - 配置服务特定参数
   - 设置翻译质量偏好
   - 配置重试策略

3. **翻译服务接口**
   - 提供服务特定的token限制
   - 提供服务特定的token计算规则
   - 处理实际的翻译请求
   - 提供服务信息和状态

4. **内容处理接口**
   - HTML预处理：提取和保护特殊内容
   - Token计算：基于翻译服务的规则计算
   - 内容分割：根据服务限制智能分割
   - 内容合并：保持HTML结构的合并
   - 结构重建：还原完整的HTML文档

## 5. 测试要求

### 5.1 单元测试
- HTML 处理测试
- Token 计算测试
- 内容分割测试
- 翻译流程测试

### 5.2 集成测试
- 完整流程测试
- 边界条件测试
- 错误处理测试
- 性能测试

### 5.3 测试覆盖
- 代码覆盖率 > 90%
- 关键路径测试
- 异常场景测试
- 并发测试

## 6. 文档要求

### 6.1 技术文档
- 详细的 API 文档
- 架构设计文档
- 部署说明文档
- 测试报告

### 6.2 用户文档
- 使用说明
- 配置指南
- 常见问题解答
- 故障排除指南

## 5. 翻译服务设计

### 5.1 系统概述

1. **设计目标**
   - 支持多种翻译服务提供商
   - 统一的翻译接口
   - 灵活的配置管理
   - 可扩展的服务架构

2. **支持的服务**
   - OpenAI API
   - Mistral API
   - Google Translate API

### 5.2 配置管理

#### 5.2.1 基础配置结构
```python
@dataclass
class TranslationServiceConfig:
    """翻译服务基础配置"""
    service_type: str
    model_name: str
    timeout: int = 30
    retry_count: int = 3
    concurrent_requests: int = 5
    batch_size: int = 10

class OpenAIServiceConfig(TranslationServiceConfig):
    """OpenAI服务配置"""
    base_url: str
    api_key: str
    organization_id: str | None = None
    model_name: str = "gpt-3.5-turbo"
    temperature: float = 0.3
    max_tokens: int = 2000

class MistralServiceConfig(TranslationServiceConfig):
    """Mistral服务配置"""
    api_key: str
    model_name: str = "mistral-medium"
    temperature: float = 0.3
    max_tokens: int = 2000

class GoogleServiceConfig(TranslationServiceConfig):
    """Google翻译服务配置"""
    api_key: str
    project_id: str
    location: str = "global"
```

#### 5.2.2 环境变量配置
```python
class TranslationSettings(BaseSettings):
    """翻译服务环境变量配置"""
    # OpenAI
    OPENAI_API_KEY: str | None = None
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_ORG_ID: str | None = None
    OPENAI_MODEL: str = "gpt-3.5-turbo"
    
    # Mistral
    MISTRAL_API_KEY: str | None = None
    MISTRAL_MODEL: str = "mistral-medium"
    
    # Google
    GOOGLE_API_KEY: str | None = None
    GOOGLE_PROJECT_ID: str | None = None
    GOOGLE_LOCATION: str = "global"
    
    # 通用配置
    DEFAULT_SERVICE: str = "google"
    TIMEOUT_SECONDS: int = 30
    MAX_RETRY_COUNT: int = 3
    CONCURRENT_REQUESTS: int = 5
    BATCH_SIZE: int = 10
    
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True
    )
```

### 5.3 服务接口设计

#### 5.3.1 基础接口
```python
class TranslationService(ABC):
    """翻译服务抽象基类"""
    
    @abstractmethod
    async def translate_text(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        **kwargs
    ) -> str:
        """翻译单个文本"""
        pass
        
    @abstractmethod
    async def translate_batch(
        self,
        texts: list[str],
        source_lang: str,
        target_lang: str,
        **kwargs
    ) -> list[str]:
        """批量翻译文本"""
        pass
        
    @abstractmethod
    async def check_service(self) -> bool:
        """检查服务可用性"""
        pass
```

#### 5.3.2 OpenAI实现
```python
class OpenAITranslationService(TranslationService):
    """OpenAI翻译服务实现"""
    
    def __init__(self, config: OpenAIServiceConfig):
        self.config = config
        self.system_prompt = """
        You are a professional translator.
        Translate the text while preserving its original meaning and style.
        Maintain any markdown or HTML formatting in the text.
        Do not add any explanations or notes.
        """
        
    async def translate_text(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        **kwargs
    ) -> str:
        """使用OpenAI API翻译文本"""
        # 实现细节
```

#### 5.3.3 Mistral实现
```python
class MistralTranslationService(TranslationService):
    """Mistral翻译服务实现"""
    
    def __init__(self, config: MistralServiceConfig):
        self.config = config
        self.system_prompt = """
        You are a professional translator.
        Translate the text while preserving its original meaning and style.
        Maintain any markdown or HTML formatting in the text.
        Do not add any explanations or notes.
        """
        
    async def translate_text(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        **kwargs
    ) -> str:
        """使用Mistral API翻译文本"""
        # 实现细节
```

#### 5.3.4 Google实现
```python
class GoogleTranslationService(TranslationService):
    """Google翻译服务实现"""
    
    def __init__(self, config: GoogleServiceConfig):
        self.config = config
        
    async def translate_text(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        **kwargs
    ) -> str:
        """使用Google Translate API翻译文本"""
        # 实现细节
```

### 5.4 服务工厂

```python
class TranslationServiceFactory:
    """翻译服务工厂"""
    
    _services: dict[str, type[TranslationService]] = {
        "openai": OpenAITranslationService,
        "mistral": MistralTranslationService,
        "google": GoogleTranslationService
    }
    
    @classmethod
    def create_service(
        cls,
        service_type: str,
        config: TranslationServiceConfig
    ) -> TranslationService:
        """根据配置创建对应的翻译服务实例"""
        if service_type not in cls._services:
            raise ValueError(f"Unsupported service type: {service_type}")
            
        service_class = cls._services[service_type]
        return service_class(config)
```

### 5.5 错误处理

```python
class TranslationError(Exception):
    """翻译错误基类"""
    pass

class ServiceConfigError(TranslationError):
    """服务配置错误"""
    pass

class ServiceUnavailableError(TranslationError):
    """服务不可用错误"""
    pass

class TranslationRequestError(TranslationError):
    """翻译请求错误"""
    pass

class TranslationLimitError(TranslationError):
    """翻译限制错误"""
    pass
