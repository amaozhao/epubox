# EPUBox v2.3 架构与任务拆分（待用户确认实施）

- 唯一需求基准：`docs/epubox_redesign.md`，版本 **2.3**，860 行，SHA-256 `bbd1aed9a313f34d5af23514253b48218a8552253f727e4219e0fd3ee084f2b0`。
- 源码基线：`13ece3a`；工作分支：`codex/translation-architecture-redesign`。沿用已创建分支，不重新切回 main、不丢弃已有文件。
- 本文完全替换上一轮 v2.1 任务方案；旧 T0–T12 编号不再生效。本文用 **P00–P14** 表示实施任务；**T01–T32** 始终指需求文档的验收用例。
- 当前授权仅为分析、架构设计、任务拆分和 review。正文中的拟议接口/命令/测试均未实现。用户确认任务拆分后才实施；文档 §15 的执行措辞不覆盖这个授权边界。
- 计划审查结果单列于 `docs/epubox_architecture_review.md`；设计 review 通过不代表实现、真实翻译或阅读器验收通过。

## 1. 重新分析后的结论与旧计划删除项

新版的核心不是扩大平台，而是把两项要求贯通到底：**完整章节解析 JSON 是正式运行输入；局部失败不妨碍后续独立工作，最终发布仍执行整书门禁。** 稳定 Unit、本地格式装配、机器校对与可靠输出仍保留。

| 旧方案 | v2.3 决定 | 本计划落点 |
|---|---|---|
| 标准/快速/草稿多模式 | 只有一种正式 completed；没有部分 EPUB 导出、跳过校对或多档质量 | §6、§9，P09 |
| ChapterPlan 与可选请求日志 | DocumentPlan 覆盖 XHTML/HTML/NCX/OPF；每文档原文+计划必须 JSON 化；请求身份与计数必须持久化 | §3–4，P01/P05/P06 |
| 泛化 UnitResult / 简化 review_pending | 使用文档规定的 UnitRecord/items 状态；区分 record_version、revision、plan_epoch | §3、§6，P01/P07 |
| 配置/词表变化的精细依赖失效 | 冻结源义相关配置；改变配置新建运行，不做增量词表平台 | §3、§5，P03/P12 |
| 仅 g/x、代码可译范围 | g/x/b；代码以原子对象本地恢复，短内容以 hints 可见；硬边界固定 | §5，P04 |
| 相邻译文动态依赖失效 | 固定章节版本向量；参与者修改使本章衔接检查整体过期 | §6，P08/P11 |
| 部分切片联合重译后拼用旧片段 | 只允许整个 Unit 的 CutPlan 升级，最多一次自动升级；旧片段全部过期 | §7，P10 |
| 每版本重新获得修复额度 | Unit 累计额度跨 plan_epoch/revision 保留；HTTP attempt 单独计数 | §6，P06 |
| 默认/新增字体优化或剪枝开关建设 | 默认保留 CSS/字体、关闭剪枝；只复用有证据且可关闭的既有输出策略，不建字体系统 | §8，P09 |
| 新旧盲评/阅读成为每书运行门槛，或增加固定性能阈值 | 真实抽查属于引擎版本验收；单书 reader_check 可为 not_run；不新增成本降幅/旧版完成率硬门槛 | §9、§11，P14 |
| 实施前全面审计历史 68 项，最后删除整个旧引擎 | 只核对影响新接口的代码；旧引擎保留用于旧运行，不与新状态混用 | §2、§10，P00/P14 |

## 2. 当前代码事实、推论和验证边界

### 2.1 已核实事实（高置信度）

| 现状 | 代码证据 | 对重构的影响 |
|---|---|---|
| Parser 已在解析后保存整本 EpubBook JSON，并优先 load_json；content 已保存源标记字符串 | `engine/epub/parser.py:256`、`:280`、`:308`；`engine/schemas/epub.py:18` | 保留“EPUB→JSON→继续”的产品流程；缺的是分文档完整计划、可靠源身份和提交协议，不能称当前完全没有 JSON |
| 普通章节按目录扫描、BeautifulSoup 规范化、保护后分块；未构建 manifest/spine 驱动的 BookPlan | `engine/epub/parser.py:327`；`engine/core/markup.py:6` | 统一包范围/源位置/安全解析；不能误称只扫 spine |
| Chunk 同时包含原文/译文、token、XPath 和完成状态；普通裸文本/alt 与超长叶节点存在覆盖/预算缺口 | `engine/schemas/chunk.py:17`；`engine/item/chunker.py:265`、`:301`、`:380` | 分离 Unit/CutPlan/Batch；独立槽位核对 |
| 已有 pre/code/style 保护、HTML/属性/占位符/术语/退化检查、回填后 XML 检查 | `engine/item/precode.py:40`；`engine/agents/verifier.py:985`、`:1212`；`engine/epub/replacer.py:75` | 保留有效反例与纯检测逻辑；不能把风险等同于所有坏结果会落盘 |
| 模型常规输出 HTML，回填解析其 HTML；修订主要在文本节点内做字符串替换 | `engine/agents/translator.py:13`；`engine/epub/replacer.py:256`；`engine/agents/workflow.py:394` | 新协议只接受文本投影，修订按 item/Unit 完整目标 |
| 校对已有源文和译文输入；耗尽后返回空建议，应用步骤仍可标 COMPLETED | `engine/agents/workflow.py:782`、`:854`、`:924` | 重构接受状态与五项复核，不是从零添加源译对照 |
| orchestrator 串行循环，会捕获部分 chunk 异常继续，成功 chunk 后保存整本 JSON | `engine/orchestrator.py:429`、`:454`、`:465`、`:473` | 当前不是完全 fail-fast；新要求是作用域明确、公平并发、失败也逐项持久化、全清单恢复 |
| primary 翻译/校对共享请求间隔；fallback 为另一通道，没有完整 attempt/累计额度协议 | `engine/agents/fallback_runtime.py:8`、`:46`、`:52` | 复用适配，不把共享配额误拆或重复计数 |
| 同名 checkpoint 直接覆盖；部分加载错误会回到重新解析 | `engine/epub/parser.py:36`、`:256`、`:280` | 新 ready 运行不允许缺文件就当零计数重新开始 |
| Builder 在多种故障后仍返回预期路径；CLI 可把 None 显示为成功且异常未 raise Exit | `engine/epub/builder.py:606`；`engine/orchestrator.py:556`；`main.py:39` | RunResult/publish 意图/实际产物哈希/非零退出统一契约 |
| 现有字体保护与回归较多，但不是本轮真实阅读器证据；language 参数未传入固定简中提示 | `engine/epub/builder.py:486`；`tests/engine/epub/test_builder.py:174`；`engine/agents/translator.py:7` | 新默认保留字体；语言规范化为 zh-Hans，不能只改 OPF 声明 |

