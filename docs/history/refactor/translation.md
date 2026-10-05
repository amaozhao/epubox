# 历史设计续篇

前文：[设计方案](plan.md)。

## 翻译工作流

### 工作流结构

```python
def get_translator_workflow() -> Workflow:
    return Workflow(
        name="TranslatorWorkflow",
        steps=[
            Step(name="translate", executor=translate_step),
            Step(name="proofread", executor=proofread_step),
            Step(name="apply_corrections", executor=apply_corrections_step),
        ],
    )
```

### translate_step

```python
async def translate_step(step_input: StepInput) -> StepOutput:
    chunk: Chunk = step_input.input
    glossary = step_input.additional_data.get("glossary", {})

    # 已翻译的 chunk（手动翻译后重新运行）直接进入校对
    if chunk.status == TranslationStatus.TRANSLATED and chunk.translated:
        return StepOutput(content=chunk)

    # 无可翻译文本内容 → 跳过
    if not _has_translatable_content(chunk.original):
        chunk.translated = chunk.original
        chunk.status = TranslationStatus.TRANSLATED
        return StepOutput(content=chunk)

    # 翻译（带重试）
    for attempt in range(MAX_RETRIES):
        try:
            translated = await call_translator(chunk.original, glossary)

            # 验证翻译结果的 HTML 结构
            is_valid, error = validate_translated_html(chunk.original, translated)
            if is_valid:
                chunk.translated = translated
                chunk.status = TranslationStatus.TRANSLATED
                return StepOutput(content=chunk)

            logger.warning(f"翻译验证失败 (attempt {attempt+1}): {error}")
        except Exception as e:
            logger.error(f"翻译异常 (attempt {attempt+1}): {e}")

    # 所有重试失败 → 保留原文，标记 UNTRANSLATED
    chunk.translated = chunk.original
    chunk.status = TranslationStatus.UNTRANSLATED
    return StepOutput(content=chunk)
```

### 翻译 Agent Prompt（简化）

旧 prompt 大量篇幅用于占位符保护指令。新 prompt 大幅简化：

```python
instructions = [
    "1. **JSON OUTPUT ONLY**: Return ONLY {\"translation\": \"...\"}.",
    "2. **TRANSLATION**: Translate the HTML content into natural, fluent, simplified Chinese.",
    "3. **HTML STRUCTURE**:",
    "   - Preserve ALL HTML tags exactly as they are.",
    "   - Keep tag attributes unchanged (class, id, href, src, etc.).",
    "   - Maintain the same number and nesting of elements.",
    "   - Only translate text content between tags.",
    "4. **PLACEHOLDERS**: [PRE:N], [CODE:N], [STYLE:N] are code/style placeholders. Copy them VERBATIM.",
    "5. **GLOSSARY**: Use the 'glossaries' dictionary for technical term translations.",
]
```

### proofread_step 和 apply_corrections_step

与当前实现基本不变，只是不再处理 `[idN]` 占位符相关逻辑。

校对器收到的是翻译后的 HTML，直接校对文本质量即可。

---

## 断点续传

### 机制

每个 chunk 翻译完成后立即保存 JSON：

```python
# orchestrator.py
for chunk in item.chunks:
    if chunk.status == TranslationStatus.COMPLETED:
        continue

    response = await workflow.arun(input=chunk, ...)
    # 更新 chunk 状态
    parser.save_json(book)  # 立即持久化
```

### 重新运行时的状态判断

```python
def _should_process_chunk(self, chunk: Chunk) -> bool:
    """判断 chunk 是否需要处理"""
    if chunk.status == TranslationStatus.COMPLETED:
        return False  # 已完成，跳过

    if chunk.status == TranslationStatus.TRANSLATED and chunk.translated:
        return True  # 已翻译但未校对（含手动翻译），需要继续校对流程

    if chunk.status == TranslationStatus.UNTRANSLATED and chunk.translated and chunk.translated != chunk.original:
        # 手动翻译后的 chunk：用户编辑了 translated 字段
        chunk.status = TranslationStatus.TRANSLATED
        return True  # 进入校对流程

    if chunk.status == TranslationStatus.UNTRANSLATED:
        # 翻译失败且未手动编辑 → 跳过，避免重复失败
        # 用户可通过手动编辑 translated 字段后重新运行来处理
        return False

    if chunk.status == TranslationStatus.PENDING:
        return True  # 待翻译

    return False  # 未知状态，安全跳过
```

### JSON 数据结构

