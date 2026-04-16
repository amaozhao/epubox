# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

EPUBox 是基于 LLM 的 EPUB 翻译工具，通过 xpath 追踪和两级占位符系统保持 HTML 结构完整性。

## Commands

```bash
# 翻译
python main.py translate book.epub --limit 1200 --language Chinese
python main.py generate-glossary book.epub

# 测试
pytest                                        # 全部测试
pytest tests/engine/item/test_chunker.py     # 单个文件
pytest --cov=engine --cov-report=html         # 覆盖率

# Lint
ruff check . --fix
ruff format .
```

## Architecture

**Pipeline**: `EPUB → Parser → PreCodeExtractor → DomChunker → Workflow(translate→proofread→apply) → DomReplacer → Builder`

### 核心设计

1. **DOM-Aware Chunking** (`engine/item/chunker.py`)
   - 每个 `Chunk` 存储 `xpaths: List[str]` 追踪原始 DOM 位置
   - 按 token_limit 贪心合并元素，保持标签完整闭合
   - `figure` 和非目录型 `nav` 视为原子容器；导航文件和内嵌目录型 `<nav class="toc">` / `epub:type="toc"` 会走 `nav_text` 分块

2. **两级占位符系统**
   - Level 1 (PreCodeExtractor): `<pre>`/`<code>`/`<style>` → `[PRE:N]`/`[CODE:N]`/`[STYLE:N]`
   - 命中 `pre/code/style` 后按原子块整体保护，不再递归进入其子树；例如 `<pre><code>...</code></pre>` 只会暴露 `[PRE:N]`
   - Level 2 (Translation): LLM 翻译时保留占位符
   - 恢复顺序: DomReplacer → PreCodeExtractor.restore()

3. **翻译工作流** (`engine/agents/workflow.py`)
   - translate_step: 调用 LLM + `validate_translated_html()` 验证结构，最多重试 3 次
   - proofread_step: 校对翻译质量
   - apply_corrections_step: 应用修正，标记为 COMPLETED
   - 内容安全错误自动 fallback 到备用模型

4. **断点续传**
   - 每个 chunk 翻译后立即 `parser.save_json(book)`
   - 重启时 `parser.load_json()` 加载状态，跳过 COMPLETED chunks

5. **XPath 恢复** (`engine/epub/replacer.py`)
   - `find_by_xpath()` 定位原始 DOM 元素
   - `.replace_with()` 替换为翻译后内容
   - `verify_final_html()` 验证最终结构

6. **XHTML 格式要求**
   - `verify_final_html()` 使用 `xml.etree.ElementTree` 验证，要求严格的 XHTML 格式
   - BeautifulSoup 自动将 HTML 转换为 XHTML（`<br>` → `<br/>`），确保验证正常工作

7. **Parser/DomReplacer 一致性**
   - 两者都使用 `BeautifulSoup(html, 'html.parser')` 解析
   - 即使原始 HTML 格式错误，规范化结果也完全一致，确保 xpath 能够准确匹配