**推论：** 应沿用当前目录、CLI、Agno、Pydantic 和可用 provider/stream 适配，替换数据归属与执行/发布契约。无需更换框架、迁移数据库或另建工程。

### 2.2 本轮验证

| 命令/检查 | 本轮结果 |
|---|---|
| `.venv/bin/python -m pytest -q` | **435 passed in 1.55s** |
| `.venv/bin/pyright` | **0 errors, 0 warnings** |
| `.venv/bin/ruff check --config pyproject.toml main.py engine tests --statistics` | 239 项既存问题，未自动修复 |
| `.venv/bin/ruff format --config pyproject.toml --check main.py engine tests --output-format concise` | 5 个文件需格式化，51 个已符合 |
| 源码状态 | HEAD 未改变，业务代码无 diff |
| 小型提取探针 | 裸文本 prefix/suffix 未入 chunk；独立 img alt 未形成任务；字符计数替身下超长叶节点超过 limit 仍单块。证明控制流，不证明实际模型 token |

**未知/未执行：** 真实 provider token/错误/费用行为、完整 EPUB 模型回归、EPUBCheck 和阅读器验收。仓库已有 `tests/chapter1.xhtml`、`tests/toc.ncx`，但本轮未获得完整授权书样；新版提及的 `examples/document.json` 等附属探针未在仓库找到。以正文完整字段表为准，P00/P01 用合成 fixture 验证契约，不把缺失示例当作已有实现。

## 3. 目标架构、职责与唯一数据模型

```text
CLI / 冻结配置
  → Prepare：安全源快照、源规范检查、包清单、支持范围
  → Extract：源槽位/Unit/上下文/词条/本地 g/x/b 注册表
  → Persist：逐份 document JSON 回读；初始 UnitRecord/CutPlan/额度
  → ready bookplan.json 最后提交
  → 从 JSON 建全书有界 ready 队列
      → 翻译 → 本地检查 → 独立源译校对 → 有限修订/完整复核
      → 每项成功/失败经单写入器立即合并 UnitRecord
      → 标题派生/章节衔接满足依赖则入队，否则保存 blocked_dependency
  → 排尽本轮可执行工作后汇总；局部未决留下 JSON，不导出部分书
  → 全部门槛齐备：装配 → 收尾 → 独立比较 → 临时包/EPUBCheck
  → publish 意图 → 目标锁下提交 → completed / 真实报告
```

网络 worker 只返回结果；它既不改 DOM，也不覆盖整份旧 UnitRecord。协调器持有单写入器；reader 按需加载文档，不让整本 DOM 常驻内存。不把各方框变成独立服务或单实现 Protocol/factory。

### 3.1 对象与模块落点

| 对象/职责 | 最小落点 | 边界 |
|---|---|---|
| BookPlan、DocumentPlan | `engine/schemas/epub.py` | 新模型与旧 EpubBook 分开；准备后不可变 |
| Unit、CutPlan、Segment/item、UnitRecord | `engine/schemas/chunk.py` | 旧 Chunk 仅供旧引擎；普通 Unit 也使用一个 Segment |
| 局部/运行状态、RunResult | `engine/schemas/translator.py` | accepted 为 Unit 版本结论，completed 为整书最终结果 |
| 翻译/review/衔接响应、RequestManifest | `engine/agents/schemas.py` | 磁盘身份由本地生成，模型只回传规定字段 |
| 安全 XML/序列化、源地址 | `engine/core/markup.py` | 保留支持 EPUB 2 的可信本地适配；禁止不受信实体联网/读本机；完整 source_markup 是权威副本 |
| 包预检/准备与 JSON 加载入口 | `engine/epub/parser.py` | 不再把可变译文写入整本 Parser JSON；不在 resume 中重切整书 |
| 槽位/语义 Unit/只读源语境 | 新 `engine/item/extractor.py` + 现有 precode/glossary | 复用结构保护反例；语境先为提取辅助函数，不新增平台 |
| 严格事件 codec/局部注册表 | 新 `engine/item/inline.py` | g/x/b、转义、父范围、固定边界、虚拟切片边界 |
| CSS 三态移动许可 | 新 `engine/core/styles.py` | 复用 tinycss2，有限扫描；不算级联/布局 |
| 请求预算/单片初始计划/长片规划 | 新 `engine/item/planner.py` | Unit 源定义不变，Batch 临时，CutPlan 保存于 UnitRecord |
| 原子文件/写锁/合并/恢复计数 | 新 `engine/services/store.py` | 一个写入器；持久化失败阻止新付费调用 |
| 请求调度/provider/attempt | `engine/agents/fallback_runtime.py`、`models.py`、`streaming_openai_like.py` | 一个调度入口，按配置服务商配额域限流；保留适配，不增智能路由 |
| 翻译/校对/衔接状态流程 | 现有 agents/translator、proofer、workflow、verifier | 固定有界流程；已知局部失败作为带 scope 数据返回 |
| 本地装配/最终独立验证 | `engine/epub/replacer.py` + 新 `engine/epub/validation.py` | 验证器不得只信装配计数或 sidecar；允许复用底层解析，不复用提取决策证明自身 |
| 排版/打包/发布恢复 | `engine/epub/builder.py` | 默认不剪枝；实际验证后才能提交 |
| 任务队列/运行结论/CLI | `engine/orchestrator.py`、`main.py` | 依赖检查与最终发布门禁不能挡住其他独立翻译 |

Python 3.13 与已锁定 SDK 不升级。已有 lxml/tinycss2 能力先通过当前版本的安全解析/命名空间测试再使用；如直接引用现有传递依赖，显式声明现用版本范围，不顺手引入另一套解析框架。旧 API 是否继续导出以真实调用者为准，不批量重命名目录。

### 3.2 身份、哈希与三个版本

- 安全 document/unit ID 由 `source_hash + 包资源路径 + 源位置 + extractor_version` 派生。Unit ID 不含并发、Batch、译文；item_id 由本地映射关联 unit/plan_epoch/segment，不让模型拆字符串推断。
- `source_hash`：源 EPUB 字节；`logical_hash`：源内容、规则、zh-Hans、固定源语境/适用词条、提取/协议/提示及生成配置；`plan_hash`：当前 CutPlan；`input_hash=hash(logical_hash, plan_hash)`。
- `target_hash`：当前完整事件流/切片集合的确定性序列化，片段未齐时为 null；`wire_hash`：一次实际完整请求消息（不含密钥）；`output_policy_hash`：单独的输出策略。
- `plan_epoch` 只在整 Unit 重规划时增加；`revision` 在新目标代次/修订/重规划时单调增加；`record_version` 每次写错误/用量/片段等原子记录都增加。三个字段绝不互相替代。
- 初始目标 revision 派发前已落盘；初译重试填补同代缺片，不创建新 revision。保存 s1 的错误/进度不使 s3 的正常响应过期；旧代次/旧计划响应仍拒绝。
- JSON 采用一个版本化的确定性 UTF-8 序列化规则（固定键序/分隔、禁止 NaN/Infinity），记录 `record_hash` 时排除自身字段；不 hash 对象地址，不用 pickle。文件哈希与内容哈希分别命名。
- 运行内冻结源、目标语言、保护/移动规则、提示、模型配置和用户词表。改变源义相关配置新建运行；改变并发/组批不失效有效 Unit。纯 output policy 变化重装配/验证；影响移动规则则新建翻译计划。显式追加额度是独立操作，保存授权参数，绝不清零计数。