```json
{
    "name": "book-name",
    "path": "/path/to/book.epub",
    "extract_path": "/path/to/temp/book-name",
    "items": [
        {
            "id": "OEBPS/chapter1.xhtml",
            "path": "/path/to/temp/book-name/OEBPS/chapter1.xhtml",
            "content": "<html>...original...</html>",
            "translated": null,
            "preserved_pre": ["<pre>...</pre>"],
            "preserved_code": [],
            "preserved_style": ["<style>...</style>"],
            "chunks": [
                {
                    "name": "a1b2c3d4",
                    "original": "<h1>Chapter 1</h1><p>First paragraph.</p>",
                    "translated": "<h1>第一章</h1><p>第一段。</p>",
                    "status": "completed",
                    "tokens": 35,
                    "xpaths": ["/html/body/h1", "/html/body/p[1]"]
                },
                {
                    "name": "e5f6g7h8",
                    "original": "<p>Second paragraph.</p>",
                    "translated": null,
                    "status": "pending",
                    "tokens": 12,
                    "xpaths": ["/html/body/p[2]"]
                }
            ]
        }
    ]
}
```

---

## 手动翻译支持

### 场景

某些 chunk 由于内容敏感、格式特殊等原因翻译失败（status = UNTRANSLATED）。用户需要手动翻译后继续流程。

### 流程

```
第一次运行：
  Chunk A: PENDING → TRANSLATED → COMPLETED  ✓
  Chunk B: PENDING → UNTRANSLATED             ✗ 翻译失败
  Chunk C: PENDING → TRANSLATED → COMPLETED  ✓

生成手动翻译报告：manual_translation_report.json
  → 列出所有 UNTRANSLATED 的 chunk

用户手动编辑：
  方式 1: 直接编辑 book.json 中 Chunk B 的 translated 字段
  方式 2: 编辑 manual_translation_report.json，由程序回写

第二次运行（断点续传）：
  Chunk A: COMPLETED → 跳过
  Chunk B: UNTRANSLATED + translated 已填写
           → 检测到手动翻译 → 状态改为 TRANSLATED
           → 进入校对步骤 → COMPLETED
  Chunk C: COMPLETED → 跳过
```

### 手动翻译报告格式

```json
{
    "generated_at": "2026-04-08T12:00:00",
    "total": 1,
    "chunks": [
        {
            "file": "OEBPS/chapter3.xhtml",
            "chunk_name": "e5f6g7h8",
            "original": "<p>Some content that failed translation.</p>",
            "translated": "",
            "xpaths": ["/html/body/p[5]"],
            "status": "untranslated"
        }
    ]
}
```

用户填写 `translated` 字段后，重新运行时程序检测并继续工作流。

---

## 验证策略

### 翻译结果验证

翻译后的 HTML 需要通过以下验证：

```python
def validate_translated_html(original: str, translated: str) -> Tuple[bool, str]:
    """
    验证翻译结果的 HTML 结构完整性

    检查项：
    1. 顶层元素数量一致（翻译不应增删元素）
    2. 顶层元素标签名一致（<p> 不应变成 <div>）
    3. PreCodeExtractor 占位符完整保留
    """
    original_soup = BeautifulSoup(original, 'html.parser')
    translated_soup = BeautifulSoup(translated, 'html.parser')

    original_elements = [e for e in original_soup.children if hasattr(e, 'name') and e.name]
    translated_elements = [e for e in translated_soup.children if hasattr(e, 'name') and e.name]

    # 1. 元素数量
    if len(original_elements) != len(translated_elements):
        return False, f"元素数量不一致: 原始 {len(original_elements)}, 翻译 {len(translated_elements)}"

    # 2. 标签名一致
    for i, (orig, trans) in enumerate(zip(original_elements, translated_elements)):
        if orig.name != trans.name:
            return False, f"第 {i+1} 个元素标签不一致: 原始 <{orig.name}>, 翻译 <{trans.name}>"

    # 3. PreCodeExtractor 占位符完整
    for pattern in [r'\[PRE:\d+\]', r'\[CODE:\d+\]', r'\[STYLE:\d+\]']:
        orig_count = len(re.findall(pattern, original))
        trans_count = len(re.findall(pattern, translated))
        if orig_count != trans_count:
            return False, f"占位符数量不一致: {pattern} 原始 {orig_count}, 翻译 {trans_count}"

    return True, ""
```

### 最终 HTML 验证

合并恢复后，验证最终输出：

