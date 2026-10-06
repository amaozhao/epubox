# 翻译前处理功能清单与迁移边界

日期：2026-10-06\
用途：记录翻译前处理能力、迁移边界及 feature/preflight 的当前交付状态。\
配套文档：[EPUB 语义分块与安全回写方案](chunking.md)。

## 1. 本轮范围与代码基线

本文最初只形成迁移清单；当前 feature/preflight 已实现 T00—T14 的选择性迁移、原子正文请求投影和预算合批。T15—T20、生产接线、真实模型请求及最终 EPUB 发布仍不属于已完成范围。

本次核查的提交：

| 位置 | 提交 |
|---|---|
| 当前分支 codex/epubox-v25-implementation | 7d882be |
| 本地 main | 61ebb35 |

下表提交用于说明最初比较来源，不代表当前 feature/preflight 的 HEAD。

重要事实：main 已经包含 P1—P4 准备管线和大部分术语核心功能。后续不能把“迁移前置术语功能”理解为重新实现全部术语模块，也不能把当前分支整批覆盖到 main。

feature/preflight 已从用户指定的 main 基线建立。translate → proofread → apply_corrections 的正文行为仍由后续 T16/T17 负责，不能把术语阶段完成误报为正文 workflow 已接通。

## 2. 当前翻译前实际执行顺序

入口是 [engine/cli.py](../engine/cli.py) 的 translate_book()；准备阶段由 [preparation_pipeline.py](../engine/services/preparation_pipeline.py) 的 prepare_translation()/resume_preparation() 驱动。

```text
读取命令参数、检查源文件和输出位置
→ 查找已有任务、验证配置及词表身份
→ P1：源书快照、EPUB 校验、文档解析、文本映射、用户词表
→ P2：术语提取计划、批量模型提取、证据验证、断点记录
→ P3：候选归并、用户规则优先、冲突核对、词频统计、词表冻结
→ P4：按冻结词表创建当前格式的翻译计划与初始记录
→ 写入并回读 bookplan.json，准备阶段结束
→ 才进入正式正文翻译
```

如果已存在有效 bookplan.json，准备入口会校验并复用它，不重新提取术语。

P1 不调用模型；P2 和需要时的 P3 会调用模型；P4 是本地规划，不代表正文已被翻译。

## 3. 入口、工作目录和已有任务发现

当前能力：

- 默认目标语言为简体中文，拒绝不支持的目标语言。
- 支持选择模型服务、上下文额度、输出额度、并发数和运行 HTTP 限额。
- 支持用户词表、自动术语提取开关及显式的术语修复入口。
- 默认最终文件名为原文件名加 -cn.epub，并放在原书旁。
- 默认工作资料目录为源书旁的同名目录，内部保留 source_hash/run_id。
- 兼容显式 --work-root 覆盖。
- 相同源书、配置及用户词表重复运行时，查找并复用原任务。
- 存在多个匹配任务、配置不一致或不兼容记录时，明确报告，不静默新开任务。
- 检查输出不得覆盖源文件或其别名；已有输出需要显式 overwrite 才可覆盖。
- 迁移旧默认 work/<source_hash> 时持有源级和任务级锁，拒绝冲突位置或不安全链接。

依据：translate_book()、_existing_run_id()、_default_work_root()、resume_book()，位于 [engine/cli.py](../engine/cli.py)。

迁移要求：保留默认路径、同任务复用和锁保护，但不要把 CLI 中正文翻译后的修复、校对或发布控制一并归类为“前置术语功能”。

## 4. P1：稳定源书快照与 EPUB 输入检查

依据：[engine/epub/preparation.py](../engine/epub/preparation.py) 的 prepare_book()、_stable_snapshot()；[engine/epub/validation.py](../engine/epub/validation.py) 的 inspect_epub()。

当前处理：

1. 计算源书 SHA-256，复制为工作目录内的 source.epub。
2. 比较复制前、复制后与快照的哈希，避免源文件在准备过程中变化；失败时有有限次重新尝试。
3. 保存源快照后设为只读，后续阶段使用该快照。
4. 检查 ZIP 项、资源路径、大小等限制，以及容器文件与 OPF 是否存在。
5. 识别 EPUB 2/3、manifest、spine、正文资源、导航和 NCX。
6. 执行源 EPUB 检查、内部引用及当前支持范围检查。
7. 形成固定的资源清单和阅读顺序。

这些检查的作用是固定输入和发现问题，不是修改源书或提前生成译文。

