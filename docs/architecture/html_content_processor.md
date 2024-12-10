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
2. 第一阶段：内容保护
   |
   ├── 从根节点开始递归查找skip标签
   |   ├── 找到skip标签：将整个标签及其内容替换为占位符
   |   └── 不是skip标签：继续递归处理子节点
   ↓
3. 第二阶段：内容翻译
   |
   ├── 检查节点内容和大小
   |   ├── 符合大小限制：直接翻译
   |   └── 超过限制：递归处理子节点
   ↓
4. 还原占位符内容
   |
   ├── 使用正则表达式匹配占位符
   └── 替换为原始内容
```

### 3.3 核心组件

#### 3.3.1 HTMLProcessor 类

主要方法：
- `process(html_content, parser="html.parser")`: 处理HTML内容
- `replace_skip_tags_recursive(node)`: 递归处理和替换skip标签
- `create_placeholder(content)`: 创建占位符
- `process_node(node)`: 递归处理待翻译内容
- `restore_content(translated_text)`: 还原占位符

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

2. **内容处理**
   - 确保所有skip标签被正确替换
   - 保持文档结构完整性

3. **占位符处理**
   - 确保占位符的唯一性
   - 正确还原所有占位符内容

## 6. 性能考虑

1. **内存使用**
   - 及时清理不需要的节点
   - 优化占位符存储

2. **处理效率**
   - 优先处理skip标签减少后续处理量
   - 避免重复遍历文档

## 7. 限制和约束

1. **HTML兼容性**
   - 支持标准HTML5标签
   - 处理EPUB特有标签

2. **文档完整性**
   - 确保不破坏文档结构
   - 保持标签属性完整
