# EPUBox

智能 EPUB 电子书翻译 CLI 工具，基于大语言模型（LLM）实现高效、可靠的翻译功能。

## 特性

- **LLM 翻译**：通过 Agno 框架集成 OpenAI/Claude 等模型，支持语义感知翻译
- **占位符保护**：HTML 标签替换为 `[idN]` 占位符，翻译后精准恢复
- **二级占位符**：`<pre>`/`<code>`/`<style>` 标签按原子块单独保护；命中后整体替换为占位符，不再递归展开内部子树
- **断点续传**：每个 chunk 翻译后即时保存 JSON，中断后可继续
- **智能校对**：翻译后自动校对，修正错词和表达
- **Kindle 字体优化**：可重排书使用设备默认中文字体，并安全删除无引用字体
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

默认会为可重排 EPUB 设置 `zh-CN`，使用设备字体并安全剪枝无引用字体。固定版式、加密字体、PUA/icon、SVG/MathML 或无法安全解析的样式会自动保留。需要完整保留原字体时使用：

```bash
python main.py translate ./book.epub --preserve-fonts
```

### 生成术语表

```bash
python main.py generate-glossary ./book.epub
```

编辑生成的 `glossary/<书名>.json`，为需要统一的术语填写译法。非空条目会被锁定，长短语优先于其中的单词；空白候选不会影响翻译。术语表修改后再次运行翻译，受影响的旧 chunk 会自动重译。

术语表生成是可选功能，普通翻译不会自动联网下载 NLTK 数据。首次使用该命令前，请在可信网络或离线数据源中为本地环境准备：

```bash
.venv/bin/python -m nltk.downloader punkt_tab stopwords averaged_perceptron_tagger_eng
```

受限网络可将预下载数据目录通过 `NLTK_DATA=/path/to/nltk_data` 提供给程序；不要为了下载而关闭 NLTK 的代理安全检查。

## 工作流程

```
EPUB 解析 → 标签替换为 [idN] → 分块 → LLM 翻译 → 校对修正 → 恢复标签 → 构建 EPUB
```

1. **标签保护**：HTML 标签替换为 `[idN]`，保留结构
2. **智能分块**：每个 chunk ≤ 2000 tokens，最多 15 个占位符
3. **LLM 翻译**：保留占位符，翻译文本内容
4. **自动校对**：修正错词、统一词汇（"您"→"你"）
5. **质量门禁**：拦截重复退化、术语漂移和残留英文
6. **精准恢复**：将 `[idN]` 恢复为原始标签
7. **字体处理**：设置中文语言、使用设备字体并删除可证明未引用的字体

## 配置

通过环境变量配置：

```bash
AGNES_API_KEY=
AGNES_BASE_URL=https://apihub.agnes-ai.com/v1
AGNES_MODEL=agnes-2.0-flash
AGNES_TEXT_RPM=10
```

将 Agnes 控制台生成的 API Key 填入项目根目录 `.env` 的 `AGNES_API_KEY`。

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