迁移注意：当前支持范围存在具体限制，例如只接受一个 rootfile，不能在文档中泛称支持任意 EPUB。新的 HTML/XML、head、CSS/JS 保留要求以配套 chunk 方案为准。

## 5. P1：文档解析、可读源视图和结构映射

当前 prepare_book() 读取正文、NCX、OPF，以及用于提取判断的样式信息，再调用文档提取器。相关代码：

- [engine/item/extractor.py](../engine/item/extractor.py)
- [engine/item/structural_extractor.py](../engine/item/structure.py)
- [engine/epub/derived_bindings.py](../engine/epub/derived_bindings.py)
- [engine/core/markup.py](../engine/core/markup.py)

当前生成和保存的内容包括：

- 原始文档内容、资源身份和文档哈希。
- 节点及 text/tail/属性位置映射。
- 可译内容、受保护内容和原有结构的归属。
- SourceTextView：可用于术语提取和引文校验的源文本视图。
- Unit 与文档的对应关系、阅读顺序及部分结构关系。
- 导航标题与正文标题等可派生内容之间的绑定。

所有文档记录持久化后，才提交 preparation.json，作为已完成解析准备的标志。

必须区分“源视图”和“模型翻译 chunk”：源视图用来定位证据、确定作用域，不意味着每个视图都要单独调用一次模型。

迁移要求：保留可靠的源文本、映射和证据定位能力，但不能照搬当前提取器的全部拆分行为。新方案要求 p/table/em/i/ul/ol 整体不可拆，当前旧的固定 1200-token 规划不能继续支配新 chunk。

另一个已知边界：当前准备代码多处直接按 UTF-8 解码资源；这不能等同于已实现所有 XML 声明编码、UTF-16/BOM 情况的支持。新解析方案需要按其验收要求验证编码处理。

## 6. P1：用户词表输入与规则标准化

依据：[inputs.py](../engine/services/terms/inputs.py) 的 load_user_terms()；数据约束位于 [engine/schemas/contracts.py](../engine/schemas/contracts.py)。

支持两种 JSON 输入：

```json
{
  "machine learning": "机器学习",
  "human-computer interaction": "人机交互"
}
```

以及带规则的列表：

```json
[
  {
    "source": "OpenGL",
    "target": "OpenGL",
    "aliases": [],
    "scope": {"kind": "book"},
    "mode": "keep_source",
    "match_policy": "exact",
    "note": "保留产品名称"
  }
]
```

当前标准化规则：

| 字段 | 含义/默认值 |
|---|---|
| source、target | 源词和对应目标；去除首尾空白 |
| aliases | 别名，默认空；去重并排序 |
| scope | 默认 book，也支持明确的文档/单元作用域 |
| mode | 默认 preferred；支持 required、keep_source |
| match_policy | 默认 exact，也支持 casefold |
| note | 默认空，保留用户词条说明 |

keep_source 未提供 target 时，默认使用 source。未知字段、错误类型、无效作用域和有冲突的规则会被校验，不静默猜测。

标准化结果生成确定性的 term_id 和哈希，保存为 glossary/user.json。后续使用保存的副本，避免外部词表文件改动无声影响运行。

必须保留三种模式的区别：

- preferred 是建议译法。
- required 是明确的强制译法。
- keep_source 是明确的保留原文规则。

不能把这些规则全部压成一个字符串字典，再让旧校验器把所有自动建议都当成强制替换。若旧 workflow 只支持简单词典，需要增加最小适配，分别传递指导词条和真正的硬规则。

## 7. P2：术语提取计划

依据：[planning.py](../engine/services/terms/planning.py) 的 plan_term_extraction()、_ordered_primary_views()、_groups()、_context_ranges()。

当前能力：

- 枚举已保存的 primary 源视图，并校验视图 ID 不重复。
- 将长源视图划为可核验的源范围，保持完整字符簇边界。
- 在字符预算内组合源范围，区分正文、表格、注释、导航等内容通道。
- 保存 primary 范围、context 范围、视图哈希、相关用户词条和提取输入身份。
- 生成确定性的提取项 ID、plan_hash 和预算。
- auto_extract=False 时生成明确的 disabled 空计划；用户词表仍保留。

当前默认值与计量单位：

| 参数 | 默认值 | 说明 |
|---|---:|---|
| max_primary_chars | 12000 | 主要源文本的字符预算，不是 token 数 |
| adjacent_context_views | 1 | 邻接关系的选取深度，不等于整个请求只能有一段上下文 |
| context_chars | 400 | 上下文范围的字符限制，不是完整输入上限 |
| item_http_limit | 6 | 每个提取项的 HTTP 额度 |
| resolution_group_limit | 20 | 参与提取计划预算计算的冲突组参数 |

