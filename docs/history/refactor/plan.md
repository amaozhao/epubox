# EPUB 翻译系统重构文档

## 目录

1. [背景与问题](#背景与问题)
2. [架构总览](#架构总览)
3. [数据模型](#数据模型)
4. [核心组件](#核心组件)
5. [完整流程（闭环）](#完整流程闭环)
6. [分块策略](#分块策略)
7. [合并恢复策略](#合并恢复策略)
8. [翻译工作流](#翻译工作流)
9. [断点续传](#断点续传)
10. [手动翻译支持](#手动翻译支持)
11. [验证策略](#验证策略)
12. [实施计划](#实施计划)

---

## 背景与问题

### 旧架构的核心错误

旧架构使用 TagPreserver 将**所有 HTML 标签**替换为 `[idN]` 占位符，LLM 只看到纯文本 + 占位符。这个设计导致了一系列问题：

1. **占位符数量爆炸**：一个普通段落 `<p>Hello <em>World</em></p>` 就产生 3 个占位符，一个章节上百个占位符
2. **占位符被 LLM 破坏**：LLM 频繁丢失、调换、重复占位符
3. **max_placeholders_per_chunk 成为瓶颈**：为避免占位符过多，强制限制每个 chunk 最多 15 个占位符，导致 token 利用率仅 ~30%
4. **Chunk 标签不完整**：TagPreserver 合并相邻标签（如 `</p>\n<p>` → `[id2]`），导致在任何位置切割都无法保证完整闭合标签
5. **复杂的索引管理**：全局索引 → 局部索引 → 恢复全局索引，三层映射增加了大量复杂度和 bug 面

### 核心认知纠正

**LLM 完全有能力处理 HTML 标签。** 现代 LLM 可以直接翻译带 HTML 标签的文本，保持标签结构不变。不需要用占位符"保护"普通 HTML 标签。

唯一需要占位符保护的是**完全不需要翻译的内容**：`<pre>`、`<code>`、`<style>` 等代码/样式块。

---

## 架构总览

### 新架构原则

1. **LLM 直接看原始 HTML**：chunk 内容就是原始 HTML 片段（如 `<p>Hello <em>World</em></p>`），不做标签替换
2. **只保护不可翻译内容**：仅 PreCodeExtractor 将 `<pre>`/`<code>`/`<style>` 替换为 `[PRE:N]`/`[CODE:N]`/`[STYLE:N]`，且命中后按原子块整体替换，不再递归进入其子树
3. **DOM 级别切割**：以完整 DOM 元素为最小单位切割，保证每个 chunk 内标签完整闭合
4. **xpath 定位**：每个 chunk 记录其包含元素的 xpath，用于翻译后精确恢复到原文位置
5. **贪心合并**：将多个完整元素打包到一个 chunk，最大化 token 利用率

### 整体流程

```
┌──────────────────────────────────────────────────────────┐
│                     Parser.parse()                        │
├──────────────────────────────────────────────────────────┤
│  1. extract() — 解压 EPUB                                │
│  2. 遍历所有 XHTML/HTML/XML/NCX 文件                      │
│  3. BeautifulSoup 规范化                                  │
│  4. PreCodeExtractor.extract()                            │
│     → 替换 pre/code/style 为 [PRE:N]/[CODE:N]/[STYLE:N]  │
│  5. DomChunker.chunk()                                    │
│     → DOM 解析 → 收集可翻译块元素（含 title）→ 贪心合并   │
│     → 导航文件与内嵌目录型 <nav> 走 nav_text 特殊分块      │
│     → 每个 chunk = 完整闭合 HTML + xpath 列表              │
│  6. save_json() — 持久化（支持断点续传）                   │
│     → 旧 checkpoint 会自动迁移导航/目录块到 nav_text      │
└──────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│                 Orchestrator.translate_epub()              │
├──────────────────────────────────────────────────────────┤
│  遍历 item → 遍历 chunk:                                 │
│    ├─ COMPLETED → 跳过                                    │
│    ├─ TRANSLATED（手动翻译后重新运行）→ 进入校对步骤       │
│    └─ PENDING → TranslatorWorkflow:                       │
│         Step 1: translate_step()                          │
│           → 发送原始 HTML 给 LLM                          │
│           → 验证返回的 HTML 结构完整性                     │
│           → 失败重试，最终失败标记 UNTRANSLATED            │
│         Step 2: proofread_step()                          │
│           → 校对翻译质量                                  │
│         Step 3: apply_corrections_step()                  │
│           → 应用校对建议 → 标记 COMPLETED                 │
│    保存 JSON（每个 chunk 翻译后立即保存）                  │
│                                                           │
│  Replacer.restore(item)                                   │
│    → 按 xpath 将翻译结果恢复到原始 DOM                    │
│    → 恢复 [PRE:N]/[CODE:N]/[STYLE:N] 占位符              │
│    → 验证最终 HTML 结构完整性                              │
│    → 写入文件                                             │
└──────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│                      Builder.build()                      │
├──────────────────────────────────────────────────────────┤
│  1. 设置 OPF/XHTML 中文语言                               │
│  2. 可重排书使用设备字体，安全剪枝未引用字体              │
│  3. 特殊/不确定字体 fail-closed 保留                      │
│  4. 打包为 EPUB 文件                                      │
└──────────────────────────────────────────────────────────┘
```

---

## 数据模型

### Chunk

```python
class Chunk(BaseModel):
    name: str                           # 唯一标识（UUID 前 8 位）
    original: str                       # 原始 HTML 片段（完整闭合标签）
    translated: Optional[str] = None    # 翻译后的 HTML 片段
    status: TranslationStatus = PENDING # 翻译状态
    tokens: int = 0                     # token 数估算
    xpaths: List[str] = []             # chunk 内各元素在原始 DOM 中的 xpath
```

**注意**：不再需要 `global_indices`、`local_tag_map` 字段。

### Block（内部数据结构）

```python
from typing import NamedTuple

class Block(NamedTuple):
    html: str       # 元素的 HTML 字符串
    tokens: int     # token 数估算
    xpath: str      # 元素在 DOM 中的路径（如 /html/body/p[2]）
```

`Block` 是 DomChunker 内部使用的中间数据结构，不持久化。用于在 `_collect_blocks()` 和 `_greedy_merge()` 之间传递数据。

### EpubItem

```python
class EpubItem(BaseModel):
    id: str                                         # 文件相对路径
    path: str                                       # 文件绝对路径
    content: str                                    # 原始完整 HTML
    translated: Optional[str] = None                # 翻译后完整 HTML
    chunks: Optional[List[Chunk]] = None            # 分块列表
    preserved_pre: Optional[List[str]] = None       # PreCodeExtractor 保存的 pre 标签
    preserved_code: Optional[List[str]] = None      # PreCodeExtractor 保存的 code 标签
    preserved_style: Optional[List[str]] = None     # PreCodeExtractor 保存的 style 标签
```

**注意**：不再需要 `wrapper_prefix`/`wrapper_suffix` 字段。恢复时直接解析 `item.content`（原始完整 HTML）并按 xpath 替换，结构外壳（`<html>`/`<head>`/`<body>` 等）自然保留在原始 DOM 中，无需单独存储。

### TranslationStatus

```python
class TranslationStatus(str, Enum):
    PENDING = "pending"             # 待翻译
    TRANSLATED = "translated"       # 已翻译（待校对）
    COMPLETED = "completed"         # 已完成（翻译+校对）
    UNTRANSLATED = "untranslated"   # 翻译失败，保留原文（可手动翻译）
```

精简状态枚举，去掉不再需要的 `IN_PROGRESS`、`FAILED`、`SKIPPED`。

---

## 核心组件

### 组件总览

```
engine/item/
├── precode.py          # PreCodeExtractor（保留，不变）
├── chunker.py          # DomChunker（重写）
├── merger.py           # Merger（重写为 xpath 恢复）
└── __init__.py

engine/epub/
├── parser.py           # Parser（修改调用流程）
├── replacer.py         # Replacer（简化）
└── builder.py          # Builder（不变）

engine/agents/
├── translator.py       # 翻译 Agent（简化 prompt）
├── proofer.py          # 校对 Agent（不变）
├── workflow.py         # 翻译工作流（简化）
└── validator.py        # HTML 结构验证（重写）
```

### 需要删除的组件

- `engine/item/tag/preserve.py` — TagPreserver
- `engine/item/tag/restore.py` — TagRestorer
- `engine/item/tag/__init__.py`
- `engine/item/placeholder.py` — PlaceholderManager
- `engine/item/renumberer.py` — Renumberer
- `engine/agents/aligner.py` — Token Alignment（不再需要，因为没有占位符对齐问题）

---

## 完整流程（闭环）

以一个具体的 EPUB 文件为例，完整展示从切分到恢复的闭环。

### 输入

```html
<html>
<head><title>Book</title><style>.cls { color: red; }</style></head>
<body>
  <h1>Chapter 1: Introduction</h1>
  <p>This is the first paragraph.</p>
  <p>This is the <em>second</em> paragraph.</p>
  <pre>function hello() { return "world"; }</pre>
  <p>This is the third paragraph.</p>
  <img src="fig.png" alt="Figure 1"/>
  <p>This is the fourth paragraph.</p>
</body>
</html>
```

### Step 1: PreCodeExtractor

替换不可翻译的代码/样式块：

```html
<html>
<head><title>Book</title>[STYLE:0]</head>
<body>
  <h1>Chapter 1: Introduction</h1>
  <p>This is the first paragraph.</p>
  <p>This is the <em>second</em> paragraph.</p>
  [PRE:0]
  <p>This is the third paragraph.</p>
  <img src="fig.png" alt="Figure 1"/>
  <p>This is the fourth paragraph.</p>
</body>
</html>
```

保存映射：
```
preserved_style[0] = '<style>.cls { color: red; }</style>'
preserved_pre[0]   = '<pre>function hello() { return "world"; }</pre>'
```

说明：
- `pre/code/style` 命中后立即整体替换为占位符
- 嵌套内容不会再单独生成内部占位符；例如 `<pre><code>nested</code></pre>` 只会得到 `[PRE:0]`

### Step 2: DomChunker — 收集可翻译块

DomChunker 解析 DOM，收集 `<head>` 中的 `<title>` 和 `<body>` 中的可翻译块元素：

| 来源 | 索引 | 元素 | xpath | tokens | 需要翻译 |
|------|------|------|-------|--------|---------|
| head | 0 | `<title>Book</title>` | `/html/head/title` | ~4 | ✓ |
| body | 1 | `<h1>Chapter 1: Introduction</h1>` | `/html/body/h1` | ~12 | ✓ |
| body | 2 | `<p>This is the first paragraph.</p>` | `/html/body/p[1]` | ~12 | ✓ |
| body | 3 | `<p>This is the <em>second</em> paragraph.</p>` | `/html/body/p[2]` | ~15 | ✓ |
| body | - | `[PRE:0]`（文本节点） | — | — | ✗ 纯占位符，跳过 |
| body | 4 | `<p>This is the third paragraph.</p>` | `/html/body/p[3]` | ~12 | ✓ |
| body | - | `<img src="fig.png" alt="Figure 1"/>` | — | — | ✗ SKIP_TAGS，跳过 |
| body | 5 | `<p>This is the fourth paragraph.</p>` | `/html/body/p[4]` | ~12 | ✓ |

### Step 3: DomChunker — 贪心合并

假设 `token_limit = 50`，贪心合并可翻译元素：

**Chunk 1**（tokens ≈ 43，xpath 列表 4 个）：
```html
<title>Book</title>
<h1>Chapter 1: Introduction</h1>
<p>This is the first paragraph.</p>
<p>This is the <em>second</em> paragraph.</p>
```
```python
xpaths = ["/html/head/title", "/html/body/h1", "/html/body/p[1]", "/html/body/p[2]"]
```

**Chunk 2**（tokens ≈ 24，xpath 列表 2 个）：
```html
<p>This is the third paragraph.</p>
<p>This is the fourth paragraph.</p>
```
```python
xpaths = ["/html/body/p[3]", "/html/body/p[4]"]
```

不可翻译元素（`[PRE:0]`、`<img/>`）**不进入 chunk**，保留在原始 DOM 中不动。

### Step 4: 翻译

LLM 收到 Chunk 1 的原始 HTML：
```html
<title>Book</title>
<h1>Chapter 1: Introduction</h1>
<p>This is the first paragraph.</p>
<p>This is the <em>second</em> paragraph.</p>
```

LLM 返回：
```html
<title>书籍</title>
<h1>第一章：简介</h1>
<p>这是第一个段落。</p>
<p>这是<em>第二个</em>段落。</p>
```

标签完整，结构不变。

### Step 5: 校对 + 应用校对

校对器检查翻译质量，应用修正建议。状态变为 COMPLETED。

### Step 6: 合并恢复

解析原始 HTML 的 DOM 树，按 xpath 逐个替换：

```
/html/head/title → <title>书籍</title>
/html/body/h1    → <h1>第一章：简介</h1>
/html/body/p[1]  → <p>这是第一个段落。</p>
/html/body/p[2]  → <p>这是<em>第二个</em>段落。</p>
/html/body/p[3]  → <p>这是第三个段落。</p>
/html/body/p[4]  → <p>这是第四个段落。</p>
```

`[PRE:0]`、`<img/>` 不动。`<html>`/`<head>`/`<body>` 等结构标签自然保留在原始 DOM 中。

### Step 7: 恢复 PreCodeExtractor 占位符

```
[STYLE:0] → <style>.cls { color: red; }</style>
[PRE:0]   → <pre>function hello() { return "world"; }</pre>
```

因为嵌套的 `code/style` 已包含在外层受保护块的原始 HTML 中，所以像 `<pre><code>...</code></pre>` 这类结构不需要单独恢复内部 `[CODE:N]`。

### 最终输出

```html
<html>
<head><title>书籍</title><style>.cls { color: red; }</style></head>
<body>
  <h1>第一章：简介</h1>
  <p>这是第一个段落。</p>
  <p>这是<em>第二个</em>段落。</p>
  <pre>function hello() { return "world"; }</pre>
  <p>这是第三个段落。</p>
  <img src="fig.png" alt="Figure 1"/>
  <p>这是第四个段落。</p>
</body>
</html>
```

**闭环完成：切分 → 翻译 → 恢复 → 结构一致。**

---

## 分块策略

### DomChunker 设计

#### 核心算法

```python
class DomChunker:
    """
    基于 DOM 结构的智能分块器

    设计原则：
    1. 以完整 DOM 元素为最小切割单位，保证标签完整闭合
    2. 贪心合并多个元素到 token_limit 上限
    3. 不可翻译元素（纯占位符、纯图片等）跳过，不进入 chunk
    4. 记录每个元素的 xpath，用于翻译后恢复
    """

    # 不可翻译的元素（跳过，不进入 chunk）
    # 注意：img 的 alt 属性虽然包含可翻译文本，但由于 alt 是属性而非子元素，
    # 无法通过 DOM 元素替换来翻译。img alt 的翻译在 Replacer.restore() 中
    # 作为后处理步骤单独处理（可选功能，优先级低）。
    SKIP_TAGS = {"img", "svg", "math", "video", "audio", "canvas", "iframe"}

    # 不可拆分的容器（整体作为一个块，不递归拆分子元素）
    ATOMIC_TAGS = {"table", "ul", "ol", "dl", "figure", "nav"}

    def __init__(self, token_limit: int = 2000):
        self.token_limit = token_limit
```

#### 分块流程

```python
def chunk(self, html: str, is_nav_file: bool = False) -> List[Chunk]:
    """
    Args:
        html: PreCodeExtractor 处理后的 HTML
        is_nav_file: 是否是导航文件

    Returns:
        chunks 列表（不返回 wrapper，恢复时直接在原始 DOM 上操作）
    """
    soup = BeautifulSoup(html, 'html.parser')

    # 1. 找到内容容器
    if is_nav_file:
        container = soup.find('navMap') or soup
    else:
        container = soup.find('body') or soup

    # 2. 收集可翻译的块元素（包括 <head> 中的 <title>）
    blocks = self._collect_blocks(container)

    # 对非导航文件，额外收集 <head><title> 的可翻译文本
    if not is_nav_file:
        title_blocks = self._collect_title_block(soup)
        blocks = title_blocks + blocks

    # 3. 贪心合并
    chunks = self._greedy_merge(blocks)

    return chunks
```

#### `<title>` 翻译处理

`<head>` 中的 `<title>` 包含可翻译文本（浏览器/阅读器显示的标题），但不在 `<body>` 中。需要单独收集：

```python
def _collect_title_block(self, soup) -> List[Block]:
    """收集 <head><title> 作为可翻译块"""
    head = soup.find('head')
    if not head:
        return []
    title = head.find('title')
    if not title or not title.get_text(strip=True):
        return []
    title_html = str(title)
    return [Block(
        html=title_html,
        tokens=count_tokens(title_html),
        xpath=self._get_xpath(title)  # → /html/head/title
    )]
```

#### 块收集（递归）

```python
def _collect_blocks(self, container) -> List[Block]:
    """
    收集容器的直接子元素作为块

    对于超过 token_limit 的单个元素：
    - 如果是 ATOMIC_TAGS（table/ul/ol），保持完整不拆分
    - 否则递归到子元素级别
    """
    blocks = []

    for child in container.children:
        child_html = str(child).strip()
        if not child_html:
            continue

        child_tokens = count_tokens(child_html)
        xpath = self._get_xpath(child)

        # 跳过不可翻译元素
        if self._should_skip(child):
            continue

        # 跳过纯 PreCodeExtractor 占位符（如独立的 [PRE:0]）
        if self._is_pure_placeholder(child_html):
            continue

        if child_tokens <= self.token_limit:
            # 正常块：直接加入
            blocks.append(Block(html=child_html, tokens=child_tokens, xpath=xpath))
        elif hasattr(child, 'name') and child.name in self.ATOMIC_TAGS:
            # 不可拆分容器（table/ul/ol）：整体作为一个块（可能超限）
            blocks.append(Block(html=child_html, tokens=child_tokens, xpath=xpath))
        else:
            # 超限元素：递归到子元素
            blocks.extend(self._collect_blocks(child))

    return blocks
```

#### 贪心合并

```python
def _greedy_merge(self, blocks: List[Block]) -> List[Chunk]:
    """
    贪心合并：将多个块打包到一个 chunk，直到接近 token_limit
    """
    chunks = []
    buffer_htmls = []
    buffer_xpaths = []
    buffer_tokens = 0

    for block in blocks:
        if buffer_tokens + block.tokens > self.token_limit and buffer_htmls:
            # 当前 buffer 加入后超限 → flush
            chunks.append(self._create_chunk(buffer_htmls, buffer_xpaths, buffer_tokens))
            buffer_htmls = []
            buffer_xpaths = []
            buffer_tokens = 0

        buffer_htmls.append(block.html)
        buffer_xpaths.append(block.xpath)
        buffer_tokens += block.tokens

    # 最后的 buffer
    if buffer_htmls:
        chunks.append(self._create_chunk(buffer_htmls, buffer_xpaths, buffer_tokens))

    return chunks
```

```python
def _create_chunk(self, htmls: List[str], xpaths: List[str], tokens: int) -> Chunk:
    """将多个 HTML 片段组合为一个 Chunk"""
    return Chunk(
        name=uuid.uuid4().hex[:8],
        original="\n".join(htmls),
        tokens=tokens,
        xpaths=xpaths,
    )
```

#### xpath 获取与查找

> **实现说明**：BeautifulSoup **不支持** xpath。以下 `_get_xpath()` 和 `_find_by_xpath()` 是基于 BeautifulSoup API 的**自定义路径实现**，而非标准 XPath 规范。路径格式（如 `/html/body/p[2]`）与 XPath 语法一致，但查找逻辑完全自行实现。不使用 lxml，因为 lxml 会自动修正 XML 标签结构，可能破坏原始内容的格式。

```python
def _get_xpath(self, element) -> str:
    """
    获取元素在 DOM 中的路径

    示例：/html/body/div[2]/p[3]

    实现原理：从目标元素向上遍历到根节点，
    对每一层计算该元素在同名兄弟中的位置索引。
    """
    parts = []
    current = element
    while current.parent:
        if hasattr(current, 'name') and current.name:
            # 计算同名兄弟中的位置
            siblings = [s for s in current.parent.children
                       if hasattr(s, 'name') and s.name == current.name]
            if len(siblings) > 1:
                index = siblings.index(current) + 1
                parts.append(f"{current.name}[{index}]")
            else:
                parts.append(current.name)
        current = current.parent
    return '/' + '/'.join(reversed(parts))


def _find_by_xpath(self, soup, xpath: str):
    """
    在 DOM 树中按路径查找元素

    Args:
        soup: BeautifulSoup 对象
        xpath: 路径字符串，如 /html/body/p[2]

    Returns:
        匹配的元素，未找到返回 None

    实现原理：将路径拆分为各级段（如 ['html', 'body', 'p[2]']），
    从根节点逐级向下查找，解析每段的标签名和可选索引。
    """
    parts = [p for p in xpath.split('/') if p]
    current = soup

    for part in parts:
        # 解析标签名和索引：p[2] → name='p', index=2
        match = re.match(r'^(\w+)(?:\[(\d+)\])?$', part)
        if not match:
            return None
        tag_name = match.group(1)
        index = int(match.group(2)) if match.group(2) else 1

        # 在当前节点的子元素中查找第 index 个同名元素
        children = [c for c in current.children
                   if hasattr(c, 'name') and c.name == tag_name]
        if index > len(children):
            return None
        current = children[index - 1]

    return current
```

### 不可翻译元素的判断

```python
def _should_skip(self, element) -> bool:
    """判断元素是否不需要翻译"""
    if not hasattr(element, 'name') or not element.name:
        # NavigableString（纯文本/空白）
        # 注意：<body> 下直接出现的文本节点（非元素包裹）无法生成有意义的 xpath，
        # 因此跳过。这类裸文本在规范的 EPUB 中极少出现。
        # 如果确实存在，BeautifulSoup 规范化阶段会将其包裹在适当的标签中。
        return True

    # 跳过图片、SVG 等
    if element.name in self.SKIP_TAGS:
        return True

    # 检查元素是否有实际文本内容（排除纯标签、纯占位符）
    text_content = element.get_text(strip=True)
    clean_text = re.sub(r'\[(PRE|CODE|STYLE):\d+\]', '', text_content)
    return not clean_text.strip()
```

```python
def _is_pure_placeholder(self, text: str) -> bool:
    """判断文本是否仅包含 PreCodeExtractor 占位符"""
    cleaned = re.sub(r'\[(PRE|CODE|STYLE):\d+\]', '', text)
    return not cleaned.strip()
```

### 导航文件与内嵌目录特殊处理

toc.ncx 是 XML，没有 `<body>`，结构如下：

```xml
<ncx>
  <navMap>
    <navPoint id="ch1">
      <navLabel><text>Chapter 1</text></navLabel>
      <content src="ch1.xhtml"/>
    </navPoint>
    <navPoint id="ch2">
      <navLabel><text>Chapter 2</text></navLabel>
      <content src="ch2.xhtml"/>
    </navPoint>
  </navMap>
</ncx>
```

处理方式：
- 容器 = `<navMap>`（而非 `<body>`）
- 每个 `<navPoint>` 作为一个不可拆分的块
- 贪心合并多个 `<navPoint>` 到 token_limit

对于普通章节文件中嵌入的目录块（如 `<nav class="toc">...</nav>` 或 `epub:type="toc"`）：

- 不再把整个 `<nav>` 保留成超大的 `html_fragment` chunk
- 而是提取其中的可翻译文本节点，按 `nav_text` 模式分块
- 回写时仍使用 `xpath + text_index` 精确恢复到原始 DOM

---

## 合并恢复策略

### xpath 跨 DOM 上下文的安全性

分块时 xpath 在 PreCodeExtractor 处理后的 HTML 上计算（`<pre>` 已被替换为 `[PRE:0]` 文本节点），但恢复时解析的是 `item.content`（原始 HTML，`<pre>` 还在）。

**为什么这是安全的**：xpath 中的同名兄弟计数只计算**相同标签名**的元素。`<pre>` 被替换后变为文本节点（NavigableString），不参与元素计数；在原始 DOM 中 `<pre>` 是元素，但标签名为 `pre`，不影响 `<p>`、`<h1>` 等其他标签的兄弟索引。因此两个 DOM 上下文中计算出的 xpath **路径一致**。

前提条件：PreCodeExtractor 只替换 `<pre>`/`<code>`/`<style>` 这些**标签名唯一**的元素，不会替换 `<p>`/`<div>` 等常见块级元素。

### Replacer.restore() 流程

```python
def restore(self, item: EpubItem):
    """
    将翻译后的 chunks 恢复到原始 HTML

    步骤：
    1. 解析原始 HTML 为 DOM 树
    2. 按 xpath 逐个替换翻译后的元素
    3. 恢复 PreCodeExtractor 占位符
    4. 验证最终 HTML 结构
    5. 写入文件
    """
    # 1. 解析原始 HTML（完整文档，包含 <html>/<head>/<body> 等结构标签）
    soup = BeautifulSoup(item.content, 'html.parser')

    # 2. 按 xpath 替换（使用 _find_by_xpath，见"分块策略"章节中的实现）
    for chunk in item.chunks:
        if not chunk.translated:
            continue
        self._replace_by_xpaths(soup, chunk)

    # 3. 恢复 PreCodeExtractor 占位符
    result = str(soup)
    if item.preserved_pre or item.preserved_code or item.preserved_style:
        pre_extractor = PreCodeExtractor()
        pre_extractor.preserved_pre = item.preserved_pre or []
        pre_extractor.preserved_code = item.preserved_code or []
        pre_extractor.preserved_style = item.preserved_style or []
        result = pre_extractor.restore(result)

    # 4. 验证 HTML 结构
    is_valid, errors = verify_final_html(item.content, result)
    if not is_valid:
        logger.error(f"HTML 结构验证失败: {item.id}, 错误: {errors}")

    # 5. 写入文件
    item.translated = result
    with open(item.path, "w", encoding="utf-8") as f:
        f.write(result)
```

### xpath 替换逻辑

```python
from copy import copy  # 需要导入：copy 用于避免跨 DOM 树移动节点时的引用问题

def _replace_by_xpaths(self, soup, chunk: Chunk):
    """
    解析 chunk 的翻译结果，按 xpath 逐个替换原始 DOM 中的元素

    关键假设：翻译后的 HTML 与原始 chunk 有相同数量、相同顺序的顶层元素
    """
    # 解析翻译后的 chunk HTML
    translated_soup = BeautifulSoup(chunk.translated, 'html.parser')
    translated_elements = [e for e in translated_soup.children
                          if hasattr(e, 'name') and e.name]

    # 校验：翻译后元素数量应与 xpath 数量一致
    if len(translated_elements) != len(chunk.xpaths):
        logger.warning(
            f"Chunk {chunk.name}: 翻译后元素数量 ({len(translated_elements)}) "
            f"!= xpath 数量 ({len(chunk.xpaths)})，尝试按顺序匹配"
        )

    # 按 xpath 逐个替换
    for i, xpath in enumerate(chunk.xpaths):
        if i >= len(translated_elements):
            break
        original_element = self._find_by_xpath(soup, xpath)
        if original_element:
            original_element.replace_with(copy(translated_elements[i]))
```

---


后续章节：[翻译工作流](translation.md)。
