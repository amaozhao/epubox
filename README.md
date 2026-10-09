# EPUBox

本地 EPUB 英译简中工具。`translate` 是唯一翻译入口：解析并保存源文档、默认提取全书术语、冻结本轮词表、逐项翻译与校对、检查章节衔接，最后验证并发布 EPUB。模型只处理源文字投影，不生成原始 XHTML；每项结果写入 JSON，局部失败会继续处理其他独立内容。

当前实施进度与已验证边界见 [v2.5 状态](docs/history/implementation/status.md)。此分支的代码正在进行端到端联调；在验收通过前，不应把单项测试通过视为整书质量放行。

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

默认输出到源书旁的 `book-cn.epub`，运行资料保存在同目录的 `book/<source_hash>/<run_id>/`。已有旧 `work/` 断点只在身份、位置和父/子任务锁允许时安全迁移；冲突或悬空链接会明确拒绝。默认自动提取术语；`--glossary ./terms.json` 可以同时提供用户规则，不会关闭自动发现。需要调整时可用 `--output`、`--work-root`、`--provider`、`--context-tokens`、`--max-output-tokens`、`--concurrency` 和 `--http-limit`。只有明确传入 `--no-auto-extract` 才跳过自动术语调用；此选项主要用于确定性测试，不降低正文校对要求。

中断后**再次执行同一条 `translate` 命令**，程序会按源 EPUB 字节、冻结的模型/规划参数和用户词表，自动找到唯一匹配的工作目录并续跑；已成功落盘的术语窗口、译文与校对结果不会从头请求。若参数或词表改变、或存在多个匹配运行，会明确报错，避免静默重复付费。只有确定要放弃已有进度时才使用另一 `--work-root`。同一本书的并发执行受锁保护；应等当前命令退出后再重启。

对已核实的单项模型内容失败，调度器在原命令中最多自动恢复一次：截断或投影结构错误会孤立重试或重规划；review 问题只在得到完整替换译文并再次通过完整 review 后才接受。恢复额度及历史请求均保存在 JSON 中，不会跨重启无限重试。持续坏响应、真实语义冲突或损坏的源/存储仍会明确报告，不能冒充完成的 EPUB。

运行中可直接查看 `book/<source_hash>/<run_id>/units/<unit_id>.json`：`items[*].target_projection` 是已保存的译文，`items[*].status` 区分初译通过、校对通过和真正需要处理的问题。进度日志分别显示初译、校对、最终接受的数量；导航项等待源标题完成会单列为依赖，不算局部错误。HTTP 是术语、翻译和校对的累计请求数。`report.json` 在命令退出并记录本轮结果时生成。

所有模型阶段在派发前检查完整消息的保守输入上界 **50,000**；术语、冲突裁决和衔接请求在输入与输出预算内合批，初译和 review 不追求填满上限。超大批次先拆开，单项仍装不下才记录局部问题；成功响应先写 JSON 日志，崩溃续跑优先回放。Agnes 没有已核实的生产模型精确预请求计数接口，因此本地用完整消息的 UTF-8 字节数加包装余量作保守门，服务端返回后再核查实际输入 token；`report.json` 会区分两者，不能把本地数误认为服务端精确用量。

若程序识别到旧版 `epubox-v25-2` 因协议问题拒绝了全部自动术语，它会停止并保留旧运行，不会静默重跑。确认要重新执行术语阶段时，只需一次性在原命令后加 `--repair-terms`；程序会先保存旧、新运行的关联记录，之后仍用不带该参数的同一条 `translate` 命令自动续跑。

同一容器内连续、可翻译的正文段落会按实际源投影尽量合成一个 Unit，直到下一段会使该 Unit 超过 1200 源 token；标题、列表、表格、注释和不可翻译内容保持结构边界。每段仍有独立的源文视图，段落顺序不能被模型改动。Unit 是有源位置和结构归属的任务；Segment 才是模型看到的源文片段，一个 Unit 若因模型整体预算仍过长，会再切成多个 Segment。**每个 Segment 的源投影硬上限为 1200 token**，无法安全切分只挂起该 Unit。Batch 可把多个独立 Segment 放进一次 HTTP 请求；提示、术语和只读上下文不计入这个源片段上限，但仍受模型请求的整体预算约束。

当前原子路径使用 `EPUB_CHUNK_MAX_TOKENS=1500`。最终初译、校对和重试请求的正文硬上限是 1500 tokens，不允许上浮；环境或 `--limit` 设置更大的值也不能突破该上限，可以调低。标签、占位符、词表和上下文不计入正文额度。p/table/em/i 保持不可拆分，超限标记待处理；ul/ol 可以按完整 li 拆分。旧断点已接受的译文和历史响应仍保留，未完成请求按新上限重组。

用户词表支持简单映射，默认是软性偏好：

```json
{"cache":"缓存"}
```

需要强制规则时显式指定 `mode` 与标准范围：

```json
[{"source":"C++","target":"C++","mode":"keep_source","scope":{"kind":"book"}}]
```

作用范围也可指定已解析的 `document_ids` 或 `unit_ids`。用户规则优先于自动候选；自动候选仅在有可核对的源证据后作为 `preferred` 冻结。未经确认的自动别名会在冻结警告中列出，不会仅凭出现就生效。独立的旧 `generate-glossary` 命令已删除。

术语模型响应会先原子保存到 `glossary/responses/`，再登记请求成功；中断后可从原文回放而不重新付费。候选全部因协议或源证据错误被拒时，程序携带错误重试一次，并保留拒绝记录；合法 `candidates=[]` 不会误判为失败。局部提取失败按设计文档 §7.8 标记缺口，冻结已核实的词条后继续正文翻译；若最终没有可用术语，报告会明确显示空词表和缺口。

## 显式指定工作目录或修订

```bash
python main.py resume ./book/<source_hash>/<run_id> --output ./book-cn.epub
```

续跑使用工作目录中的 `source.epub`、`preparation.json`、`glossary.json`、`documents/`、`units/` 和 `requests/`，不要求原用户词表文件仍存在。它扫描完整任务清单，复用成功的术语窗口与已接受译文，只处理未完成项。准备阶段先提交 `parsed_ready`，术语冻结后才创建 ready 的 `bookplan.json`；没有 BookPlan 不表示之前没有付费请求。

先只读查看恢复动作，不产生模型调用或改动文件：

```bash
python main.py plan-resume ./book/<source_hash>/<run_id>
```

自动次数已耗尽时，可明确指定需要重开的 Unit 和新增额度；同一授权重复执行不会重复加额：

```bash
python main.py resume ./book/<source_hash>/<run_id> --output ./book-cn.epub \
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

[设计文档](docs/history/design/plan.md)、[审定任务拆分](docs/history/implementation/plan.md)、[逐项验收矩阵](docs/history/implementation/tests.md)和[当前状态](docs/history/implementation/status.md)记录范围与证据。实书翻译质量、人评及阅读器检查需要分别验收，不能用模型替身测试代替。

## 许可证

MIT