上述值可以被保存的 extraction_config 覆盖，不能在迁移说明中把它们都写成不可调整的通用硬限制。

当前提取计划的总 HTTP 预算计算为：提取项额度之和加上 3 × resolution_group_limit。它不同于正文翻译的 chunk 上限。

**术语提取窗口与正式翻译 chunk 必须分开。** 当前术语窗口按纯文本证据范围规划；新的 EPUB_CHUNK_MAX_TOKENS 及原子标签规则用于正式翻译，不能靠照搬术语窗口来拆开 p/table/ul。

当前上下文可能包含前后邻接范围、显式上下文视图和明确阅读边上的跨文档范围，不能声称已经满足“每次请求只带最近 1–2 个片段”。迁移时需要按用户最新约束重新收紧，而不是原样复制这部分选择逻辑。

## 8. P2：模型提取、批量派发与计数

依据：[runner.py](../engine/services/terms/runner.py) 的 TermRunner；共用请求入口位于 [engine/agents/runtime.py](../engine/agents/runtime.py)。

当前能力：

- 一次请求可包含多个提取项，而不是每个术语或每个 HTML 节点一次请求。
- 使用冻结的模型、提示词和目标语言身份，避免恢复时换错配置。
- 批量请求并发执行，并在空位出现后继续调度。
- 每次派发前保存请求清单、预留 attempt，完成后保存响应和实际 usage。
- 已预留次数与实际发出的 HTTP 次数分开统计。
- 单项异常与其他项隔离；截断批次可以缩小后重试。
- 使用已有响应回放，避免“模型已经返回，但状态未写完”导致重复请求。
- 限额或模型服务要求暂停时保存原因与状态，保留已完成结果。

当前运行默认/限制：

| 项目 | 当前行为 |
|---|---|
| 输出上限 | 默认 4096 tokens，可由冻结配置覆盖 |
| 并发 | 默认 2，可由冻结配置覆盖 |
| RPM | 仅在冻结配置明确给出时限制；不在运行层猜测供应商默认值 |
| TPM | 配置后限制；未配置则不由此项设限 |
| 运行 HTTP 限额 | 显式限额优先，否则使用提取计划预算及合法追加额度 |
| 提取项逻辑调用 | 最多 2 个逻辑提取调用，另受单项 HTTP 额度约束 |
| 冲突核对 | 另有至多 3 次的受控请求/尝试检查 |
| 派发前输入检查 | 完整请求估算不得超过 50000，并考虑已配置 TPM 中的输出预留 |

术语合批目前还使用约 1300 输出 tokens/项的经验值限制项数：

```text
batch_item_cap = min(256, max(1, output_tokens // 1300))
```

因此输出上限为 4096 时，这个经验规则最多先选择 3 项，再按完整输入预算缩小。1300 是经验参数，不能描述成模型规定，也不能把它当作新正文 chunk 的固定限制。

当前分支的输入预算 v2 统计完整渲染消息，使用本地 tokenizer 计数，并预留 50% 分词差异和 256 tokens 包装空间；旧算法保留用于历史日志校验。派发同时检查冻结的输入上限、输出预留、模型上下文和 TPM。接口返回的实际输入超出程序限制时，会停止后续派发。派发前估算不等于服务端精确 token 数。

T08 的通过记录是所有新术语派发的前置条件。执行器在真正预留 HTTP attempt 前重新核对源快照、准备记录、原子清单、映射、模型和预算身份；缺失或不匹配时暂停且不发请求。已保存的模型响应仍先在本地回放，不因凭据暂时缺失而重复请求。

请求清单、attempt、原始响应、提取记录、候选池和冻结词表仍分别保存在 JSON 文件中。reserved 表示已占用额度，sent/unknown/succeeded/failed 表示实际进入过网络边界的状态，两类计数不能互相替代。日志索引和源视图在进程内增量缓存，恢复时以落盘 JSON 为权威来源。

## 9. P2：候选协议与源证据校验

依据：[terms.py](../engine/agents/terms.py)、[candidates.py](../engine/services/terms/candidates.py)。

当前模型只负责提出候选，包括：

- source、target。
- 类别：term/person/organization/product/abbreviation/other。
- 可选别名、scope_hint、note。
- 指向主源视图的 view_id 与原文连续引文 source_quote。

本地校验负责：

