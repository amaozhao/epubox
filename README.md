# EPUBox

智能 EPUB 电子书翻译 CLI 工具，基于大语言模型（LLM）实现高效、可靠的翻译功能。

## 特性

- **LLM 翻译**：通过 Agno 框架集成 OpenAI/Claude 等模型，支持语义感知翻译
- **占位符保护**：HTML 标签替换为 `[idN]` 占位符，翻译后精准恢复
- **二级占位符**：`<pre>`/`<code>` 标签单独保护，避免代码被翻译
- **断点续传**：每个 chunk 翻译后即时保存 JSON，中断后可继续
- **智能校对**：翻译后自动校对，修正错词和表达
- **多格式支持**：支持 NCX（toc.ncx）和 XHTML（nav.xhtml）两种导航文件格式

## 安装

```bash
pip install -e .
```

或直接运行：

```bash
python main.py translate <epub_path>
```

## 使用

### 翻译 EPUB

```bash
python main.py translate ./path/to/book.epub
```

指定目标语言和分块大小：

```bash
python main.py translate ./book.epub --language Chinese --limit 1200
```

### 生成术语表

```bash
python main.py generate-glossary ./book.epub
```

## 工作流程

```
EPUB 解析 → 标签替换为 [idN] → 分块 → LLM 翻译 → 校对修正 → 恢复标签 → 构建 EPUB
```

1. **标签保护**：HTML 标签替换为 `[idN]`，保留结构
2. **智能分块**：每个 chunk ≤ 2000 tokens，最多 15 个占位符
3. **LLM 翻译**：保留占位符，翻译文本内容
4. **自动校对**：修正错词、统一词汇（"您"→"你"）
5. **精准恢复**：将 `[idN]` 恢复为原始标签

## 配置

通过环境变量配置：

```bash
export OPENAI_API_KEY="sk-..."
# 或
export ANTHROPIC_API_KEY="sk-ant-..."
```

## 项目结构

```
engine/
├── orchestrator.py        # 核心协调器
├── agents/
│   ├── translator.py     # 翻译代理
│   ├── proofer.py        # 校对代理
│   └── workflow.py       # 工作流（翻译→校对→修正）
├── epub/
│   ├── parser.py         # EPUB 解析
│   ├── builder.py        # EPUB 构建
│   └── replacer.py       # 占位符恢复
└── item/
    ├── chunker.py        # HTML 分块
    ├── placeholder.py     # 占位符管理
    └── tag/
        ├── preserve.py  # 标签→占位符
        └── restore.py    # 占位符→标签
```

## 许可证

MIT