### 3.3 必须落盘的字段

DocumentPlan 按需求 §10.2 完整实现：`format=epubox-document-1`、document_id/source_hash、resource(path/media_type/字节sha256)、adapter/extractor_version、source_markup、nodes、source_slots、units、每 Unit registry、boundaries、derived_bindings、preparation_issues。原文从原字节按 UTF-8 解码，不做换行转换/strip；node_key 对应根起元素子序号路径和 qname，注释/PI 不占元素序号但仍登记模板/保护所有权。原文/计划不可分别改动。

UnitRecord 按 §10.4 实现：`format=epubox-unit-1`、unit_id/document_id/source_hash、logical_hash/input_hash、plan_epoch/record_version/revision、cut_plan、items、candidate、accepted_revision/accepted_target_hash、local_checks/review/unresolved_issues/counters；补齐 target_hash、上个有效/历史 accepted 候选来源。不能投影的已登记局部单元可 cut_plan/input_hash=null，初始 needs_attention、不可派发，但仍在必需清单。

items 至少有 identity、stage、status、target、target_hash、检查、请求来源和失败/下一步。状态限定为 `pending/in_flight/candidate/local_valid/reviewed/retry_wait/needs_attention/blocked_dependency`；Unit accepted 单独按版本/检查派生。错误至少 `scope/stage/code/message/request_id/item_id/plan_epoch/revision/retry_action`，动作枚举 `automatic/explicit_retry/repair/dependency/none`。

RequestManifest 保存 request_id、协议/目标 item 映射、plan_epoch/input_hash/base_revision/目标哈希、wire_hash、实际请求所需参数及 attempts。每 attempt 独立 attempt_id、affected_items、预留、状态、已知 usage；identity 派发后不可变。大响应可裁剪/移除，身份与轻量计数留到运行结束。

DocumentStatus 存 checks 文档，含当前参与 Unit 版本向量、初始窗口/额度、衔接状态与缺失依赖。派生导航等仍拥有独立源 Unit；其可变 derived 记录放对应 UnitRecord，保存来源 Unit/revision/target_hash、纯文字值和本地检查，不冒充独立模型审查通过。

## 4. JSON 准备、保存与恢复协议

### 4.1 固定文件结构

```text
work/<source_hash>/<run_id>/
  source.epub
  bookplan.json
  documents/<document_id>.json
  units/<unit_id>.json
  checks/<document_id>.json
  requests/<request_id>.json
  staging/
  publish.json
  report.json
```

每份包内 HTML/XHTML 都有明确文档记录（包括合法全保留文档），NCX/OPF 使用同一外壳和有限适配函数；二进制资源留源快照，不转 base64。非 manifest 引用资源先在预检解决清单可信性，不能静默漏列。输出另存；原路径、快照及其符号/硬链接别名不能作目标。

### 4.2 准备提交顺序

1. 建立源快照并复核哈希，预检以实际快照为准；复制期间源变动则重做。安全读取 container/OPF/manifest/spine；源 EPUBCheck ERROR/FATAL 阻断，WARNING 记录。工具缺失、XML/资源/支持边界或输出配置失败时零模型调用。
2. 按文档解析/提取/覆盖核对，原子写 DocumentPlan，再从 JSON 回读验证 source_markup、地址、槽位、投影、g/x/b、属性；随后释放 DOM。模型请求只允许读投影/context/terms/hints/constraints，禁止整个 DocumentPlan/source_markup 泄入请求。
3. 从已存源 Unit 构造初始 CutPlan、revision、pending/局部 needs_attention、Unit/初始窗口额度，初始化所有 UnitRecord。初始计划保证翻译与校对都能预算；总任务清单包含无法投影的已隔离范围与派生标签。
4. 所有文档/初始任务存在并核验后，最后原子写 `bookplan.json`，`preparation_state=ready`，记录冻结配置、每份 document 文件哈希、完整必需 Unit IDs/总数及初始额度。
5. 每次启动派发先加载并验证 ready；ready 前中断可续准备缺文件，保证还没有付费调用；ready 后缺文件不能走该“重新准备”分支。

文件原子提交统一为同目录临时文件 → flush/fsync → replace → 平台适用时同步父目录。准备源计划只读；单运行用 OS 进程锁（目标平台原生锁）而非仅凭 PID 文件推断存活；网络任务无写权限，写锁丢失/磁盘满/持久化失败全局 failed 并停止派发。

### 4.3 请求派发的崩溃顺序

协调器先原子记录 RequestManifest attempt 预留，再更新所涉 Unit 的 in_flight/额度，全部持久化成功后才允许 HTTP。真实每次 HTTP（包括传输重试）都创建新的 attempt_id；同 request_id 的重试也分别计数。全局请求次数每 attempt 一次；所涉多个 Unit 各占一次自身额度，但不能相加变成全局次数。

恢复以可信 `(request_id, attempt_id)` 幂等对账。只写 attempt 尚未更新 Unit 就崩溃，保守占用该预留；只有本地预留而未确认发出的请求标 unknown/reserved，不声称已计费。已发送但结果未知的请求不能当作未发生或完成；允许剩余额度内重试，不能保证远端恰好一次。若任一预留写失败，禁止该请求出网并转全局存储失败。

### 4.4 单写入器合并

`apply_item_result(manifest, item_result)` 必须在最新 UnitRecord 上检查 item、plan_epoch、input_hash、目标代次及被审查目标哈希，只合并该 item 的 delta，再推进 record_version。不能用 worker 的整份旧记录覆盖 s1/s3，也不能拿 record_version 变化判断目标 revision 过期。重复回调对同 request/attempt/item 幂等；冲突候选拒绝并留证。

初译同代可补片；修订新建目标 revision，未改片段只能本地显式继承并注明哈希；整个 UnitRecord 原子提交目标集合。重规划在一次 UnitRecord 提交中升级 plan_epoch/revision、登记新 CutPlan、使旧片段/接受/审查失效；DocumentPlan 不变，累计次数不变。

### 4.5 恢复决策

恢复从 ready BookPlan 全清单开始，不使用“最后成功序号”。顺序为源/BookPlan可信性 → 文档哈希/适配版本 → 请求预留对账 → UnitRecord结构/哈希/计数 → 本地接受校验 → 就绪步骤/依赖重建。

