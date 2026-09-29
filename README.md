# EPUBox

本地 EPUB 英译简中工具。`translate` 是唯一翻译入口：解析并保存源文档、默认提取全书术语、冻结本轮词表、逐项翻译与校对、检查章节衔接，最后验证并发布 EPUB。模型只处理源文字投影，不生成原始 XHTML；每项结果写入 JSON，局部失败会继续处理其他独立内容。

当前实施进度与已验证边界见 [v2.5 状态](docs/epubox_v25_status.md)。此分支的代码正在进行端到端联调；在验收通过前，不应把单项测试通过视为整书质量放行。

## 安装与模型

需要 Python 3.13+、EPUBCheck 及已配置的模型服务。安装开发环境：

```bash
uv sync --extra dev
```

默认使用 `.env` 中的 `AGNES_API_KEY`、`AGNES_BASE_URL`、`AGNES_MODEL`。`--provider proxy` 使用对应的 CR proxy 配置。模型 ID 与提示配置会冻结在本轮运行记录中；续跑不会改用另一个模型。

可通过 `EPUBCHECK_COMMAND='java -jar /absolute/path/epubcheck.jar'` 或 `--epubcheck-command` 指定检查器。未显式指定时，开发工作区优先使用已验证的 `.tools/epubcheck/epubcheck-5.3.0/`；没有该版本时才选其他内置版本，均无时使用系统命令。源书有 ERROR/FATAL、词表输入不合法或输出路径不安全时，不会发起模型请求。

## 一个命令翻译整本书

```bash
python main.py translate ./book.epub
```

默认输出到源书旁的 `book-zh-Hans.epub`，运行资料保存在 `work/<source_hash>/<run_id>/`。默认自动提取术语；`--glossary ./terms.json` 可以同时提供用户规则，不会关闭自动发现。需要调整时可用 `--output`、`--work-root`、`--provider`、`--context-tokens`、`--max-output-tokens`、`--concurrency` 和 `--http-limit`。只有明确传入 `--no-auto-extract` 才跳过自动术语调用；此选项主要用于确定性测试，不降低正文校对要求。

中断后**再次执行同一条 `translate` 命令**，程序会按源 EPUB 字节、冻结的模型/规划参数和用户词表，自动找到唯一匹配的工作目录并续跑；已成功落盘的术语窗口、译文与校对结果不会从头请求。若参数或词表改变、或存在多个匹配运行，会明确报错，避免静默重复付费。只有确定要放弃已有进度时才使用另一 `--work-root`。同一本书的并发执行受锁保护；应等当前命令退出后再重启。

同一容器内连续、可翻译的正文段落会按实际源投影尽量合成一个 Unit，直到下一段会使该 Unit 超过 1200 源 token；标题、列表、表格、注释和不可翻译内容保持结构边界。每段仍有独立的源文视图，段落顺序不能被模型改动。Unit 是有源位置和结构归属的任务；Segment 才是模型看到的源文片段，一个 Unit 若因模型整体预算仍过长，会再切成多个 Segment。**每个 Segment 的源投影硬上限为 1200 token**，无法安全切分只挂起该 Unit。Batch 可把多个独立 Segment 放进一次 HTTP 请求；提示、术语和只读上下文不计入这个源片段上限，但仍受模型请求的整体预算约束。

用户词表支持简单映射，默认是软性偏好：

```json
{"cache":"缓存"}
```

需要强制规则时显式指定 `mode` 与标准范围：

```json
[{"source":"C++","target":"C++","mode":"keep_source","scope":{"kind":"book"}}]
```

作用范围也可指定已解析的 `document_ids` 或 `unit_ids`。用户规则优先于自动候选；自动候选仅在有可核对的源证据后作为 `preferred` 冻结。未经确认的自动别名会在冻结警告中列出，不会仅凭出现就生效。独立的旧 `generate-glossary` 命令已删除。

## 显式指定工作目录或修订

```bash
python main.py resume ./work/<source_hash>/<run_id> --output ./book-zh-Hans.epub
```

续跑使用工作目录中的 `source.epub`、`preparation.json`、`glossary.json`、`documents/`、`units/` 和 `requests/`，不要求原用户词表文件仍存在。它扫描完整任务清单，复用成功的术语窗口与已接受译文，只处理未完成项。准备阶段先提交 `parsed_ready`，术语冻结后才创建 ready 的 `bookplan.json`；没有 BookPlan 不表示之前没有付费请求。

先只读查看恢复动作，不产生模型调用或改动文件：

```bash
python main.py plan-resume ./work/<source_hash>/<run_id>
```

自动次数已耗尽时，可明确指定需要重开的 Unit 和新增额度；同一授权重复执行不会重复加额：

```bash
python main.py resume ./work/<source_hash>/<run_id> --output ./book-zh-Hans.epub \
  --retry-unit <unit_id> --add-unit-http 6 --add-run-http 6 --authorization-id repair-001
```

校对修订可用 `--repair-file ./repairs.json` 导入包含 `unit_id`、当前 `base_revision`、`plan_epoch` 和完整 `target`（长 Unit 使用完整 `targets` 映射）的 JSON。章节检查问题用 `--retry-check <document_id> --add-check-http 3` 明确重开。额度追加保留既有请求计数；坏 ID 或过期修订会在加额前拒绝。

状态只有 `completed`、`paused`、`needs_attention`、`failed`。只有 `completed` 且本次 EPUBCheck、结构/资源校验与发布均通过，才返回成功退出码和正式输出路径。未完成运行保留 JSON 进度，不发布混有失败片段的草稿。旧 HTML 或旧 JSON checkpoint 不会混入本轮协议；需为原书创建新运行。

## 开发验证

```bash
uv run pytest -q
uv run pyright
uv run ruff check main.py engine tests
```

[设计文档](docs/epubox_mvp_design_v2_5.md)、[审定任务拆分](docs/epubox_v25_implementation_plan.md)、[逐项验收矩阵](docs/epubox_v25_test_matrix.md)和[当前状态](docs/epubox_v25_status.md)记录范围与证据。实书翻译质量、人评及阅读器检查需要分别验收，不能用模型替身测试代替。

## 许可证

MIT
