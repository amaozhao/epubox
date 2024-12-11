# EPUB HTML内容处理设计文档

## 1. 概述

本文档描述了EPUB电子书HTML内容处理的设计方案。该方案基于HTML结构进行内容提取和分割，确保翻译内容不超过API限制的同时保持文档结构的完整性。

## 2. 核心设计

### 2.1 基本原则

1. **基于HTML结构**
   - 使用HTML标签作为内容分割的基本单位
   - 保持文档结构的完整性
   - 避免破坏HTML标签的语义

2. **占位符机制**
   - 使用特殊字符组合作为占位符
   - 确保占位符不会被翻译API误处理
   - 维护占位符和原始内容的映射关系

3. **递归处理**
   - 从根节点开始递归处理HTML内容
   - 优先处理需要跳过的标签，整体替换为占位符
   - 对剩余内容进行翻译任务划分

## 3. 详细设计

### 3.1 不需要翻译的标签

```python
SKIP_TAGS = {
    # 脚本和样式
    'script', 'style',
    
    # 代码相关
    'code', 'kbd', 'var', 'samp',
    
    # 特殊内容
    'svg', 'math', 'canvas', 'address', 'applet',
    
    # 多媒体标签
    'img', 'audio', 'video', 'track', 'source',
    
    # 表单相关
    'input', 'button', 'select', 'option', 'textarea', 'form',
    
    # 元数据和链接
    'meta', 'link',
    
    # 嵌入内容
    'iframe', 'embed', 'object', 'param',
    
    # 技术标记
    'time', 'data', 'meter', 'progress',
    
    # XML相关
    'xml', 'xmlns',
    
    # EPUB特有标签
    'epub:switch', 'epub:case', 'epub:default',
    
    # 注释标签
    'annotation', 'note'
}
```

### 3.2 处理流程

```
1. 输入HTML内容
   ↓
2. 初始化和清理
   |
   ├── 设置最大块大小 (max_chunk_size，默认4500 tokens)
   ├── 清理占位符计数器和映射
   ↓
3. 第一阶段：内容保护
   |
   ├── 从根节点开始递归处理
   |   ├── 对于skip标签：调用_handle_skip_tag替换为占位符
   |   ├── 对于文本节点：调用_handle_text_node直接翻译
   |   └── 对于其他节点：检查token数量
   ↓
4. 第二阶段：内容翻译
   |
   ├── 检查节点大小
   |   ├── 如果小于等于max_chunk_size：直接翻译整个节点
   |   └── 如果大于max_chunk_size：
   |       ├── 收集子节点信息
   |       └── 递归处理子节点组
   ↓
5. 还原占位符内容
   |
   ├── 清理翻译结果中的额外文本（如"翻译："等前缀）
   ├── 使用正则表达式匹配占位符
   └── 替换为原始内容
```

### 3.3 节点处理策略

1. **Skip标签处理**
   - 直接替换为占位符
   - 保留原始内容以供还原

2. **文本节点处理**
   - 直接调用翻译API
   - 保持文本节点的独立性

3. **普通节点处理**
   - 检查节点的token数量
   - 如果在限制范围内，保持节点完整性，直接翻译
   - 如果超出限制，才拆分处理子节点

4. **子节点组处理**
   - 收集所有子节点信息
   - 尝试合并处理符合大小限制的子节点
   - 对超出限制的子节点递归处理

### 3.3 核心组件

#### 3.3.1 HTMLProcessor 类

主要方法：
- `process(html_content, parser="html.parser")`: 处理HTML内容
- `replace_skip_tags_recursive(node)`: 递归处理和替换skip标签
- `create_placeholder(content)`: 创建占位符
- `process_node(node)`: 递归处理待翻译内容
- `restore_content(translated_text)`: 还原占位符内容

内部方法：
- `_handle_skip_tag(node)`: 处理需要跳过的标签
- `_handle_text_node(node)`: 处理纯文本节点
- `_translate_node_directly(node, content)`: 直接翻译整个节点
- `_collect_child_info(node)`: 收集节点的子节点信息
- `_process_child_groups(child_info)`: 处理子节点分组
- `_translate_group(nodes)`: 翻译节点组

## 4. 数据结构

### 4.1 翻译任务结构

```python
{
    'content': str,  # 需要翻译的内容
    'node': NavigableString  # 对应的文本节点
}
```

### 4.2 占位符映射结构

```python
{
    '†0†': '原始内容1',
    '†1†': '原始内容2',
    ...
}
```

## 5. 错误处理

1. **HTML解析错误**
   - 验证HTML结构的完整性
   - 处理无效的HTML标签
   - 支持多种HTML解析器（html.parser, lxml）

2. **节点处理**
   - 正确识别和处理不同类型的节点（Tag, NavigableString）
   - 维护节点的父子关系
   - 确保节点替换操作的安全性

3. **翻译结果处理**
   - 清理翻译结果中的额外文本
   - 处理翻译API返回的HTML转义字符
   - 处理节点数量不匹配的情况

4. **大小控制**
   - 准确计算节点的token数量
   - 确保不超过翻译API的限制
   - 合理拆分过大的节点

## 6. 性能考虑

1. **节点处理策略**
   - 优先处理整体节点，减少不必要的拆分
   - 只在节点超过限制时才进行递归处理
   - 避免过度分割导致的上下文丢失

2. **翻译优化**
   - 使用信号量控制并发翻译请求
   - 减少不必要的API调用
   - 保持翻译块的合理大小

3. **内存管理**
   - 及时清理不需要的节点
   - 优化占位符存储结构
   - 避免重复创建BeautifulSoup对象

## 7. 限制和约束

1. **HTML兼容性**
   - 支持标准HTML5标签
   - 处理EPUB特有标签
   - 支持多种HTML解析器

2. **翻译限制**
   - 单个翻译块不超过max_chunk_size（默认4500 tokens）
   - 保持HTML标签的完整性
   - 维护文档结构的层次关系

3. **占位符处理**
   - 使用特殊字符组合（†数字†）作为占位符
   - 确保占位符不会被翻译API误处理
   - 维护占位符和原始内容的映射关系