| 保存状态 | 动作 |
|---|---|
| 当前 accepted 有效 | 复用；只补过期衔接/派生/发布 |
| 有合法目标、review 未完成或失败 | 从 review 继续，不重翻；耗尽/需修复状态需显式激活 |
| 同 CutPlan s1/s3 成功、s2 失败 | 仅补 s2；s3 未尝试时照常派发；不强制整 Unit 重规划 |
| pending/in_flight/retry_wait | 核验身份/计数，按剩余自动额度和退避恢复；远端未知保守记录 |
| needs_attention/自动额度耗尽 | 其他工作继续；显式失败项重试/修订导入/追加额度后才激活选定项，保留历史 |
| blocked_dependency | 依赖当前有效则入队，否则保存缺失 IDs，不占 worker/额度、不永久等待 |
| 单 UnitRecord 缺失或损坏 | 隔离 Unit 及依赖，其他可信任务继续；不能重建为零计数/accepted |
| 单 DocumentPlan 缺失或哈希不符 | 隔离该文档及依赖，其他已核验文档继续；普通 resume 不静默重提取 |
| source.epub/BookPlan 身份或共享清单不可信 | failed，不继续付费 |
| publish 意图存在 | 先按 §9 核对是否已经完成提交，不能凭路径存在推定成功 |

显式损坏修复仅在相同源快照/适配器版本能生成与 BookPlan 登记完全相同哈希时恢复 DocumentPlan，否则新运行。结果无证据不能 accepted；未知计数用可信 RequestManifest 保守恢复，证据不足须明确确认追加额度。旧 v2 checkpoint 仍由旧引擎读取，新引擎不迁移 HTML target、不删除旧文件。

## 5. 提取、受限格式与请求规划

### 5.1 范围和源归属

预检支持静态可重排 EPUB 2/3；不因代码、数学、图表、复杂列表、脚注拒绝技术书。正文加密、动态生成正文、未适配固定/混合阅读包、正文音频同步、无法处理签名关系停止正式翻译；字体混淆与正文 DRM 分开，标识及混淆资源保留。独立音视频/图片保留资源，报告未翻译其中内容。

完整清单盘点 manifest，spine 仅管主阅读顺序；非 spine 尾注、linear=no、导航独立提取且同资源一次。源规范 ERROR/FATAL 与全局 XML 不可解析先阻断；能划定确定源范围的局部投影/语义布局问题可登记 needs_attention 后让其他 Unit 继续；不能定位归属的内部异常不可伪装局部失败。

每个 `.text/.tail/白名单属性` 的完整源字符串分配连续区间，恰好归应译 Unit、保护、结构空白或事先声明范围外。独立全树枚举与 extractor 结果对账；结构所有权与属性所有权分开。空白有去向，xml:space/预格式/代码不可 strip；注释、PI、DOCTYPE、命名空间与依赖前缀的值必须保留。

语义 Unit：完整段落/标题/引文，递归列表与单元格文本，容器裸文本+相邻内联形成虚拟区域；不增加 p、不同时让祖先/子孙拥有同一文字。表格保留 rowspan/colspan/headers，明确表头用现有关系，不明确仅给有限坐标/邻近源文。图注独立，figure 的大媒体不送模型。

硬保护代码/公式内部优先；其余按有效 translate=no/yes 继承和冻结用户例外；yes 不能越过硬保护。图片现有非空 alt/title/aria-label/aria-description 可独立翻译，SVG/MathML 图形内部及嵌套说明整体保留；不翻译 ARIA ID引用/状态。原有中文保持，混合段中的应译英语继续处理；不做繁简转换/多语识别平台。

OPF 主书名/文字简介默认为 Unit，多语言备用标题保持；归属不明保留元数据并报告合法范围。作者/出版商/ISBN/原出版日期/标识不变。已有用户词典导入兼容为 `source/target/scope/mode/note`，mode=`preferred/required/keep_source`；旧用户明确锁词仅在其声明语境/范围适用，不将自动候选全部升级为 required。空词表合法，显式坏路径/坏文件报错；可选 NLTK 候选保留离线边界，不建自动发现或 sense 分类平台。

### 5.2 g/x/b 与 CSS

- g：可译内联范围；x：代码/公式/媒体/脚注等原子对象（根 tail 不随复制）；b：仅约束用虚拟区间，装配消去。属性/纯文本元数据不接受控制标记。
- 源投影先转义反斜杠与字面定界符，再 JSON 编码。响应先 strict JSON 再按字符扫描投影；未知、重复、交叉、换父、悬空转义拒绝。普通尖括号作为文字经 XML API 转义，不解析模型 HTML。
- 获准的 g/非硬 x 只在同父、同 b 区间内整体移动，不删除/复制/拆合。脚注同父引用相对顺序固定，另做语义绑定审查。
- br/页码/结构空锚点构造 `b1→x1→b2`，向 Unit 根锁定含硬边界祖先的相关兄弟顺序；b 不横跨 g。无法构造合法不重叠约束的局部 Unit 保留待处理，不删锚点。wbr 保留但不等同页码，不宣称中文字符偏移相同。
- CSS 仅读 inline/style/本地样式表和有界 import，条件规则所有分支保守收集；许可三态 `reorder_allowed/locked/unknown`。完整读取且只含支持的无顺序依赖类型/class/id/通配/子代/后代选择器才可能允许。
- 位置/兄弟/伪元素/生成内容/计数器/未知规则：能可靠定位则锁父范围，不能则文档内联锁序；不拒整书、不算级联或样式物化。ruby/方向性/块级语义不能当普通强调。`:lang`/属性选择器受译文影响列为显式变化及版本样本验收，不虚称锁序解决一切。
- 无关长属性测试只用规则未引用的 data-trace 等；class/style/translate/href 改变可能合法影响计划，不能沿用旧“长属性一律无关”断言。

### 5.3 请求与严格响应

保持需求 wire 协议字段：翻译 `protocol=epubox-text-1,request_id,target_language=zh-Hans,items[{item_id,source,context,terms,hints,constraints}]`；输出同协议/request_id及 items 的 item_id/target。request manifest 本地绑定 Unit/hash/版本/注册表，不要求模型算 hash。上下文固定书名/标题/前后源文/必要表头脚注；短代码只读提示，必要代码仅给预算内原片段，不生成未经核实摘要。

拒绝重复 JSON 键、非法数值、异常深度/字节/数量、非法 XML 字符/孤立代理；限制集中配置，超过不得截断 target。顶层 protocol/request_id/schema错误或截断使整批无效，不抢救半数组；顶层完整合法时 item 缺失/坏字段仅影响该项，重复 ID 的全部候选无效，未知 ID 不写任何 Unit，正常独立项逐项保存。