```python
import xml.etree.ElementTree as ET

def verify_final_html(original: str, restored: str) -> Tuple[bool, str]:
    """
    验证最终 HTML 的完整性

    检查项：
    1. 无残留 PreCodeExtractor 占位符
    2. XML well-formedness（XHTML 本质是 XML）
    """
    # 1. 无残留占位符
    remaining = re.findall(r'\[(PRE|CODE|STYLE):\d+\]', restored)
    if remaining:
        return False, f"残留占位符: {remaining}"

    # 2. XML well-formedness 检查
    # 使用 xml.etree.ElementTree 解析，它不会自动修正标签，
    # 如果标签不配对或格式错误会直接抛出 ParseError。
    # 注意：lxml / BeautifulSoup 都会自动修正不合法标签，
    # 不适合用于验证——即使输入有错也会被静默修复。
    try:
        ET.fromstring(restored)
    except ET.ParseError as e:
        return False, f"XML 格式错误: {e}"

    return True, ""
```

> **为什么不用 lxml 或 BeautifulSoup 做验证？**
> 两者都会自动修正不合法的标签结构（添加缺失闭合标签、修复嵌套等），导致无法检测出实际错误。
> `xml.etree.ElementTree` 严格按 XML 规范解析，遇到格式错误直接报错，适合验证 XHTML 的 well-formedness。

---

## 实施计划

### Phase 1: 核心重写

| 优先级 | 任务 | 文件 |
|--------|------|------|
| P0 | 重写 DomChunker | `engine/item/chunker.py` |
| P0 | 重写 Merger/Replacer（xpath 恢复） | `engine/item/merger.py`, `engine/epub/replacer.py` |
| P0 | 修改 Parser 调用流程 | `engine/epub/parser.py` |
| P0 | 更新 Chunk 数据模型 | `engine/schemas/chunk.py`, `engine/schemas/epub.py` |
| P0 | 简化翻译 Agent prompt | `engine/agents/translator.py` |
| P0 | 重写 HTML 验证 | `engine/agents/validator.py` |
| P0 | 简化翻译工作流 | `engine/agents/workflow.py` |

### Phase 2: 清理

| 优先级 | 任务 | 文件 |
|--------|------|------|
| P1 | 删除 TagPreserver / TagRestorer | `engine/item/tag/` |
| P1 | 删除 PlaceholderManager | `engine/item/placeholder.py` |
| P1 | 删除 Renumberer | `engine/item/renumberer.py` |
| P1 | 删除 Aligner | `engine/agents/aligner.py` |
| P1 | 更新 Orchestrator | `engine/orchestrator.py` |

### Phase 3: 测试

| 优先级 | 任务 |
|--------|------|
| P0 | DomChunker 单元测试（完整标签、贪心合并、超限递归、导航文件） |
| P0 | xpath 恢复单元测试（正常替换、元素数量不匹配、嵌套结构） |
| P0 | 端到端测试（真实 EPUB → 翻译 → 恢复 → 验证） |
| P1 | 断点续传测试 |
| P1 | 手动翻译流程测试 |

### Phase 4: 优化（可选）

| 任务 | 说明 |
|------|------|
| 缓存 tiktoken encoder | `count_tokens()` 当前每次调用都创建新 encoder |
| 批量翻译 | 多个小 chunk 合并为一个 API 调用 |
| 并行翻译 | 多个 chunk 并行调用 LLM |

---

## 附录：与旧架构对比

| 维度 | 旧架构 | 新架构 |
|------|--------|--------|
| LLM 输入 | 纯文本 + `[idN]` 占位符 | 原始 HTML |
| 标签保护 | TagPreserver 替换所有标签 | 无需保护，LLM 直接处理 |
| 占位符 | `[idN]`（上百个/章节） | 仅 `[PRE:N]`/`[CODE:N]`/`[STYLE:N]`（极少） |
| 切割单位 | 占位符文本的字符位置 | 完整 DOM 元素 |
| 标签完整性 | 不保证 | 天然保证 |
| Token 利用率 | ~30%（受 max_placeholders 限制） | ~90%+（仅受 token_limit 限制） |
| 定位机制 | 全局/局部索引映射 | xpath |
| 恢复机制 | 字符串拼接 + 占位符替换 | DOM xpath 精确替换 |
| 代码复杂度 | TagPreserver + PlaceholderManager + Renumberer + Aligner | DomChunker + xpath 替换 |
| 索引管理 | 三层（全局→局部→恢复全局） | 无 |
| 翻译 prompt | 大量占位符保护指令 | 简洁的 HTML 保持指令 |
