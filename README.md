# EPUBox

本地 EPUB 英译简中工具。`translate` 是唯一翻译入口：解析并保存源文档、默认提取全书术语、冻结本轮词表、逐项翻译与校对、检查章节衔接，最后验证并发布 EPUB。模型只处理源文字投影，不生成原始 XHTML；每项结果写入 JSON，局部失败会继续处理其他独立内容。

当前实施进度与已验证边界见 [v2.5 状态](docs/history/implementation/status.md)。此分支的代码正在进行端到端联调；在验收通过前，不应把单项测试通过视为整书质量放行。

## 安装与模型

需要 Python 3.13+、EPUBCheck 及已配置的模型服务。安装开发环境：

```bash
uv sync --extra dev
```

默认使用 `.env` 中的 `AGNES_API_KEY`、`AGNES_BASE_URL`、`AGNES_MODEL`。`--provider proxy` 使用对应的 CR proxy 配置。模型 ID 与提示配置会冻结在本轮运行记录中；续跑不会改用另一个模型。

所有模型阶段（术语提取、术语消歧、初译、校对及章节检查）统一使用非流式请求，一次接收完整响应。主、备用接口每次请求的 HTTP 超时和总等待上限均为 300 秒；重试同样适用。旧断点中的较短超时不覆盖此策略，已完成记录继续复用。运行中的进程需重启后才会使用新策略。

可通过 `EPUBCHECK_COMMAND='java -jar /absolute/path/epubcheck.jar'` 或 `--epubcheck-command` 指定检查器。未显式指定时，开发工作区优先使用已验证的 `.tools/epubcheck/epubcheck-5.3.0/`；没有该版本时才选其他内置版本，均无时使用系统命令。源书有 ERROR/FATAL、词表输入不合法或输出路径不安全时，不会发起模型请求。

## 一个命令翻译整本书

```bash
python main.py translate ./book.epub
```

默认输出到源书旁的 `book-cn.epub`，运行资料保存在同目录的 `book/<source_hash>/<run_id>/`。已有旧 `work/` 断点只在身份、位置和父/子任务锁允许时安全迁移；冲突或悬空链接会明确拒绝。默认自动提取术语；`--glossary ./terms.json` 可以同时提供用户规则，不会关闭自动发现。需要调整时可用 `--output`、`--work-root`、`--provider`、`--context-tokens`、`--max-output-tokens`、`--concurrency` 和 `--http-limit`。只有明确传入 `--no-auto-extract` 才跳过自动术语调用；此选项主要用于确定性测试，不降低正文校对要求。

中断后**再次执行同一条 `translate` 命令**，程序会按源 EPUB 字节、冻结的模型/规划参数和用户词表，自动找到唯一匹配的工作目录并续跑；已成功落盘的术语窗口、译文与校对结果不会从头请求。若参数或词表改变、或存在多个匹配运行，会明确报错，避免静默重复付费。只有确定要放弃已有进度时才使用另一 `--work-root`。同一本书的并发执行受锁保护；应等当前命令退出后再重启。

若提示 `compact book session does not match the original source`，表示同名工作目录记录的源文件身份与当前 EPUB 不同。修复或重新下载 EPUB 后即使文件名不变，也不能混用旧断点。命令会立即停止并显示不匹配的字段和工作目录，不重复续跑。要继续旧进度，应恢复原始 EPUB；要翻译新版 EPUB，应先将旧工作目录改名备份，或指定另一个 `--work-root`，再执行翻译命令。请勿直接修改断点中的哈希来绕过校验。

若停在 `preflight` 且 `HTTP=0`，控制台会列出阻塞项的资源路径、单元 ID、标签、正文 token 数和预算原因，然后停止；固定的预检阻塞不会自动重试五轮。完整段落或单行表格内容本身超过 1500 tokens 时，删除断点、减少上下文或反复续跑都不能消除超限。

重新执行 `translate` 或普通 `resume` 会给失败项本次命令的一次恢复机会，包括历史自动重试已耗尽的项目。一个合并请求收到完整但无效的模型内容时，仍失败的成员先保持合并；累计三次内容失败后改为单项请求，后续轮次不再重新合批。已成功成员立即复用。响应截断仍立即缩批，网络超时或连接失败沿用同一物理请求的传输重试，不计入三次内容失败。可安全细分的复杂内容仍可按完整列表项、句子或表格单元格形成永久子计划。有效初译继续复用，只重做必要的校对。

同一次命令内部的最多五轮自动续跑共享这次恢复机会，不反复追加；再次由用户启动命令才有新的机会。合并和单项内容失败分别持久化，重启后继续原有拆分状态。单项仍受相同结果、内容代和父子任务总额度约束。历史失败账本和 HTTP 计数不清零，显式设置的总 HTTP 限额不自动增加。指定 `--retry-unit`、`--retry-check` 或修订文件时，不会顺带重开未选中的失败项。持续坏响应仍会如实标记为未完成。

运行中可直接查看 `book/<source_hash>/<run_id>/units/<unit_id>.json`：`items[*].target_projection` 是已保存的译文，`items[*].status` 区分初译通过、校对通过和真正需要处理的问题。进度日志分别显示初译、校对、最终接受的数量；导航项等待源标题完成会单列为依赖，不算局部错误。HTTP 是术语、翻译和校对的累计请求数。`report.json` 在命令退出并记录本轮结果时生成。