翻译和 review 两类最终消息均满足输入+输出预留+余量及 provider 单独上限。准确 tokenizer 可用才使用；否则保守估算记录误差。先缩 Batch，单 Unit 仍超预算才 CutPlan；同批其他待译项不充当隐式语境。schema、hints、源+目标、JSON开销都计入，不凭英文源长度估中文输出。

## 6. 执行、额度、校对与结束逻辑

### 6.1 不因局部失败提前结束

所有 ready 文档的可执行 item/步骤按稳定清单进入有界队列，默认并发 2（并发 1 也要通过隔离测试）。状态由协调器持有，普通请求和到期重试公平轮转；内容失败隔离单项、持久化后让出位置。依赖等待另存 checks/derived，不压住 worker。

worker 只将明确已知且有 document/unit/item ID 的内容、协议、请求错误转换为 scope 结果；不能 `except Exception: continue` 吞共享存储/内部错误。可采用普通 asyncio queue+有界 worker；若用 TaskGroup，局部错误不能以未处理异常取消正常兄弟，显式取消清理后传播。

每个响应或失败立即落盘，不等整章/整书。缺失 review 不阻止其他初译；长 Unit 缺 s2 不阻止 s3；章节衔接或标题派生缺依赖不阻止下一章。仅当 ready、in-flight、可自动到期 retry 均空，才汇总；需要人工解除的 dependency 不造成永久等待。

| 作用域 | 动作/最终影响 |
|---|---|
| item 内容/拒绝/协议/局部 review、Unit 额度耗尽 | 在额度内重试，否则 needs_attention；其他任务继续 |
| 一批整体 JSON 无效/暂时网络异常 | 该请求项缩批或隔离重试；其他 Batch 继续 |
| 一章衔接失败/额度耗尽、明确派生等待、可隔离局部提取/装配限制 | 挂起该章/依赖并记录；独立工作继续 |
| 429 | 全配额域冷却；共享服务恢复上限耗尽转 paused，不能让大量 Unit 各自烧完额度 |
| 鉴权/余额/共享模型配置/确认服务持续不可用、运行总额度、用户取消 | paused，停止全局新派发，尽力保存已返回结果 |
| 源/BookPlan不可信、不能隔离内部异常、磁盘满/锁丢失/持久化失败 | failed，禁止继续付费 |
| 发布校验/提交失败 | failed，但保留已接受译文，重做装配/发布而非翻全书 |
| 迟到响应/过期人工修订 | 拒绝覆盖当前版本，独立工作继续 |

运行中显示 `execution_state=running/draining/stopped`。结束优先级 **failed > paused > needs_attention > completed**；只有全部可执行独立工作处理后仍有局部未决，才 needs_attention。发生全局暂停仍保留局部问题。第一次取消停止新派发并尽力保存返回；强制取消立即终止，后续按 JSON 恢复。

### 6.2 额度是一份账

- 翻译/校对/修复/衔接共用入口，按实际服务商配额键限 RPM/已配置 TPM/在途量。保留当前 provider 能力，不新增智能选择器；本次运行模型配置固定，失败不能自动换一个未记录模型身份。禁用 SDK/HTTP 客户端隐式重试，或以相同 attempt 钩子覆盖每次真实 HTTP，不只数外层 Agent.arun。
- 一个初译周期每 item：初译 1、协议修复≤1、定向重译≤1；review 1、修订后完整复核≤1。仅唯一一次章节衔接修订可启动指定 Unit 新修订/复核周期；产生 revision 本身不续额度。
- 传输重试≤2/逻辑请求，初次也计 HTTP。`unit_http_limit=24×max(1,初始Segment数)`，重规划新增片段共享剩余；只读衔接另用 `6×初始窗口数`，生成式 Unit 修订消耗 Unit 额度；运行上限默认≤初始 Unit+衔接额度之和，可更低。
- Unit 限额只挂起该 Unit；章节限额只挂该章衔接；运行上限才全局 paused。共享服务恢复上限、RPC超时/退避上限为显式本地配置，P06 以有限故障测试确定保守默认，不能留无限等待。
- 计数按实际 attempt 唯一键；一批影响多个 Unit 不重复增加全局数。重规划/revision/恢复不清计数，显式提高额度记录变更，只有已知 usage/可靠价格才显示费用。

### 6.3 Unit review

新鲜独立源译调用，不延用初译聊天自我肯定，不提供文件/代码/网页工具。五项 checks=`accuracy/fluency/terminology/bindings/script`，取值 pass/fail/uncertain/not_applicable；accuracy/fluency 必审，术语/绑定的 N/A 由本地计划决定，script 对新生成文字检查简体（原中文保留不做繁简转换）。绑定源/目标范围从真实目标事件流提取，不信模型另报对应表。

`protocol=epubox-review-1`，request_id/items/item_id/base_revision/decision/checks/issues；issue 至少 code/severity/message，severity=minor/major/critical。

- no_change 禁止 target，必需检查满足且没有未解决 major/critical/uncertain；minor 有警告可接受。
- replace 必须完整 item target；旧 checks 不适用于新目标，新候选重新完整本地检查及五项独立 review。再次要求修改/无法判断则 needs_attention，不无限循环。
- needs_attention 禁止 target，保留候选和问题；不晋级、不恢复原文当完成。
- 本地按 manifest 的 input_hash/plan_epoch/base_revision/目标hash拒绝过期结果。`accepted_revision == active revision` 且 accepted_target_hash、local_checks、review 全部匹配才可消费。旧 accepted 留历史；若已发现必须修复问题，修订失败不能回滚后抹掉问题。

### 6.4 衔接、派生与人工出口

只有实际参与叙述/表格/注释衔接的 Unit 当前均 accepted 才冻结本章版本向量启动；属性和独立元数据不作叙述前置。初始窗口按源关系登记：长 Unit 每个切片接缝、同小节连续叙述相邻 Unit、表格/注释自己的关系；无需要检查的关系用本地 not_required，不能凭空发空请求。

窗口给源文和冻结译文，只返回 issues/所涉IDs，不返回章节译文。一次自动衔接修订轮内只改所指 Unit，重新走完整单元接受，再冻结新向量扫描；再有重大问题则 needs_attention。任何参与者后改使本章完整衔接记录过期，独立属性不触发；其他章节不重翻。只读接缝摘录偏移仅用于语境，绝不写回。

导航纯文本复用须有明确资源/fragment绑定、源纯文本相同、目标无不适合导航原子；derived值从当前 accepted 标题取文字投影，自己的源Unit/分母保留。来源修订则 derived 过期/blocked，重算后本地检查；不可靠则独立翻译+review。仅维护源计划声明的这些有限依赖，不建动态图。

最小人工修订文件有 unit_id/base_revision/plan_epoch/完整target；长Unit给完整切片目标集合。导入新候选后同样结构/review/衔接；过期拒绝，不能编辑 accepted=true 绕过。明确源计划损坏修复与模型失败重试是不同入口。

## 7. 长 Unit：只允许受控整体重规划