1. 检查协议、请求 ID、提取项 ID 和候选字段。
2. 检查引文确实来自当前冻结的 primary 源范围。
3. 拒绝虚构引文、错误视图、只引用上下文或受保护提示的候选。
4. 检查源词及别名有对应证据。
5. 保留合法候选，同时记录被拒候选的原因。

模型不能自行生成最终 accepted 状态、强制规则或扩大实际作用域。提取成功但候选为空是合法结果，不等于提取失败；部分候选被拒也不必否定同项的其他合法候选。

## 10. P3：本地归并、用户优先与冲突核对

依据：[candidates.py](../engine/services/terms/candidates.py) 的 dispose_candidates()；[resolution.py](../engine/services/terms/resolution.py) 的 TermResolutionRunner。

当前处理：

- 归并相同候选及有效证据。
- 用户规则在其作用域内优先，自动候选不能覆盖或绕过用户规则。
- 自动候选的有效范围由源证据及实际适用单元约束，不能凭模型建议无限扩展到全书。
- 同一源词在重叠作用域中存在不同候选时，形成明确的冲突组。
- 只有有冲突时才进行模型核对。
- 核对只能选择已有候选、在允许范围内缩小作用域，或 defer。
- 不能由核对模型凭空创造新的译名或扩大作用域。
- 未解决冲突保留为诊断；不能为了让流程完成而随意选一个。

冲突请求支持批量处理、缺项重试、截断拆批和日志回放，并兼容已有冲突协议记录。

迁移时保留“本地归并为主、模型只处理必要歧义”的边界，不把所有候选重新逐条发送核对。

## 11. P3：源词频统计和词表冻结

依据：[engine/services/terms/freeze.py](../engine/services/terms/freeze.py) 的 freeze_terminology()、_with_source_frequency()；持久化约束见 [store.py](../engine/services/store.py) 的 write_freeze()/write_glossary()。

冻结前：

- 检查提取项已经进入终态。
- 保留用户词条的模式、作用域和备注。
- 自动采用词条以 preferred 形式进入冻结词表，不自动升级为硬规则。
- 按实际 primary 源文本统计作用域内词频，不把模型候选出现次数当成源词频。
- 记录提取缺口、拒绝候选及未解决冲突等警告。

随后生成并持久化：

- freeze_id。
- 源书、准备计划、提取计划、候选池、用户词表和规则的身份/哈希。
- 覆盖与缺口信息。
- 不可变的冻结意图和最终 glossary.json。

冻结后不再修改同一轮的候选池或词表。后续 review 的术语建议不能直接改变当前冻结词表。

当前状态需要区分：

| 状态 | 含义 |
|---|---|
| closed | 提取已经收尾 |
| closed_with_gaps | 已收尾，但有失败或不可规划的提取项 |
| disabled | 用户明确关闭自动提取 |
| not_required | 没有需要自动提取的项目 |
| paused | 仍有未完成工作，需要恢复 |

closed_with_gaps 当前可以形成带警告的词表并继续准备。这不能被解释成“术语已经完整、没有问题”，更不代表正文已完成。

## 12. P4：当前的翻译前计划，以及不能原样移植的部分

依据：[preparation_pipeline.py](../engine/services/preparation_pipeline.py) 的 _advance() 后半段；兼容路径位于 [context.py](../engine/item/context.py) 的 select_terms()/plan_unit()/initial_derived_navigation()；新原子请求接口位于 [request.py](../engine/item/request.py) 和 [packing.py](../engine/item/packing.py)。

当前 P4 会：

1. 读取冻结词表和文档映射。
2. 建立上下文索引。
3. 为当前 Unit/Segment 选择适用术语，区分 target 与 context 角色。
4. 生成逻辑输入、词表、上下文的身份，以及 CutPlan 和初始 ItemRecord。
5. 为可由正文标题派生的导航项建立依赖。
6. 对无法规划的单元保存局部问题。
7. 在所有必需记录准备完毕后，最后写入并回读 bookplan.json。

这些功能中，术语选择、作用域判断、稳定映射和准备完成标志值得保留；**现有 Unit/CutPlan 的具体拆分方式不能原样移植**。

新分支的改造要求：

