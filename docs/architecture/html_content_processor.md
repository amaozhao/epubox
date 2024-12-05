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
   - 当内容超过限制时递归处理子节点
   - 确保处理的内容块不超过API限制

## 3. 详细设计

### 3.1 不需要翻译的标签

```python
SKIP_TAGS = {
    # 脚本和样式
    'script', 'style',
    
    # 代码相关
    'code', 'pre', 'kbd', 'var', 'samp',
    
    # 特殊内容
    'svg', 'math', 'canvas',
    
    # 多媒体标签
    'img', 'audio', 'video', 'track', 'source',
    
    # 表单相关
    'input', 'button', 'select', 'option', 'textarea', 'form',
    
    # 元数据和链接
    'meta', 'link', 'a',
    
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

这些标签的内容将被完整保留，不进行翻译处理。对于这些标签：
- 保持原始内容不变
- 保持原始样式和属性
- 在翻译过程中使用占位符替换整个标签内容

### 3.2 占位符设计

1. **格式**
   ```
   †n†
   ```
   其中 n 为序号，例如：†0†, †1†, †2†

2. **特点**
   - 使用ASCII特殊字符 † 作为边界
   - 在普通文本中出现概率极低
   - 不太可能被翻译API误处理
   - 具有良好的编码兼容性
   - 视觉上容易识别
   - 最小化占用空间

3. **示例**
   ```python
   class HTMLContentProcessor:
       def __init__(self, max_chunk_size=4500):
           self.max_chunk_size = max_chunk_size
           self.placeholder_counter = 0
           self.placeholders = {}
           
       def _create_placeholder(self, content):
           """创建简化的占位符"""
           placeholder = f"†{self.placeholder_counter}†"
           self.placeholders[placeholder] = content
           self.placeholder_counter += 1
           return placeholder
           
       def restore_content(self, translated_text):
           """还原占位符内容"""
           result = translated_text
           # 使用简单的正则表达式匹配占位符
           pattern = r'†(\d+)†'
           
           for match in re.finditer(pattern, result):
               placeholder = match.group(0)
               if placeholder in self.placeholders:
                   result = result.replace(placeholder, self.placeholders[placeholder])
                   
           return result
   ```

### 3.3 处理流程

```
1. 输入HTML内容
   ↓
2. 替换不需要翻译的内容为占位符
   ↓
3. 递归处理HTML节点
   |
   ├── 检查节点内容大小
   |   ├── 如果在限制内：创建翻译任务
   |   └── 如果超过限制：递归处理子节点
   ↓
4. 生成翻译任务列表
   ↓
5. 翻译完成后还原占位符
```

### 3.4 核心组件

#### 3.4.1 HTMLContentProcessor 类

```python
class HTMLContentProcessor:
    def __init__(self, max_chunk_size=4500):
        self.max_chunk_size = max_chunk_size
        self.placeholder_counter = 0
        self.placeholders = {}
```

主要方法：
- `process_html(soup)`: 处理HTML内容
- `_create_placeholder(content)`: 创建占位符
- `_replace_skip_content(node)`: 替换不需要翻译的内容
- `_process_node(node, tasks)`: 递归处理节点
- `restore_content(translated_text)`: 还原占位符

## 4. 数据结构

### 4.1 翻译任务结构

```python
{
    'content': str,  # 需要翻译的内容
    'node': BeautifulSoup.Tag  # 对应的HTML节点
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

2. **内容大小控制**
   - 处理超大的HTML节点
   - 确保不超过API限制

3. **占位符冲突**
   - 确保占位符的唯一性
   - 处理可能的冲突情况

## 6. 性能考虑

1. **内存使用**
   - 使用递归方式处理大文件
   - 及时释放不需要的内容

2. **处理效率**
   - 优化递归处理逻辑
   - 减少不必要的字符串操作

## 7. 限制和约束

1. **API限制**
   - 默认最大块大小为4500字符
   - 可根据具体API调整限制

2. **HTML结构**
   - 依赖于有效的HTML结构
   - 需要正确的标签嵌套

## 8. 未来优化方向

1. **占位符机制**
   - 可能需要更安全的占位符设计
   - 考虑更多的特殊情况

2. **分割策略**
   - 优化节点分割算法
   - 提高处理效率

3. **内容处理**
   - 添加更多的内容处理规则
   - 支持更复杂的HTML结构

## 9. 使用示例

```python
# 创建处理器实例
processor = HTMLContentProcessor(max_chunk_size=4500)

# 处理HTML内容
soup = BeautifulSoup(html_content, 'html.parser')
tasks, placeholders = processor.process_html(soup)

# 处理翻译任务
for task in tasks:
    translated_content = translate_service.translate(task['content'])
    task['translated_content'] = translated_content

# 还原占位符
final_content = processor.restore_content(translated_content)