CutPlan 有 plan_epoch、连续源区间、segment/item IDs、g/b范围栈、虚拟起止标志和哈希。按句/从句切事件流，不能截断原子或 Unicode 字符簇；专业缩写/版本/小数用反例；若缺可靠字符簇实现，保守不在可疑簇内切，不加新库凑通用分词平台。

跨切片同一g/b的虚拟开闭位置/栈固定。各片独立验证虚拟重复；完整Unit再检查实际g/x恰好一次；拼接只消去登记虚拟边界，不复制ID、不加空格/删重复/挪标点。不跨b搬文，不改变外侧父关系。

s1成功/s2失败/s3成功均即时保存；同plan_epoch恢复只补s2，完整候选/target_hash等片段齐备。MVP可等齐后才review，各片review绑定相同计划/目标片段/语境才可复用。修订片段后整Unit原子提交，所有接缝重新检查。

翻译/校对超限先缩批/去非必要上下文；未切片目标仍装不下review，或接缝语义需要联合窗口时，从源生成新CutPlan。最多一次自动整Unit升级，旧切片全部过期，revision不归零；其他Unit保留。不得按英文偏移切中文或嫁接新旧片段；累计额度不增长，不保证无限细分能完成。

## 8. 本地装配与最终独立核对

顺序固定：源模板 → accepted目标g/x → 消去b/虚拟边界 → 本地源目标sidecar → 属性/导航/元数据 → 已登记语言/既有输出变更 → 序列化重读。模板/重建区域/原子子树结构所有权唯一；旧XPath只对不变源定位，不在被改树上反复定位子孙。

图片恢复后才应用alt；冻结比较明确排除登记过的具体属性变化，其他全核对。未改二进制逐字节复制，资源路径/字体混淆/标识不变；注释/PI/DOCTYPE/namespace及依赖前缀的值保留。XML API写文字，模型尖括号不能造元素。

最终验证从源快照独立枚举槽位与结构基线，以已验证目标事件流计算许可变化，再读取实际输出核对：每源区间唯一去向、每Unit消费一次、实际目标文字=当前accepted译文、容器/列表/表格顺序、g/x身份父关系、冻结数据、真实属性值、引用。不能掩掉全部目标文本只比较标签，不能信sidecar自报。故意删中文字/重复tail/改href/漏alt必须失败。

ID按单资源唯一，不全书唯一。内部URI按资源基址规范化相对路径/百分号/fragment，特殊不可支持基址预检报告；外部链接保持，不进行网络可达检查。NCX/head-title/OPF纯文本不写格式marker。

目标语言 **zh-Hans**，更新对应dc:language及已译元素lang/xml:lang，合法保留外语必要时保持原继承语言；没有承载元素的混合词不新增span，报告粒度限制，代码不因关键词自动标英语。EPUB3唯一包级dcterms:modified为UTC，EPUB2不塞EPUB3字段；原日期/作者/identifier不动。书名相关file-as等可确定派生值本地更新，不能确定则保留并报告，不生成新事实。全部允许变化写ChangeSet。

默认字体/CSS原样保留，关闭剪枝；已有中文显示策略只有真实样本证据、可关闭且ChangeSet记录时才复用，不新造策略或内置字体。output_policy纯变化重装配和检查，不重翻；影响移动许可则不复用原逻辑计划。

## 9. 正式发布、CLI 与报告

正式门禁：输入/范围有效；全部必需Unit当前accepted或有效derived；无阻断问题；所有必需衔接绑定当前向量；最终结构/资源检查通过；EPUBCheck已执行无ERROR/FATAL。WARN/minor记录不自动失败。每书reader_check未执行为not_run；人工阅读是版本验收，不给日常运行加人工批准门。

发布协议：

1. 从源快照和有效JSON在staging构建临时包，保持源EPUB版本、mimetype首项不压缩、container/manifest/spine；identity测试不应用翻译收尾。
2. 关闭并重开临时包，验证内容/资源/引用与EPUBCheck，计算本次字节sha256。
3. 原子保存publish.json意图：run/plan指纹、当前完整有效结果版本向量、目标路径/hash及验证记录。
4. 持目标独占锁，在目标同一文件系统准备已验证临时文件并原子替换，再标publish完成、生成报告。不同文件系统staging不能直接replace；拷到目标文件系统临时文件后核对同一hash再提交。
5. 默认已有目标就报错、不覆盖；显式overwrite才允许替换经过检查的旧目标。拒绝输入/快照及符号/硬链接别名，提交前锁内重查；多进程不可互相覆盖。
6. 替换后进程崩溃：resume核对可信意图/目标hash/当前向量，匹配则补记completed，不重翻。无意图/不匹配/旧文件不算成功；保存已接受译文以便仅重做发布。

RunResult字段：status、output_path/output_sha256、work_dir、structural_check、semantic_review、coherence_check、epubcheck、reader_check、unresolved_issues。只有completed给正式路径及退出码0；paused/needs_attention/failed全部非零且output_path=null。先兼容现有0/1调用习惯，具体原因用status/report表达，不凭空承诺旧CLI已有细分退出码。

保留translate/generate-glossary命令；新增仅必要的选择新引擎/运行目录resume/修订文件/显式失败项重试/明确追加额度/overwrite入口，准确参数名在P09按现有Typer统一。--language接受Chinese/zh/zh-CN/zh-Hans等简中别名并规范化zh-Hans，其他语言预检报不支持；--limit作为请求预算控制，不改变源Unit身份；--preserve-fonts仍可接受，新引擎默认已保留。无快速/草稿选项。

report从全清单和有效JSON派生：必需Unit总数、accepted数、有效derived数、待译/待review/局部失败/等待依赖、文档/阶段/源位置、in-flight、累计HTTP和已知token、下一步动作、历史错误与当前未决分开。完成率=(当前accepted必需Unit+有效derived必需Unit)/固定必需总数；保留内容另列，片段错误/待做诊断非互斥不能相加作总量；失败/缺损不缩分母。report坏可重算，不作唯一状态源。

## 10. 实施任务与依赖（全部待确认后实施）

每项都有需求落点、负责代码、可运行交付与验收。只在确认后补缺失回归并修改；不把未来T01–T32写为本轮通过。P00只登记旧行为和新预期，后续任务实现时再使新契约测试变绿。对既有lint按触及范围处理，不把全库格式化加入MVP。