- 用新原子 chunk 方案替换原先固定 1200-token 源片段规划。
- p/table/em/i/ul/ol 整体不可拆，不得沿用旧拆分后再试图补回标签。
- 在术语模型请求前，先做不依赖词表的原子大小与不可拆约束预检；词表冻结后再做完整请求预算检查。
- 正文模型请求只带相关词条和最多最近 1–2 个上下文片段，不复制全书词表、完整哈希和每项重复背景。
- 术语作用域与证据 ID 必须和新结构映射一致；旧 Unit ID 不能未经转换直接写入新计划。
- 对旧 workflow 提供稳定、精简的准备结果，不强迫它接收当前新执行器的全部状态对象。

T13/T14 已完成可单独调用的纯规划接口：

- `SourceIndex` 从原子清单建立受验证的文档、内容通道与阅读顺序索引。
- `build_payload(stage, items, glossary, index, ...)` 构造完整候选载荷；整批共享最近最多两个前文，各自最多 400 字符，排除全部当前成员，不发送 XPath、源哈希、祖先结构或逐项重复背景。
- 术语只选择当前目标或可见前文实际相关项，完整保留模式、大小写策略、别名和备注，并区分 target/context 角色。
- review 必须读取调用方传入的当前已保存目标和 revision；它不会猜测初译内容。
- `pack_requests(...)` 按资源、内容通道、原子相邻性及 S/I/R/M 预算顺序贪心合批，没有 8 项或 1200-token 隐藏上限，也不切开任何原子项。
- 阶段已完成 ID 由调用方从断点传入；纯函数只返回 batches、boundaries、blocked 和 skipped，不写入或修改断点。单个 review 超限时，已保存初译仍由调用方保留。

这些接口尚未接入生产 P1—P4、正文 workflow 或 CLI；接线分别属于 T15、T16 和 T19。当前旧 P4 继续服务既有格式，不能把独立规划测试解释为整本书已经使用新路径。

## 13. 准备结果的最小交接接口

迁移后的准备阶段应向正文 workflow 交付以下信息，而不是交付一套新的执行器：

```text
准备状态与诊断
工作目录和稳定源书快照
资源清单、文档阅读顺序及结构/源文本映射
冻结用户规则与自动词表
freeze_id、必要版本与哈希
新 chunk 计划及每块相关术语的查询能力
```

workflow 使用这份结果继续 translate → proofread → apply_corrections → 保存。模型只接收本批实际需要的信息，完整身份与证据保存在本地。

对于已有已保存结果，只能在源身份、证据、作用域和配置检查一致时复用。改解析器或改原子归属后，需要明确适配；不能承诺旧 v2.5 JSON 自动兼容，也不能清空原资料后直接重跑。

## 14. 当前落盘文件

以下路径相对于一个具体任务的工作目录：