所有模型阶段在派发前检查完整消息的保守输入上界 **50,000**；术语、冲突裁决和衔接请求在输入与输出预算内合批，初译和 review 不追求填满上限。超大批次先拆开，单项仍装不下才记录局部问题；成功响应先写 JSON 日志，崩溃续跑优先回放。Agnes 没有已核实的生产模型精确预请求计数接口，因此本地用完整消息的 UTF-8 字节数加包装余量作保守门，服务端返回后再核查实际输入 token；`report.json` 会区分两者，不能把本地数误认为服务端精确用量。

若程序识别到旧版 `epubox-v25-2` 因协议问题拒绝了全部自动术语，它会停止并保留旧运行，不会静默重跑。确认要重新执行术语阶段时，只需一次性在原命令后加 `--repair-terms`；程序会先保存旧、新运行的关联记录，之后仍用不带该参数的同一条 `translate` 命令自动续跑。

同一容器内连续、可翻译的正文段落会按实际源投影尽量合成一个 Unit，直到下一段会使该 Unit 超过 1200 源 token；标题、列表、表格、注释和不可翻译内容保持结构边界。每段仍有独立的源文视图，段落顺序不能被模型改动。Unit 是有源位置和结构归属的任务；Segment 才是模型看到的源文片段，一个 Unit 若因模型整体预算仍过长，会再切成多个 Segment。**每个 Segment 的源投影硬上限为 1200 token**，无法安全切分只挂起该 Unit。Batch 可把多个独立 Segment 放进一次 HTTP 请求；提示、术语和只读上下文不计入这个源片段上限，但仍受模型请求的整体预算约束。

当前原子路径使用 `EPUB_CHUNK_MAX_TOKENS=1500`。最终初译、校对和重试请求的正文硬上限是 1500 tokens，不允许上浮；环境或 `--limit` 设置更大的值也不能突破该上限，可以调低。标签、占位符、词表和上下文不计入正文额度。p/em/i 保持不可拆分，超限标记待处理；ul/ol 可以按完整 li 拆分。预算内的表格整体翻译，超长表格可在完整外层 tr 之间拆成多个请求，单个 tr 不再拆分；thead/tbody/tfoot、合并单元格、嵌套表格及原始结构在合并回填时保留。已有表格预检阻塞且尚未发出请求的断点会保存新的行拆分预检凭据，原凭据保留，无须删除工作目录。旧断点已接受的译文和历史响应仍保留，未完成请求按新上限重组。

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

## Kindle 字体兼容

最终打包默认让阅读器选择字体：清除源书的 `@font-face` 和字体族指定，处理外部 CSS、内嵌样式与行内样式，并删除 EPUB 内的字体文件及对应的 OPF / 字体混淆记录。标题字号、粗体、斜体与段落布局保留；不写死 `system-ui` 或某个中文字体名称。源书和翻译断点保持原样。

代码中的等宽字体保留，包括 `Menlo, Consolas, monospace` 和已知等宽字体，不会改成正文的 `inherit`。代码区域的原始样式、注释和缩进也不会被字体及中文空白清理改写。

所有新翻译默认使用通用代码保护：识别语义代码标签、代码类名及内外部 CSS 指定的代码块和内联代码，将其作为只读占位符保留。初译和校对不能删除、复制、伪造代码占位符或把源码复制到可编辑正文。缺失或无法安全判定的代码样式会在准备阶段报错，避免把疑似代码当正文发送。

打包时会自动对照原书核验代码字符、标记、注释和缩进，不要求逐本配置保护清单。旧断点继续使用原提取版本和历史请求；重新出版时自动恢复已接受译文中被误改的代码，不重新翻译正文或伪造模型校对记录。

已完成的运行重新执行原 `translate` 或 `resume` 命令时，会复用已接受的译文，以新字体策略重新打包旧输出，无须重新翻译。打包仍须通过资源引用校验和 EPUBCheck。

此策略主要适用于可重排的文字书。检测到与嵌入字体并存的私用区文字或图标时，会停止清理并报错，保留原文件，避免图标变成方框；无法安全解析的样式也会明确报错。Kindle 的中文斜体、合成粗体与具体字形仍受设备影响，EPUBCheck 通过不等于实机视觉验收。可在 Kindle Previewer 或设备上切换字体、字号检查中英混排、标题及强调文字。

## 中文排版整理

出版阶段在忠实回填和结构验证后整理中文排版：安全移除无属性、无文本且没有 CSS 用途的空 `span/em/strong`，清理正文段首及汉字、中文标点之间的普通空白。索引 ID、语义属性、英文词间空格、URL、代码、NBSP 和要求保留空白的样式区域保持原样；非空强调标签不合并。清理使用字节补丁，完成后重新核验引用与 EPUBCheck，策略版本和清理数量写入出版凭据。

已完成的断点重新执行原 `translate` 或 `resume` 命令时，若出版清理策略已更新，会复用已接受译文重新打包，无须重新翻译。只有完成成品、没有翻译断点时，可调用 `engine.epub.publish.repack_completed(source, output, checker)` 输出另一个 EPUB；此路径只读取成品，不创建翻译模型，也不覆盖输入文件。

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