| ID | 阶段 | 交付与主要文件 | 依赖 | 验收/本任务边界 |
|---|---|---|---|---|
| P00 | A | 事实基线、旧行为反例、合成EPUB2/3、T01–T32计划、版本验收清单；tests/README | 无 | 现有测试可运行；固定样本/阅读器/人工评分/模型预算的已有值与待补项；不先审68项，不声称新行为通过 |
| P01 | B | schemas契约、确定性hash、状态/三版本、store原子写/OS写锁/record_hash基础 | P00 | 普通JSON往返、非法schema拒绝、原子文件/锁检查；只消费给定hash，真实输入hash由P03/P05接通；不宣称已实现网络恢复 |
| P02 | B | parser/markup/validation：快照、manifest/spine盘点、输入支持、安全XML、源EPUBCheck、输出路径预检 | P01 | EPUB2/3/混淆/签名/音频同步范围、路径/资源/实体限制；工具缺失或源ERROR/FATAL零调用；WARNING记录 |
| P03 | B | extractor/precode/glossary：源槽位、结构/属性所有权、完整Unit、固定语境/词表、OPF/导航源绑定 | P02 | T01/T05/T14/T18提取部分；translate继承、body/tail、混排、元数据/保留范围；独立覆盖无漏重；没有空模型任务 |
| P04 | B | inline/styles：g/x/b codec、有限CSS、hard boundary与父域许可 | P03 | T02–T07格式部分；合法调序通过、非法父/重复/交叉/越界拒绝；未知CSS局部/文档锁序，不默许删样式 |
| P05 | B | parser/store/planner/replacer/validation：单片基础计划、逐document回读、初始Unit记录、ready最后提交、JSON-only identity装配/临时包 | P01,P02,P03,P04 | T26跨进程：退出清内存仅从JSON恢复；零模型；原文CRLF/namespace/PI保留；初始额度已落盘；T17独立变异核验；B阶段不翻译/改语言/书名 |
| P06 | C | runtime/models/agents.schemas：普通item严格wire协议、真实请求预算、RPM/TPM/在途/SDK重试控制、预留attempt计数 | P05 | T04/T15/T25协议与计数；预留完成才HTTP；请求/额度中途崩溃无免费重试；模型配置/容量/费用上限确认后才做最小真实请求 |
| P07 | C | orchestrator/workflow/store：全书ready队列、scope错误、公平重试、逐项合并、draining/取消/outcome | P06 | T23/T28/T29/T31/T32初步：并发1/2中间失败后同章/跨章继续；好项不取消；已知局部失败不冒泡、全局错误不吞；不永久等人工依赖 |
| P08 | C | proofer/workflow/verifier：五项review、完整修订/复核、accepted版本、普通章节衔接快照与一次修订轮 | P07 | T10–T13；新目标不能沿用旧checks、major/uncertain阻断；单元/章节失败隔离；不做动态依赖图，长片接缝由P11扩展 |
| P09 | C | replacer/builder/validation/main：标题derived、OPF/语言ChangeSet、资源/默认字体、唯一completed、publish/报告/CLI | P05,P08 | T17–T21基本全链、T32退出契约；普通/常见内联且两阶段预算内的真实小书可翻译→校对→JSON续跑→验证输出；尚不宣称长Unit/MVP完成 |
| P10 | D | planner/inline/store：完整CutPlan、字符簇安全切分、g/b虚拟边界、双阶段预算、一次整Unit升级 | P09 | T08/T09/T24/T31：s2失败不挡s3，同计划复用成功片段；重规划整Unit旧片段失效且计数不重置，兄弟并发不互盖 |
| P11 | D | workflow/proofer/store/main：切片review/继承、全部接缝、章节版本向量复查、完整人工修订导入 | P10 | T06/T11–T13/T24/T29：修改片段重审、全部接缝重核；迟到/旧epoch拒绝；人工导入完整集合后同门禁；衔接/修订额度有界 |
| P12 | D | parser/store/runtime/builder：从全JSON清单恢复、缺损隔离、显式修复/重试/追加额度与崩溃矩阵 | P09,P11 | T16/T21/T27/T28/T30/T31：中间洞、仅续review、ready前/后中断区别、Unit/Document损坏隔离、attempt幂等和publish补记；不迁移旧HTML状态 |
| P13 | E | 整书集成与T01–T32确定性自动检查；tests及验证记录 | P12 | 对小EPUB2/3真实文件执行提取→JSON→翻译替身→review替身→恢复→装配→打包→重开/规范；变异检测与失败继续有证据；mock结果明确标注，T10/T11真实语义与T22留P14 |
| P14 | E | 真实书/同模型源译对照/目标阅读器验收，README迁移和新默认入口 | P13 | T10/T11真实检出与人工评价、T22完整实书；固定内容覆盖、无确定性错误/伪成功，无大面积锁序/拒绝；记录成本/完成率。证据未齐不切默认；保留旧引擎完成旧运行，不删除旧成果 |

**阶段闭环：** A=P00；B=P01–P05（无模型跨进程JSON往返）；C=P06–P09（预算内普通Unit的真实完整主线与局部失败续跑）；D=P10–P12（长Unit、边界、人工出口和故障恢复完整）；E=P13–P14（全MVP整书验收和默认切换）。C仅以无需切片的阶段样本验收，不以拒绝所有长段宣称MVP完成。

P02之后可并行设计原子存储细节与提取/codec单测，正式依赖顺序仍按表；只有契约稳定且不共享写文件时并行实施。orchestrator、workflow、store的集成由单一负责人处理，不能各自维护不同状态机。每阶段都有入口和回归，先新路径通过再接默认；没有额外“旧引擎退役”MVP任务。

## 11. 文档覆盖与验收追踪

### 11.1 按需求章节追踪（防止只覆盖32个用例却漏正文）

| 需求来源 | 必备契约 | 本计划/任务 |
|---|---|---|
| §0 | 唯一正式完成；不加快速/草稿/平台 | §1、§9；P01/P09/P14 |
| §1 | 输入边界/源规范预检/安全XML/translate继承/完整资源与元数据范围 | §4–5；P02/P03 |
| §2 | 七对象、冻结配置、确定性hash、三版本 | §3；P01/P05/P07/P10 |
| §3 | 独立槽位区间、结构/属性所有权、完整语义及只读提示 | §5；P03/P05 |
| §4 | g/x/b与hard边界、严格wire身份、JSON批/项故障分层 | §5；P04/P06 |
| §5 | CSS保守三态、不建级联；无关长属性测试正确 | §5；P04/P14 |
| §6 | 双阶段预算、字符簇/原子安全切片、虚拟边界、整Unit重规划 | §5、§7；P05/P06/P10 |
| §7 | 固定源语境、五字段词表/空坏区别/数字等价/辅助检测边界 | §5–6；P03/P08 |
| §8 | 当前版本accepted、五项完整校对/再审、有限衔接、人工修订 | §6–7；P08/P11 |
| §9 | 非fail-fast、scope与fair retry、共享配额、attempt预留、取消和补洞 | §4、§6；P06/P07/P10/P12 |
| §10 | 固定文件布局、原文+计划、ready最后提交、record合并/损坏隔离/固定分母 | §3–4、§9；P01/P05/P07/P12 |
| §11 | 独立最终文字/结构比较、URI/文档ID、derived导航、zh-Hans/modified/ChangeSet/字体 | §8；P05/P09/P13 |
| §12 | 四outcome、非完成无正式路径、目标锁/别名、publish意图与中断恢复 | §9；P09/P12 |
| §13 | 保留目录/provider，A–E可运行闭环 | §3、§10；P00–P14 |
| §14 | 32项实现后验收、实书/真实模型/人工/阅读器分层证据 | 本节；P13/P14 |
| §15–16 | 实施边界、禁止项、历史68项不恢复成阻塞审计 | §1、§10；全部任务 |