| 路径 | 内容 |
|---|---|
| source.epub | 不可变源快照 |
| preparation.json | P1 身份、文档清单、阅读顺序、用户输入及配置 |
| documents/*.json | 文档结构、源视图及映射 |
| glossary/user.json | 标准化用户词表副本 |
| glossary/plan.json | 提取项、源范围、上下文、预算及提取身份 |
| glossary/extraction/*.json | 每个提取项的状态、候选、诊断和请求引用 |
| requests/*.json | 术语/核对等模型请求清单和 attempts |
| glossary/responses/<request_id>/<attempt_id>.json | 已保存的术语提取响应 |
| responses/resolution/<request_id>/<attempt_id>.json | 已保存的冲突核对响应 |
| glossary/candidates.json | 候选池、冲突组及核对结果 |
| glossary/freeze.json | 冻结身份、覆盖信息和快照载荷 |
| glossary.json | 正文阶段使用的冻结词表 |
| units/*.json | 当前 P4 生成的单元记录；新 chunk 方案需要适配 |
| bookplan.json | 当前准备阶段的最终提交标志及索引 |

文件名称并不意味着必须把所有当前数据类型照搬进新分支；需要保留的是对应能力、可恢复性和身份校验。

## 15. 相对 main 的迁移清单

本次比较基于本地 main=61ebb35；真正创建新分支时应再次记录并核对基线。

| 部分 | main 状态/当前分支差异 | 迁移动作 |
|---|---|---|
| inputs.py | 本次比较无差异 | 直接复用 main，保留规则语义 |
| planning.py | 本次比较无差异 | 复用核心证据规划，按新要求调整上下文策略 |
| candidates.py | 本次比较无差异 | 复用验证、用户优先和冲突分组 |
| freeze.py | 本次比较无差异 | 复用冻结与词频规则 |
| preparation.py | 当前分支增加阶段进度等改进，也包含翻译配置身份变化 | 选择性迁移前置进度，不盲目覆盖新解析方案 |
| preparation_pipeline.py | 更细准备进度、批量初始化、内存进度快照及暂停原因 | 迁移必要优化，替换 P4 拆分接口 |
| runner.py | 日志索引/缓存、并发批次、批量请求与恢复计数等改进 | 已迁移并保留回归测试 |
| resolution.py | 请求/恢复处理的增量修复 | 按差异核对后迁移，不重写已存在核对流程 |
| store.py | 批量初始化、终态判定修复；还混有正文 chunk 新功能 | 只迁移准备阶段需要的存储能力，避免整文件覆盖 |
| terms.py | 当前差异主要涉及 review 的适用性、版本及问题限制 | 自动术语协议本身不必因这些差异重做；正文 review 另行适配 |
| agents/runtime.py | 共用计量、限流、暂停、预算等改进，也含正文协议内容 | 只选迁移所需共用能力，保留旧日志兼容性 |
| cli.py/main.py | 启动提示、同任务恢复、同名目录及其他正文功能混合 | 单独迁移路径与准备入口，避免复制正文恢复分支 |

尤其应保留已经修过的准备阶段问题：

- 不在每个提取项、每次进度刷新时重复扫描整本书和全部请求日志。
- 初始化与进度统计复用已有记录和索引。
- 已落盘响应在崩溃后回放，并正确收尾其 attempt。
- 已结束的 unknown 审计结果不等于仍在进行的请求。
- 被相同身份的后续成功请求覆盖的旧尝试，不应永久阻塞术语冻结和 ready。
- 保留累计调用和用量，不因重启或修复清零。

这些差异的范围必须以代码比较和测试为准，不能把单个提交的全部文件默认视作纯前置功能。

## 16. 明确不随前置功能迁移的内容

- 当前正文执行器的串行批次循环及其后续未完成改动。
- 将 review 的 replace 当失败、反复校对旧候选的分支。
- 正文人工助手兜底、语义修复多轮状态机及其恢复控制。
- 当前固定 1200-token 分片规则和不满足新原子约束的拆分逻辑。
- 为上述新执行器服务、但原 workflow 不需要的复杂状态与完整提示字段。
- 未经验证的 HTML 纠错后重写、head/CSS/JS 重生成行为。

正文结构保护和回填可选择性复用，但应在 chunk 方案中独立验证，不以迁移术语功能为理由整包引入。

## 17. 迁移验收

| 验收项 | 期望结果 |
|---|---|
| 非法源书、无效配置、错误用户词表 | 在相应前置阶段明确报告，不进入正文翻译 |
| 重复 translate 同一任务 | 复用快照、词表和已完成记录 |
| 用户词表两种输入形式 | 标准化一致，模式/作用域/备注不丢失 |
| 自动提取覆盖 | 每个 primary 源范围有明确计划与处理状态 |
| 虚构引文、错误视图或缺少别名证据 | 候选被拒，原因可追踪 |
| 单项部分候选错误 | 合法候选保留，不重复处理成功项 |
| 用户规则与自动候选冲突 | 用户规则不被覆盖，不偷偷扩大自动作用域 |
| 冲突核对 | 只选已有候选或 defer，不能发明译名 |
| 自动词条 | 保持 preferred，不变成全部强制匹配 |
| 模型已返回后进程中断 | 使用日志回放，不重复请求 |
| 额度、限流、超时及暂停 | 次数真实、原因明确、断点可恢复 |
| 冻结后重启 | 重用相同冻结结果，不重复提取或改词表 |
| 提取存在缺口 | 缺口明确可见，不被记为正文已完成 |
| 新原子 chunk | p/table/em/i/ul/ol 不拆；超限在预检暴露 |
| 向 workflow 交接 | 只传所需文本、术语及映射，不引入新的翻译执行器 |

现有测试参考：

- [test_preparation_pipeline.py](../tests/engine/services/test_preparation_pipeline.py)
- [inputs.py](../tests/engine/services/terms/inputs.py)
- [planning.py](../tests/engine/services/terms/planning.py)
- [runner.py](../tests/engine/services/terms/runner.py)
- [candidates.py](../tests/engine/services/terms/candidates.py)
- [resolution.py](../tests/engine/services/terms/resolution.py)
- [freeze.py](../tests/engine/services/terms/freeze.py)

本文同时保留最初盘点和当前迁移边界。T08/T11/T12 的已实现合同见 [terminology.md](terminology.md)，T13/T14 的独立正文请求合同见 [packing.md](packing.md)；生产接线、正文 workflow 和整本书真实验收仍按 T15—T20 继续，不能由独立规划测试替代。
