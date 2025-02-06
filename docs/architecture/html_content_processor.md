# EPUB HTML内容处理设计文档

## 1. 概述

本文档描述了EPUB电子书HTML内容处理的设计方案。该方案基于树结构进行内容提取和分割，确保翻译内容不超过API限制的同时保持文档结构的完整性。

## 2. 核心设计

### 2.1 基本原则

1. **基于树结构**
   - 将HTML文档转换为树形结构进行处理
   - 使用节点类型区分叶节点和非叶节点
   - 保持文档结构的完整性和层次关系

2. **翻译一致性**
   - 维护特殊词汇的翻译映射
   - 确保专业术语翻译的一致性
   - 支持自定义翻译词典

3. **递归处理**
   - 从根节点开始递归构建树结构
   - 合并相邻的文本节点
   - 对叶节点进行翻译处理

## 3. 详细设计

### 3.1 不需要翻译的标签

```python
SKIP_TAGS = {
    # 脚本和样式
    'script', 'style',
    
    # 代码相关
    'code', 'pre', 'kbd', 'var', 'samp',
    
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
2. 初始化处理器
   |
   ├── 创建翻译提供者（TranslatorProvider）
   ├── 设置源语言和目标语言
   ├── 初始化跳过标签集合
   ↓
3. 构建树结构
   |
   ├── 创建根节点
   ├── 解析HTML内容（BeautifulSoup）
   ├── 替换需要跳过的标签
   ├── 递归遍历HTML节点
   |   ├── 收集并合并可合并的节点
   |   └── 构建树节点关系
   ↓
4. 翻译处理
   |
   ├── 递归遍历树节点
   |   ├── 对叶节点进行翻译
   |   └── 保持非叶节点的结构
   ↓
5. 重建HTML内容
   |
   ├── 递归处理树节点
   ├── 还原HTML结构
   └── 输出处理后的内容
```

### 3.3 核心组件

#### 3.3.1 TreeNode 类

表示树结构中的节点：

```python
class TreeNode:
    def __init__(self, node_type: str, content: str, token_count: int, parent: Optional['TreeNode'] = None):
        self.node_type: str = node_type  # 节点类型：leaf 或 non-leaf
        self.content: str = content      # 节点内容
        self.token_count: int = token_count  # token 数量
        self.parent: Optional[TreeNode] = parent  # 父节点
        self.children: list[TreeNode] = []  # 子节点列表
        self.translated: Optional[str] = None  # 翻译后的内容
```

#### 3.3.2 TranslatorProvider 类

提供翻译服务和词汇映射：

```python
class TranslatorProvider:
    def __init__(self, limit_value):
        self.limit_value = limit_value
        self.translations = {
            # 预定义的翻译映射
            "Preface": "前言",
            "Chapter": "章节",
            # ... 更多映射
        }
    
    async def translate(self, content: str, source_lang: str, target_lang: str) -> str:
        # 实现翻译逻辑
        pass
```

#### 3.3.3 TreeProcessor 类

处理HTML内容的主要类：

主要方法：
- `process(content: str, parser='html.parser')`: 处理HTML内容
- `_traverse(node, parent: Optional[TreeNode])`: 递归遍历节点
- `_collect_mergeable_nodes(node)`: 收集可合并的节点
- `_translate_nodes(node: TreeNode)`: 翻译节点
- `restore_html(node: TreeNode, parser: str)`: 重建HTML内容

## 4. 数据结构

### 4.1 树节点结构

```python
{
    'node_type': str,      # 节点类型（'leaf' 或 'non-leaf'）
    'content': str,        # 节点内容
    'token_count': int,    # token 数量
    'parent': TreeNode,    # 父节点引用
    'children': list,      # 子节点列表
    'translated': str      # 翻译后的内容（仅叶节点）
}
```

### 4.2 翻译映射结构

```python
{
    'Preface': '前言',
    'Chapter': '章节',
    'FastAPI': 'FastAPI',
    'Python': 'Python',
    # ... 更多映射
}
```

## 5. 错误处理

1. **HTML解析错误**
   - 验证HTML结构的完整性
   - 处理无效的HTML标签
   - 支持多种HTML解析器（html.parser, lxml）

2. **节点处理**
   - 正确识别和处理不同类型的节点
   - 维护树结构的完整性
   - 确保节点关系的正确性

3. **翻译处理**
   - 处理翻译失败的情况
   - 保持专业术语的一致性
   - 处理特殊字符和HTML实体

## 6. 性能考虑

1. **树结构优化**
   - 合理合并相邻文本节点
   - 避免过度分割树结构
   - 优化节点存储和访问

2. **翻译优化**
   - 使用预定义的翻译映射
   - 避免重复翻译相同内容
   - 优化token计算方法

3. **内存管理**
   - 及时清理不需要的节点
   - 优化树结构的内存占用
   - 避免创建过多的临时对象

## 7. 限制和约束

1. **HTML兼容性**
   - 支持标准HTML5标签
   - 保持EPUB特有标签的处理
   - 确保输出的HTML有效性

2. **翻译限制**
   - 遵守翻译API的限制
   - 保持专业术语的准确性
   - 维护文档的可读性