### 11.2 T01–T32：主责任务、输入与可观察断言

每个用例只有一个主责验收任务；基础实现可分布在依赖任务中。以下都是**待实现后的验收**。

| 验收 | 主责任务 | 输入/故障与必须观察到的结果 |
|---|---|---|
| T01 | P03 | body裸文本/多tail/嵌套块/空白；槽位连续完整、唯一归属；P05往返不吞字 |
| T02 | P04 | 同名链接/强调合法换序和嵌套组移动；身份/父关系/原属性保持 |
| T03 | P04 | 缺/重复/未知/交叉/换父/b越界；拒坏项，P07证明正常Unit不丢 |
| T04 | P06 | 定界符/反斜杠/尖括号/非法XML/代理/重复键/截断；正确转义或拒绝，整批坏JSON不抢救 |
| T05 | P03 | code/公式/图片外tail与alt及translate继承；硬保护、tail一次、白名单正确，P05属性后置 |
| T06 | P11 | br/页码/多个脚注；硬边界保持，五项review独立检查绑定 |
| T07 | P04 | first-child前有em/未知CSS/真正无关data长属性；语义正确、保守锁序、任务不被无关属性扰动 |
| T08 | P10 | 多片长链接/嵌套g/b；真实范围仅一次、虚拟边界消去、不复制ID |
| T09 | P10 | 初译装得下但review超限、重规划崩溃；双预算，新旧plan不混、其他Unit保留 |
| T10 | P14 | 比较反转/否定条件丢失/流畅错译/照抄；记录本地与真实模型检出，人工抽查独立于结构 |
| T11 | P14 | 链接错中文/强调错焦点/脚注换论断；结构合法反例进入绑定审查与人工回归 |
| T12 | P08 | replace引入新错误/no_change含major或uncertain；不晋级，新target完整重审 |
| T13 | P11 | 迟到review/衔接后Unit修改/连续修订；过期拒绝、本章向量失效、次数有界 |
| T14 | P03 | 空/坏词表、多义memory、等价数字；合法空表，坏配置报错，不靠次数/格式误锁 |
| T15 | P06 | 缩批/乱序/429/超时/SDK重试；身份不变、实际attempt统一计数、未知费用不写0 |
| T16 | P12 | 半写/同名书/更换配置/锁冲突；不串用或并发覆盖；改配置新run，原有效记录可复用原run |
| T17 | P05 | 实际装配后删中文/重复tail/改href/漏alt；独立内容/结构核对拒绝，不只XML合法 |
| T18 | P09 | 跨文档同名ID/相对编码URI/非spine尾注；按资源解析，尾注完整，外链不联网 |
| T19 | P09 | 有格式标题派生导航、标题后续revision变；仅取文字，独立derived状态和分母，派生失效 |
| T20 | P02 | EPUB2/3/字体混淆/签名/音频同步；正确支持边界，不误升级或误判DRM |
| T21 | P12 | 目标旧文件/磁盘失败/替换后崩溃；不伪成功/伤原书，可信publish意图匹配恢复 |
| T22 | P14 | 真实模型整本书与实际阅读器；正文/代码/表格/脚注/导航可读、质量与成本如实记录 |
| T23 | P07 | 并发1和2、同章中间翻译/review失败；同章后续/下一章继续并逐项保存、分母不变 |
| T24 | P10 | s1/s2/s3中s2失败，退出后同plan恢复；s1/s3复用，仅补s2，齐备再接受 |
| T25 | P06 | 完整Batch缺/坏单项与另一批截断；保留前者正常项、拒绝后者整批，P07验证别批继续 |
| T26 | P05 | 每文档JSON落盘退出再加载identity；原文/Unit/slot/gxb/属性定位齐全，无内存DOM依赖，不发长属性 |
| T27 | P12 | 中间失败/末项成功、target有但review失败；全清单补洞，成功不重翻、只续review |
| T28 | P12 | Unit/章限额后再触发运行限额/鉴权/写盘；局部继续、全局才暂停/失败，计数不清零 |
| T29 | P11 | 失败标题derived等待/失败段衔接等待；不占worker和额度，后文继续，补齐后对应步骤复活 |
| T30 | P12 | ready前中断/ready后Unit或Document损坏；ready前零调用，ready后不重置，局部隔离继续 |
| T31 | P10 | 同代两个Segment并发，一项先记错误/进度；record_version不改revision，兄弟保存，旧代迟到拒绝 |
| T32 | P07 | 首个失败/队列排尽/仅剩人工依赖；既不早退也不死等，needs_attention、null输出、进度保留 |

### 11.3 版本验收和普通运行验收分开

P00准备版本验收清单，P14前落实授权实书、源哈希、模型参数/预算、固定问题句、人工评阅人/维度、目标阅读器/版本。小样本覆盖正文、技术代码、表格/脚注、复杂内联；承诺EPUB2/3则两类都有实际包样本。上轮未收到阅读器选择，暂按README的Kindle方向列为待落实项，不宣称已验证。

确定性目标为源归属/结构/资源/状态/发布错误0；真实源译按相同模型配置对照旧链路，人工分别评忠实度、自然度、术语、格式归属，记录检出/漏检、自动完成率/人工待处理率和成本，不承诺零错译或固定提升百分比。若有大面积锁序或拒绝、关键场景不可用，P14不通过。

缺实书/真实模型/人工抽查/阅读器/规范验证证据时P14保持待验收，新引擎仍可选、旧入口仍默认；不以合成或mock结果代替证据。通过后才切默认；旧运行保留旧引擎与原状态，不安排删除用户数据。日常单书不需要人工阅读器确认即可在机器门槛满足时completed。

## 12. 计划 review 的完成条件

独立 reviewer 必须分别完成：

1. **需求符合性**：逐节核对v2.3与T01–T32，对禁止项做反向检查；识别新增范围、弱化约束或漏项。
2. **架构与可执行性**：检查任务DAG、阶段闭环、ready和发布先后、三版本/并发、额度/崩溃恢复、局部/全局错误边界。
3. 主作者修订后由对应reviewer复查；所有阻断项关闭，最终结论与受审计划哈希写入review记录。工具再检查任务编号/依赖无环、32项一一覆盖、源文档哈希未变、源码无改动。

本轮结束交付本文与review记录；即使review通过，仍等待用户确认任务拆分后才进入P00实施。
