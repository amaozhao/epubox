# 历史设计续篇

前文：[设计方案](plan.md)。

## 10. HTML→JSON、结果 JSON 与断点恢复

### 10.1 必须采用的文件结构

```text
work/<source_hash>/<run_id>/
  source.epub                    # 已校验的只读源快照
  preparation.json               # parsed_ready：源索引、document 哈希、用户输入/提取配置
  documents/
    <document_id>.json            # 原 HTML+基础解析计划，不绑定生效术语
  glossary/
    user.json                    # 用户输入的标准化副本；未指定则明确为空
    plan.json                    # 术语窗口/源视图引用/输入哈希/初始额度
    extraction/
      <extraction_item_id>.json   # 每窗目标候选、证据校验、失败与累计次数
    candidates.json              # 收集后的候选、冲突/核对结果和采用原因
    freeze.json                  # 已收口的冻结意图及精确 snapshot_payload
  glossary.json                  # 本轮生效只读快照，冻结之后才出现
  bookplan.json                  # translation ready：引用 preparation+词表，必需翻译任务清单
  units/
    <unit_id>.json                # CutPlan、所用词条、片段/校对、反馈与计数
  checks/
    <document_id>.json            # 衔接版本向量、等待依赖、检查结果
  requests/
    <request_id>.json             # 所有阶段调用身份/attempt/额度/已知用量；无密钥
  staging/                       # 临时待验证产物
  publish.json                   # 本次提交意图/完成凭证
  report.json                    # 可重算的提取、翻译、失败和依赖摘要
```

每个 EPUB 内 HTML/XHTML 资源必须有明确 .json，不只是一个目录；NCX/OPF 沿用相同外壳及对应适配器。图片、字体不转 base64，仍保留于源 EPUB。文件名只使用本地产生的安全 ID，不取自译后标题、模型候选或术语原文。

DocumentPlan 的 unit ID 由源哈希/源资源/源位置/提取版本生成；术语变化不改源身份。source.epub 在复制后复核字节哈希，输入复制时变动则重新准备。工作目录不按书名认领旧解压数据。

`glossary/plan.json` 格式为 epubox-term-plan-1；各 extraction 文件为 epubox-extraction-record-1，含 item_id、源/视图/输入哈希、record_version、状态、原候选/校验结论、request/attempt、错误、counters。候选池为 epubox-candidates-1，含源计划身份、候选/冲突组、已消费响应身份、决策/额度及 record_version/record_hash。它是当前采用决策的权威记录；原提取结果是来源，不在恢复时无条件重建并丢失已有核对决策。普通读预览不改任何文件。

源视图 text/hash/来源完整保存在 document JSON，plan 以 view_id/源区间引用；所有窗口及候选都可从普通 JSON 恢复，不依赖进程内对象。提取记录与候选池按单写入器原子更新；冻结后候选池/提取记录封存，迟到消息仅留请求诊断。后续术语建议保存在 UnitRecord，不继续写这个池。

### 10.2 章节 JSON 保存什么：原 HTML 与已解析计划共同持久化

MVP 采用**原始标记字符串 + 可序列化的解析计划**，不另造完整 DOM 数据库或通用 XML AST 标准。字段必须能从普通 JSON 加载；禁止 pickle、进程地址或只在内存有效的 lxml Element 引用。

| DocumentPlan 字段 | 必须保存的内容/约束 |
|---|---|
| `format` | 固定 `epubox-document-3`；表示磁盘数据协议，与设计版本 2.5 分开 |
| `document_id`、`source_hash` | 文档身份、源 EPUB 字节哈希 |
| `resource` | 原包内路径、media_type、源资源字节 SHA-256；不取自译文 |
| `adapter_version`、`extractor_version` | 实际解析/提取协议版本，变化必须显式处理兼容性 |
| `source_markup` | 完整源 HTML/XHTML 字符串，含 head/body、标签、属性、代码和原空白；由原字节按既有 UTF-8 约定解码，不经过换行转换/strip；仅本地保留 |
| `nodes` | 源 node_key 到稳定源地址的表，以及 kind/qname；每次从未改动 source_markup 建树后解析，不对已改写树重新找旧位置 |
| `source_slots` | text/tail/白名单属性的源位置、源值/区间及唯一去向；空值无须创建模型任务但在源字符串中保留 |
| `source_views` | §7.2 的完整 SourceTextView：文字、哈希、Unit/文档身份、source_refs、view_kind；仅用于源读取/证据，不用于中文位置映射 |
| `units` | 每个 Unit 的完整源定义：ID/类型、源槽位、源投影、结构所有者/装配区域、固定源视图/语境引用及出处、结构类检查、局部引用表；不含生效词条或依赖词表的哈希 |
| `units[].registry` | g/x/b 的源节点/子树引用、父范围、边界/移动许可、源覆盖文字与 hints；冻结子树可指回 source_markup 中的 node_key，不必重复复制大代码块 |
| `boundaries`、`derived_bindings` | 源相邻关系/硬边界、导航等确定性派生的源绑定；不是可变译文依赖图 |
| `preparation_issues` | 明确的局部不支持或未决源问题，含作用域；不能静默删除对应源范围 |

**source_markup 是源结构的权威副本；其余解析字段是经过核对的派生计划。** 不能修改其中一份后继续信任另一份。PreparationPlan 先记录每份 DocumentPlan 的完整文件哈希，BookPlan 再引用同一源清单；两阶段模型调用前核对其源位置/投影/保护关系。保存为 JSON 不会丢原始长属性，但长属性仍不得进入模型翻译请求。

源地址建议采用从文档根开始的元素子序号数组（明确不计注释/PI），并保存 qname 复核；适配器已有可靠地址方案可沿用。注释、PI、DOCTYPE、命名空间及混合内容仍由完整 source_markup 和既有安全解析器保留；装配器处理自己拥有的区域时不能丢掉这些本地保留对象。单位装配区域内存在这些节点时，源计划必须记录其模板/保护归属，不把“元素地址不计注释”误读为可以删除注释。

示意结构如下；其中具体哈希及完整节点/Unit 字段按上表生成，下段仅展示结构职责，不是可直接运行的完整计划；本版附带的 examples 专门覆盖新增术语协议：

```json
{
  "format": "epubox-document-3",
  "document_id": "d001",
  "resource": {"path": "OEBPS/chapter01.xhtml", "media_type": "application/xhtml+xml"},
  "source_markup": "<html xmlns=\"http://www.w3.org/1999/xhtml\"><head><title>Example</title></head><body><p>Hello.</p></body></html>",
  "nodes": [{"node_key": "n4", "element_path": [1, 0], "qname": "{http://www.w3.org/1999/xhtml}p"}],
  "source_slots": [{"slot_id": "s1", "node_key": "n4", "field": "text", "source_value": "Hello.", "owner_unit_id": "u001"}],
  "units": [{"unit_id": "u001", "kind": "paragraph", "source_projection": "Hello.", "slot_ids": ["s1"], "registry": {}}],
  "boundaries": [],
  "derived_bindings": [],
  "preparation_issues": []
}
```

这里保留 source_markup 不是继续让模型翻译 HTML。**正文请求构造器只读取 Unit 投影、必要 hints/constraints、语境和词条；术语请求只读取源文字视图和相关用户词条；不能把整个 DocumentPlan JSON 或 source_markup 发给模型。** 也不能只把 HTML 字符串套一层 JSON 而不保存 Unit/槽位/引用关系，再宣称已经完成解析持久化。

### 10.3 两级准备门槛与可恢复冻结

**P1：源解析提交。** 全局输入检查通过后保存 source.epub、用户词表副本及各 document JSON；每个文件原子写后重新加载，核验源槽位/视图/注册表。盘点必须完整，局部不能生成投影但源归属明确的区域仍在清单中。最后提交 `preparation.json`（format=epubox-preparation-1，state=parsed_ready），包含全部 document 哈希、原用户词表副本哈希、源/提取/翻译配置。P1 前不派发任何模型调用；P1 后可释放 DOM，从 JSON 进行术语准备。

**P2：提取与候选。** 原子提交不可变 glossary/plan.json 以及各窗口初始状态，之后才允许付费提取。其 plan_hash 与 preparation 哈希绑定；不修改 preparation 来跟随可变结果。逐项持久化成功/失败与请求费用，其他独立窗口继续。所有窗口完成或明确局部终结后，单写入器归并候选/用户词表，保存有限冲突核对结果。所有已采用候选有来源和处置记录。

**P3：冻结提交。** 无遗留在途请求、待做窗口或可自动重试项；用户未触发全局暂停；候选都已收口。对输入池哈希、用户副本、源计划和覆盖结论检查后，原子写 `glossary/freeze.json`（format=epubox-freeze-1），其中保存确切 snapshot_payload 和 rules_hash。然后按该载荷确定性编码、写出 glossary.json，计算其文件字节哈希。freeze_id 不由模型产生，快照与意图不能互相产生循环哈希：意图引用候选/源输入，快照只携 freeze_id，不包含意图文件哈希；BookPlan 最后引用两者的文件哈希。

**P4：翻译准备提交。** 使用已冻结的词表从基础 DocumentPlan 计算词条选择、语境、logical_hash、初始 CutPlan 和 UnitRecord；所有记录及额度落盘。无法规划的局部源区域保留 needs_attention，cut_plan/input_hash 可为 null，不发模型请求，也不删分母。最后提交 `bookplan.json`（format=epubox-book-3，preparation_state=ready），登记 preparation/glossary/freeze 哈希、源任务清单、派生依赖和初始 Unit 计划身份。bookplan 不保存不断变化的 UnitRecord 文件哈希，当前结果按各自 record_hash/版本核验。

**P5：翻译执行。** 只有 bookplan ready 且所有引用身份通过校验，才派发正式翻译/校对/修订/衔接调用。派发前、响应后、失败和部分片段结果都按 §9/§10.4 保存。词表和基础文档此后只读。

恢复按最后一个可信提交点进行，而不是笼统使用“无 ready=零调用”：

| 崩溃位置 | 恢复规则 |
|---|---|
| preparation 未提交 | 可从源快照续做源解析/副本；此阶段无模型调用 |
| preparation 已提交、提取中 | 复用已持久化窗口与费用，续做缺失工作；初始窗口记录派发后缺失不得当零计数 |
| 部分结果已存、候选池尚未完成 | 依据 request/item 与消费标识幂等归并；核对结果 input_hash 相同才复用，已有决策不能被重新发现的重复候选覆盖 |
| freeze 已存、glossary 缺失/写入中断 | 核验冻结意图，从同一 snapshot_payload 重写；不重新提取、不生成另一份词表 |
| glossary 已存、Unit 初始记录或 bookplan 未齐 | 使用同一快照补齐初始计划，最后提交 bookplan；不能把词表存在直接视为可翻译 |
| bookplan 已 ready | 只按 §10.5 核验并续跑翻译；不修改用户输入副本、候选池或 glossary |

freeze 提交前即便 auto_extract=false，也保存显式 disabled 的空提取计划/候选池，合并用户输入（没有则空），再走相同冻结/P4；不存在手工模式下绕过身份/发布检查的分支。准备期文件损坏可隔离时给出局部记录诊断，但不能将已付费提取当作从未执行；已封存的输入/冻结身份损坏必须显式修复或停止，不用新模型响应猜原快照。

### 10.4 结果 JSON 与单写入器

UnitRecord 至少保存：`format=epubox-unit-3、unit_id、document_id、source_hash、logical_hash、input_hash、plan_epoch、record_version、revision、cut_plan、items、candidate、accepted_revision、accepted_target_hash、local_checks、review、term_feedback、unresolved_issues、counters`。

每个 items 记录对应当前 CutPlan 的稳定 segment/item 身份、selected_term_ids/term_applicability/terms_hash/context_hash，包含阶段、状态、目标投影（未成功为 null）、目标哈希、检查状态、请求来源、失败原因和下一步动作。局部状态限定为 `pending / in_flight / candidate / local_valid / reviewed / retry_wait / needs_attention / blocked_dependency`；Unit 的 accepted 仍由 §8 汇总判定，不由模型直接填写。普通 Unit 使用一个 Segment 记录，不再维护另一套结果格式。

失败记录至少有 `scope、stage、code、message、request_id、item_id、plan_epoch、revision、retry_action`；retry_action 为 `automatic / explicit_retry / repair / dependency / none`。失败不能把原文写成 target，更不能将 target 非空等同成功。只读衔接/派生步骤的 dependency 状态写在 checks 或派生记录，不伪造翻译失败。

一个运行目录只有一个协调进程写入；网络任务把结果交回单写入器。完整 UnitRecord 同目录临时写、flush/fsync、原子替换；适用平台同步目录并测试。记录内部校验 `record_hash`（计算时排除该字段自身）；它用于发现损坏，不是抵御恶意手改的安全签名。加载仍须重新跑必要的结构/状态检查。[^R9]

对当前代次的 Segment 并发返回，协调器在最新记录上合并**当前 item**，再比较/推进 record_version；不能将含旧兄弟结果的整个对象覆盖回磁盘。持久化进度不增加目标 revision，不使正常兄弟响应过期。CutPlan 升级则将整个新计划和旧片段失效状态在同一 UnitRecord 中一次提交。

RequestManifest 的请求身份/wire_hash 派发后固定；其 attempts 数组按实际调用原子追加。每次 HTTP 派发前落盘 `attempt_id、affected_items、reservation、state`，随后才能发送；同一逻辑 request_id 的传输重试必须使用新的 attempt_id。计数恢复对 `(request_id, attempt_id)` 幂等合并，不能只按 request_id 去重而漏计真实重试。某个派发记录写好、对应提取窗口/冲突组/Unit 的预留尚未更新就崩溃时，恢复保守计入该预留，不能因此获得额外额度。反过来，只有本地预留而未确认发出的调用不宣称已计费。保留轻量计数/身份记录到运行结束，不必永久保存大响应正文。这是单机文件协议，不新增数据库或事件平台。

### 10.5 JSON 是正常续跑入口，不是可选缓存

恢复先判阶段：存在可信 freeze.json 但 bookplan 未 ready 时，只重放冻结与后置计划；只有 preparation.json 时，核验源文档/用户副本/提取计划/窗口与候选记录，续做术语准备；连 parsed_ready 都没有时，才续做无模型源解析。不得以“没有 bookplan”判断尚未付费或删除提取成果。

正式翻译恢复流程为：加载 ready 的 bookplan → 核验 source.epub、glossary.json 与 document JSON 哈希/版本 → 从 document JSON 恢复源 Unit/注册表 → 加载 UnitRecord → 按下表重新派发剩余工作。**不重新从原 EPUB 切整本书、不把最后成功序号当唯一断点，也不依赖退出前的内存 DOM。** 装配时可以重新解析 JSON 中的 source_markup；最终独立验证仍可读取源快照，这与不重新提取全部任务不矛盾。

| 已保存状态 | 恢复动作 |
|---|---|
| preparation 已就绪，窗口缺结果/可重试 | 只续提取缺项，成功窗口/计数保留；不调度正文 |
| 全部提取已终结，但候选池/核对未收口 | 复用原始提取结果，幂等合并；只续剩余有额度的核对 |
| freeze 意图存在，glossary/bookplan 写入中断 | 校验意图，重放相同快照和初始计划；不再调用模型或改词表 |
| 当前版本 accepted 且校验有效 | 复用，不重新翻译/校对；必要的过期章节衔接仍重做 |
| 合法目标已保存、校对未完成或校对请求失败 | 复用目标，从校对继续，不重译源文 |
| 一个长 Unit 部分 Segment 成功、部分失败/未做，CutPlan 不变 | 复用成功片段，只补缺失/失败片段，再形成完整目标并校对 |
| pending 或中断遗留 in_flight | 核验请求身份与额度；in_flight 为远端结果未知，剩余额度内续跑，不假定未收费 |
| retry_wait 且自动额度未耗尽 | 按退避信息恢复；不阻塞其他 ready 项 |
| 自动重试耗尽、结构/语义 requires repair | 保留 needs_attention；继续其他工作。显式选择重试失败项/导入修订后再激活，必要额度提升必须记录 |
| blocked_dependency | 依赖当前已满足则入队；否则保留等待，不消耗请求、不永久占住 worker |
| CutPlan 主动升级 | 同 Unit 旧片段全部失效；其他 Unit 不变 |
| 单个 UnitRecord 缺失/损坏 | 隔离该 Unit 和其依赖，其他有效任务继续；不能重置已用额度或猜 accepted |
| 单个 DocumentPlan 缺失/哈希不符 | 隔离该文档及其依赖，其他已核验文档可继续；不在普通 resume 中静默重提取并混用旧译文 |
| 源快照/BookPlan/glossary 快照身份不符、共享盘点或冻结规则不可信 | 运行停止 failed，不用同名文件或改后的用户词表替代快照 |

恢复必须扫描完整任务清单以发现“中间的洞”：u001 成功、u002 失败、u003 成功，不能因为最后成功是 u003 就跳过 u002，也不能从 u002 起把 u003 重新翻译。自动额度仍足够的请求失败可以自动重试；已耗尽/需修复项只有显式授权才重开。显式授权追加新的尝试周期/额度，不清空历史计数或把所有失败项无差别再跑一遍。

损坏记录的恢复属于显式修复动作：能从已校验源快照/同版本适配器重建出与 BookPlan 登记哈希完全相同的 DocumentPlan 才恢复其源计划；否则新建运行。结果记录不能无证据重建成 accepted；未知计数由可信 RequestManifest 保守重建，证据不足则要求用户明确确认后追加额度。MVP 不建设自动历史迁移器。

旧 HTML checkpoint 不自动迁移到新协议，也不删除旧成果。**保留的是“EPUB 解析→JSON 保存→从 JSON 继续翻译”的产品流程，不承诺与未读取的旧 JSON 字段完全兼容。** 实施时读取实际 parser/schema/checkpoint，能复用的 JSON 写入与加载能力继续用；需要版本变更就显式隔离。旧运行可由旧引擎完成，不能把旧 translated_html 装进新 target 后标记成功。

### 10.6 完成率、失败与依赖报告

CLI/报告在执行时显示 `execution_state=running/draining/stopped`，结束后的 outcome 才取 §12 的四种值。运行过程中出现 needs_attention 的 Unit **不等于马上停止整个运行**。

术语准备另显示：计划窗口数、成功/带拒绝候选/失败/未做窗口数、源输入覆盖、候选数、采用/延期/拒绝数、冻结状态与提取/核对费用。术语覆盖和翻译完成率使用不同分母，不能把成功提取当作已翻译。

正文至少分别显示：必需源任务数、成功接受数、有效派生数、待翻译数、待校对数、局部失败数、等待依赖数、当前阶段/在途数、累计请求和已知 token；按文档和阶段列出未决源位置及下一步动作。代码/合法保留内容另列，不作为模型失败项。完成率分子为当前已 accepted 的必需 Unit 加有效派生 Unit，分母为全部必需源任务。错误/等待/待做片段数另作非互斥诊断维度，不能相加当作总量；例如一个 Unit 可同时有失败片段和未尝试片段。

总必需数来自 BookPlan/DocumentPlan，不因失败、未知目标或派生等待而缩小。历史错误与当前未解决分开；JSON 结果存在/target 非空、尝试过、校对过、accepted 与全书 completed 是不同状态。report.json 是派生视图，损坏可重算，不能作为唯一状态来源。

---

### 10.7 不自动清理可恢复资料

构建成功不删除 source.epub、preparation、bookplan、glossary 与提取/候选/冻结记录、documents、units、checks 或仍需用于额度/来源核对的 requests 记录。运行结束后这些 JSON 仍应能支持检查、定位和重装配。用户明确要求清理时才处理无引用临时 staging 和可删诊断正文；不要新增自动清理命令作为 MVP 前置功能。不能复制上游 Skill 默认 build --cleanup 的资料生命周期。[^U1][^U8]

---

## 11. 本地装配、导航、语言与资源

### 11.1 装配次序只有一个

```text
不可变源模板
  → 应用已接受 Unit 的目标事件流，恢复原始 g/x 对象
  → 消去虚拟 b 与切片边界，不新增真实结构
  → 建立源节点到目标节点映射（仅本地 sidecar）
  → 应用允许的属性、导航和元数据目标
  → 应用登记过的语言/既有排版变更
  → 序列化并重新读取验证
```

对一个源结构只有一个所有者：模板、某个重建区域或某个原子子树；不能祖先整块替换后再用旧 XPath 替换子孙。复制 x 时不带属于外层 Unit 的根 tail，内部 tail 按子树保留。

可译属性在恢复子树**之后**应用，避免 alt 被旧图片副本覆盖。冻结子树对比不是盲目比较整个旧哈希：先登记具体属性白名单变更，再核对其余数据/结构；不是把所有属性一概忽略。

模型文本用 XML API 作为文本写入，不重新解析为 HTML。保留命名空间的含义及依赖前缀的值，不因序列化自动重命名而破坏 epub:type 等值的解释。未修改的二进制资源逐字节复制；不重新压图、不删字体、不改资源路径。

### 11.2 独立最终检查

从源快照单独枚举文本归属与固定结构基线（这是最终独立验证，不是续跑时重新规划所有翻译任务）；从已验证目标事件流计算应有的许可变化；再读取实际输出比较。不能只相信装配器自报“已恢复了 N 个对象”，也不能把所有目标文字掩掉而漏检装配器吞字。

必须核对：每个源槽位有唯一去向、每个 Unit 被消费一次、目标文字等于其当前已接受译文、固定容器/列表/表格结构与顺序、g/x 身份与父关系、冻结内容、实际白名单属性值及内部引用。

输出树上的源对应关系可用本地 sidecar 辅助定位，但比较依据仍是源和目标实际内容/结构，不把 sidecar 声明本身当证明。验证应通过故意改 href、删节点、删中文字、重复 tail 等变异样本证明能够发现装配错误。

**资源诊断借鉴而不降级：**参考上游按源—目标图像引用计数比较、聚合所有错误的办法，但 epubox 从真实 XML 节点/本地注册表导出带 document_id、node_key、Unit 的资源清单，不对 Markdown 做正则恢复。计数比较只能是额外诊断，仍须核对源对象身份、父关系和实际属性；两张相同 src 图片互换位置，单纯 Counter 可能看不出来。原文中转义的 `<img>` 教程和代码样例不是实际资源节点，不能误报成丢图。[^U8]

最终检查一次收集可独立判断的多个局部问题并给出定位，不止报第一个错；但源快照损坏等全局不可信情况仍立即停止，不为凑齐错误列表继续误处理。

### 11.3 ID、URI 与导航

**ID 唯一性按资源文档判断，不是全书字符串全局唯一。** 不同章节都含 `id="section1"` 并不因此冲突。链接按源资源基址解析到规范化资源路径和 fragment；核验目标存在、引用不变，不去访问外部网址证明其在线可用。

支持的本地路径规范化、百分号编码、相对路径和同文件片段必须有测试。无法解析的特殊基址在预检报告，不到最后用模糊字符串替换。

导航标签和正文标题只有在目标绑定明确、源纯文本相同、目标无不适合导航的原子对象时，才复用标题的**文字投影**，不复制标题 DOM。此标签仍有自己的源槽位和 derived 状态，绑定标题的 accepted revision；标题修订后自动使派生值过期。源标签不同或复用不可靠则独立翻译/校对。NCX、head/title 和 OPF 纯文本字段使用同一规则，不把格式标记写入纯文本字段。

MVP 不实现跨书翻译记忆库。复用只发生在本次书的明确绑定，不能按相同词语给所有导航做全局替换。

### 11.4 语言与元数据的有限变更

目标主语言为 `zh-Hans`。更新对应 `dc:language` 及已翻译文档/元素的 lang/xml:lang；已明确保留的外语原文不被误声明为中文。原来继承英文语言的保留元素必要时显式维持其源语言；代码不因英语关键字自动标为自然语言英语。

没有真实元素承载的个别混合语言文字，不为标语言新增包裹节点；在报告中保留这种粒度限制。程序不能同时承诺“一个节点都不新增”与“任意混合句每个词都有独立语言属性”。

EPUB 3 更新包级 `dcterms:modified`，使用 UTC 格式，保持唯一的包级修改时间；EPUB 2 不硬塞 EPUB 3 元数据。原出版日期和作者等保持。[^R1] 需要更改的值进入 ChangeSet；其余元数据不由模型重写。

原出版物标识符保持，不在 MVP 重发 ISBN/标识符；源文件不覆盖、输出文件另存，字体混淆依赖保持。对可能依赖书名的 file-as 等派生元数据，不让陈旧值暗称已更新：按适配器确定性更新可处理项，其余保留并报告，不推导新的事实。

### 11.5 字体和排版

默认保留原字体与 CSS，关闭剪枝。仓库若已有通过真实样本验证的中文显示策略，可以作为**明确记录且可关闭的输出变更**复用；本次不假定它已经验证过。

不使用全局 `* { font-family: ... !important }` 覆盖代码、图标和公式，不内置或分发字体文件。排版策略只影响输出，不改翻译输入；策略变化后重新装配、做结构/资源和阅读验证，不必重译源义不变的文本。若策略改变了移动许可或内容含义，则不能按纯排版变化复用本次运行，应重新准备相关翻译计划。

---

## 12. 正式输出与故障恢复

### 12.1 唯一完成条件

只有以下全部为真才进入发布：输入/范围已通过；词表与 ready 计划身份一致、冻结关系有效（自动提取 closed_with_gaps/disabled/not_required 的已披露状态不自动阻断）；所有必需 Unit 当前版本 accepted，或由当前 accepted 标题可靠派生并通过文字/属性本地检查；没有未解决的阻断项；所有必需章节衔接检查绑定当前版本；最终结构和资源检查通过；最终 EPUBCheck 已执行并无 ERROR/FATAL。

WARN 和 minor 必须记录，但不自动等同于错误。真实阅读器抽查是**发布新引擎版本的验收项**；运行报告只报告本次实际做过的检查，不能让每本书必须有人手动确认才能获得机器完成状态，也不能每本自动写“阅读器已验证”。[^R4]

Run 的**结束结果**仅为 `completed / paused / needs_attention / failed`；正在执行时使用 §10.6 的 execution_state，不因一个 Unit 失败就提前结束。判定顺序：不可继续的全局输入/存储/内部/发布错误为 failed；运行级预算/取消/共享服务问题为 paused；其余可执行独立工作均已处理、本轮可自动重试也已终结后，仍有局部问题或其依赖则为 needs_attention；全部门槛通过才为 completed。局部问题即使与全局暂停并存，也必须保留在报告中。

**“没有输出正式 EPUB”不等于“失败后的章节没有继续翻译”。** 未决阶段的 JSON 已保存全部进展；补齐失败项并通过依赖检查后，直接使用已有有效结果装配。非 completed 都不能返回代表成功的退出码；实际退出码与现有 CLI 兼容映射。

建议 RunResult 字段：`status、output_path、output_sha256、glossary_freeze_id、extraction_status、extraction_warnings、structural_check、semantic_review、coherence_check、epubcheck、reader_check、unresolved_issues`。reader_check 未执行为 `not_run`。非 completed 的 output_path 为 null；工作目录路径单独提供。

### 12.2 输出提交步骤

1. 使用源与当前 accepted 结果重新生成本次 staging EPUB；按源版本保留容器、manifest、spine、mimetype 等规则。MVP 不照搬 HTML/EPUB mtime 快取，每次正式重装配从已校验 JSON 构建，不需要重译。
2. 关闭写入器，重开临时包；核验章节、资源、链接及规范，计算本次产物字节哈希。
3. 原子保存 `publish.json` 提交意图，包含 run/计划指纹、完整结果版本向量、output_policy_hash、assembler/validator/工具版本、目标路径、产物哈希和验证记录；确定性编码计算 build_fingerprint。
4. 在目标同一文件系统使用原子替换提交产物，再把 publish.json 标记完成并派生报告。
5. 返回本次真实产物；不通过路径存在就判断成功。输入路径、源快照路径和其符号/硬链接别名都不能作为输出。

默认不覆盖已存在的正式输出；用户明确允许覆盖时也必须先验证临时产物。运行间针对同一输出目标使用锁或独占提交策略，不能两个进程同时互相覆盖。

### 12.3 发布中断不能伪成功

若文件替换完成但状态尚未落盘，恢复时核对已保存的提交意图、目标哈希、当前版本向量和 build_fingerprint，匹配才补记完成；不重新翻译。如果目标仍是旧文件、哈希不符或缺少可信意图，就不能把路径存在当作本次成功。

若验证、磁盘写入或提交失败，保留已接受译文及诊断，输出失败/未完成，不输出损坏 EPUB，也不删除原输入或之前正确产物。这里是文件级提交协议，不是引入数据库事务平台。

---

### 12.4 构建成功只来自本次验证，不来自日志或文件存在

上游的构建脚本包含各格式结果报告，但本次读取的 main 分支中，`generate_formats()` 返回 False 的正常路径没有相应的非零退出分支。因此其 “Build Complete” 文案不能充当本项目完成契约。[^U8] epubox 沿 builder→orchestrator→CLI 贯通有类型失败；本地检测到失败必须传播到最终 status/退出码，不因为目标旧文件存在而补记成功。

不移植 Markdown 标题重推断、新 slug 目录生成、默认仿宋模板、DOCX/PDF 多格式构建或 Calibre 发布包装器。导航仍绑定原资源和源锚点，字体仍保留既有方案，语义/结构/规范的三个门槛仍分开。

---

## 13. 对现有代码的最小改造路线

所有路径仍是待实际仓库核实的改造入口。保持 CLI 和可用 provider 接口，不为了重构统一搬目录。

| 原文件/职责 | MVP 修改 |
|---|---|
| `core/markup.py` | 统一解析/序列化、namespace 和混合空白处理；无模型 HTML 修复 |
| `epub/parser.py` | 保留包读取与现有 JSON 流程；强制导出完整 DocumentPlan JSON，提供 JSON 加载入口；结果状态交 store |
| `item/chunker.py` | 拆语义提取与传输规划；HTML 长度不再是请求单位 |
| `item/precode.py` | 复用有效保护规则，生成单次注册表；不在每次调用重识别 |
| `schemas/chunk.py` | 增加/迁移 Unit、CutPlan、UnitRecord；不借旧 translated_html 字段装新协议 |
| `agents/translator.py` | 保留模型接入，改输入输出投影和本地 RequestManifest |
| `agents/verifier.py` | 分开协议、结构、内容异常、源译审查；保留有效检测器 |
| `agents/workflow.py` | 实现固定校对/修订/衔接；局部错误转带 scope 的结果；依赖未齐挂起，不使后续工作 fail-fast |
| `agents/fallback_runtime.py` | 提取/核对/翻译共用调用调度与台账；phase 门槛和局部失败隔离 |
| `services/glossary.py` | 词表导入/校验、候选归并/证据/频次、scope/优先级、快照与按需选词；复用 store |
| `agents/term_extractor.py`（必要时新增） | 固定术语提取/有限冲突核对协议，调用已有模型适配器；不自主写文件 |
| `epub/replacer.py` | 从源模板和 accepted 事件流本地装配；属性后置更新 |
| `epub/builder.py` | 保留打包；关闭默认剪枝；验证、提交、失败返回统一 |
| `orchestrator.py` | parsed_ready→术语冻结→translation ready 两级门槛；恢复、非 fail-fast 与发布 |

确有需要再新增 `item/extractor.py、item/inline.py、item/planner.py、item/context.py、services/store.py`；CSS 有限扫描可放在 markup 附近的独立小模块。先拆职责再定文件，不建设庞大的插件/适配器框架。`plan_resume` 可先放在 store 附近的小模块；SourceContext/TermSelector 保持纯函数，不把 translate-book 脚本全部复制进 engine。函数级移植表见 §17.3。

### 13.1 开发闭环

**A：现有行为与最小样本。** 读真实调用关系，运行已有测试，保留发生过问题的源/目标小样本；只核对影响本次接口的现状，不要求先完结 68 项审计。

**B：无模型且跨进程往返。** 使用明确的测试配置关闭自动提取并使用空词表/固定 fixture；源提取 → 文档/初始状态 JSON 保存 → 退出并清空内存 → 仅加载 JSON 源计划 → 原投影原样返回 → 装配 → 打包重开。identity 模式不应用目标语言/书名等翻译收尾，按源结构语义核验，不要求 ZIP 时间戳或属性排列字节一致。

**C：术语准备与完整单元主线。** 先贯通源 JSON→提取→候选→用户优先合并→冻结，验证无 bookplan 时提取也能恢复、局部失败继续与冻结重放；再处理普通段落和常见内联先完成相关术语/短源语境、真实翻译、校对、保存、纯恢复计划及输出；注入中间 item/Unit 失败，证明后续同章和跨章内容继续、成功结果立即落盘、续跑只补缺项；长单元暂未实现时只能用于阶段样本，不宣称 MVP 已完成。

**D：长单元与边界。** 实现 CutPlan、虚拟边界、局部限额、原子升级、衔接复查和人工修订导入。验证 s2 失败不阻止 s3，普通续跑不使成功片段失效，并发片段结果互不覆盖。

**E：整书验收。** 覆盖导航、属性、语言、资源、发布恢复和选定阅读器；通过 §14 后新引擎才切默认。旧引擎保留用于旧运行恢复，不共享接受状态。

各阶段都保留可运行入口和新增回归用例；不先删除旧链路再开始设计，也不把 mock 协议测试当成真实翻译质量验收。

---

## 14. MVP 验收清单

下列是**实现后必须执行的测试**，不是本次文档审查的测试成绩。不得仅因“程序能输出一本 EPUB”就判定完成。

| 编号 | 输入/故障 | 必须观察到的结果 |
|---|---|---|
| T01 | body 裸文本、多个 tail、嵌套块、空白 | 源槽位完整无重；身份往返不吞字、不重复 |
| T02 | 两个同名链接/强调在中文中换序、嵌套组移动 | 本地身份/父关系/原属性正确；不依赖出现次序 |
| T03 | 缺标记、重复、未知、交叉、换父、虚拟 b 越界 | 无效候选拒绝，其他有效 Unit 不丢 |
| T04 | 字面定界符、反斜杠、尖括号、非法 XML 字符/孤立代理字符、JSON 重复键/截断 | 正确转义或拒绝；不从坏 JSON 取半截结果 |
| T05 | code/公式/图片带外部 tail 与 alt，translate 继承 | 保护内容不改、tail 一次、可译属性后置且范围正确 |
| T06 | br/页码硬边界、脚注多处引用 | 真实边界保留；语义绑定被单独审查 |
| T07 | first-child 前先有 em、未知 CSS、无关长 data 属性 | 正确区分选择器语义；保守锁序；真正无关属性不改变任务 |
| T08 | 跨多个 Segment 的长链接、嵌套 g/b | 最终原范围一次；虚拟事件消去；不复制 ID |
| T09 | 翻译可装下但校对超限、重规划时中断 | 校对预算预留；新计划/旧结果不混用；其他 Unit 保留 |
| T10 | 快慢反转、否定/条件丢失、流畅错译、英文字照抄 | 本地与模型的真实检出结果分别记录；人工评价不过度依赖结构 |
| T11 | 链接包错中文、强调重点错、脚注换论断 | 结构可以合法，但绑定审查/人工回归能够暴露问题 |
| T12 | 修订产生新错误、no_change 带 major/uncertain | 不错误晋级；新 target 重新完整检查 |
| T13 | 迟到 review、章节检查后 Unit 改动、连续修订 | 旧版本拒绝；章节结论过期；循环有界 |
| T14 | 空词表、坏用户词表、memory 多义、数字等价写法 | 空表合法；坏配置报错；多义/格式变化不误锁 |
| T15 | 缩批/乱序返回、429、超时、SDK 重试 | 归属不变；统一计数；不承诺未知费用为零 |
| T16 | 文件半写、同名不同书、新配置续跑、锁冲突 | 不能串用/并发覆盖；有效进度可恢复 |
| T17 | 装配后删中文字、复制 tail、改 href、漏 alt | 最终独立比较拒绝，不只验证 XML 合法 |
| T18 | 不同文档同名 ID、相对链接、非 spine 尾注 | 正确按文档解析；不误报全书 ID 冲突、不漏尾注 |
| T19 | 导航复用带格式标题、标题后续修订 | 导航只取有效文字；派生版本更新，不复制标签 |
| T20 | EPUB 2/3、字体混淆、签名/音频同步等支持边界 | 不误升级、不误判字体为正文 DRM；未支持关系预检指出 |
| T21 | 目标旧文件存在、写盘失败、替换后进程崩溃 | 不伪成功、不伤原书；提交意图可恢复确认 |
| T22 | 实书模型链路与目标阅读器 | 正文/代码/表格/脚注/导航可读；质量与成本有真实记录 |
| T23 | 同章中间 Unit 翻译失败，另一个 Unit 校对失败，并发分别设 1 和 2 | 同章后续与下一章继续并逐项保存；无 fail-fast，无静默缩分母 |
| T24 | s1 成功、s2 失败、s3 成功；进程退出后按原 CutPlan 续跑 | s1/s3 的目标复用，仅补 s2；Unit 齐备后才合并/接受 |
| T25 | 完整 Batch 一项缺失/无效，另一个 Batch 整体 JSON 截断 | 前者保存独立有效项；后者整批拒绝但其他批次继续，不抢救半截 JSON |
| T26 | 每份 HTML 解析为 JSON 后退出，重新加载并 identity/装配 | source_markup、Unit、槽位、g/x/b 与属性定位齐全；不用原内存 DOM，不把长属性送入模型 |
| T27 | 最后一项成功但中间失败；目标已生成而校对失败 | 扫描完整清单补洞；成功译文不重翻；校对失败仅续校对 |
| T28 | 一个 Unit 额度耗尽、章衔接额度耗尽，再分别触发运行总额度/鉴权/写盘失败 | 局部限额不停止独立任务；运行级限制才全局暂停/失败，错误计数不清零 |
| T29 | 失败标题使导航派生等待，失败段使衔接等待 | 等待不占 worker/额度；后续正文继续；补齐后只执行相应依赖检查 |
| T30 | parsed_ready 前/术语准备中/translation ready 后中断与 JSON 损坏 | 首个门槛前零模型调用；术语成果/费用可恢复；ready 后缺记录不归零，局部可隔离则继续 |
| T31 | 同一目标代次两个 Segment 并发返回，一项先写错误或进度 | record_version 与 revision 分离；合法兄弟结果均保留，旧代次迟到结果仍拒绝 |
| T32 | 首个局部失败、所有 ready 任务排尽、仍有人工待处理依赖 | 不提前退出，也不永久等待；返回 needs_attention，output_path=null，全部进度可恢复 |
| T33 | plan/status 预览两次，含坏记录和旧词表格式 | 不改任何源/状态/词表字节，不调用模型；action/reasons 可复现 |
| T34 | source 正常、输出被手改或外部未知译文存在 | 不 record_only 放行；显式导入后作为候选重新校验 |
| T35 | 邻接 chars=0/负数、非连续文档、长保护标记 | 0 前后皆空、负数预检报错；不串邻居、不截断协议或改正文 |
| T36 | C++/.NET、cat/category、alias、exact/casefold、跨范围 memory | 匹配与scope正确，别名去重；多义不受全书表面词唯一规则误拒绝 |
| T37 | 同 item 命中超过 50 个词条/提示装不下 | 不截掉约束；缩批/从源重规划或局部待处理，其他任务继续 |
| T38 | 词条带分隔符、note/mode/alias 改动，字典/有序数组换序 | 结构化摘要无连接歧义；提示变化影响摘要；有序内容不被排序掩盖 |
| T39 | 派发后外部词表改变、重复响应/无改动 review 重回 | 运行使用冻结快照，记录真实已发送子集；合并幂等而 HTTP 计数不漏 |
| T40 | 同src图片、转义img代码样例、多个最终资源错误 | 对象身份和计数并查，文字样例不算资源；定位多个真实差异 |
| T41 | 产物较新但输出策略已变化、构建返回False、已存在旧书 | 不以mtime/存在/日志成功；新staging重建，失败贯通status/退出码 |
| T42 | 构建成功后再次只读检查/从JSON重装配、尝试旧-1/-2数据普通续跑 | 核心JSON仍存在；旧协议不悄悄升级混用，不复刻cleanup |
| T43 | 无用户词表、auto_extract 默认开启 | 从所有应扫描源视图提取候选并落盘；不是直接空表翻译，也不重新发送 HTML |
| T44 | 术语仅出现在后半书、表格/尾注、格式跨词；代码/图内英语 | 默认源视图覆盖应扫内容，硬保护区不作为术语发现正文；术语覆盖不冒称召回率 |
| T45 | 虚构 quote、借其他窗口 view_id、只出现在 hints 的词、未知 scope ID | 拒绝无效候选/范围，其他合法候选保存；证据存在不冒称译法正确 |
| T46 | 用户 preferred/required 与自动建议同词冲突、部分范围重叠 | 用户规则优先，原文件不改；自动词条不通过缩 scope 绕过用户，不升级 required |
| T47 | memory 多义、同词异译在不同文档、同文档冲突 | 范围分开保存；有限核对仍不明就 defer，不全书 source 唯一、不按频次强选 |
| T48 | 别名重叠、重复请求/反馈、同视图作为邻接出现多次 | 匹配/词频不重复计；同候选证据幂等合并，真实 HTTP 重试仍计费计数 |
| T49 | preparation 已就绪、提取已收费，bookplan 尚未存在时崩溃 | 恢复已提取成果与 attempts，不认为 ready 前永远零调用、不重置计数 |
| T50 | 中间提取窗口失败耗尽、后续成功、部分坏候选 | 后续继续；成功部分保存；所有窗口终结后可 closed_with_gaps 冻结并翻译，报告缺口 |
| T51 | 提取中账户失败/用户取消/运行硬预算触顶，仍有 pending | 全局 paused，不把剩余窗口标空/失败提前冻结；续跑只补缺项 |
| T52 | freeze 写完后 glossary 未写、glossary 写完后 bookplan 未写；迟到结果 | 同一快照重放；无重复模型提取、无循环哈希、迟到候选不改已冻结规则 |
| T53 | DocumentPlan 完整源 JSON、术语冻结后词条绑定 | document 字节不变；有效词条/哈希存在 UnitRecord/CutPlan；只有 P4 后翻译 |
| T54 | 翻译中 review 提出新术语、无建议、重复 term_suggestions | review-2 兼容可选字段；反馈局部保存/幂等，不修改 candidates/glossary，不阻塞独立任务 |
| T55 | 冻结词表存在已确认错义，或重大问题只写成建议 | 已识别重大错误不放行；正常候选缺口不等于错义；必要时新配置运行而非偷改规则 |
| T56 | 提取/核对额度耗尽，缩批、重试和恢复 | 窗口/冲突组限额与全局额度分开，计数不中断重置；高成本阶段可提前暂停 |
| T57 | frozen snapshot 中只改 evidence/frequency，另改 note/mode/target | 完整文件哈希检测任何变动；rules/terms 哈希仅随实际提示/作用规则变化；不能忽略 note |
| T58 | 用户明确 auto_extract=false、无可提取内容、成功空候选、全部局部提取失败 | disabled/not_required/closed 空候选/closed_with_gaps 分开；同一最终质量标准 |
| T59 | context 的词条属于另一文档/义项、同 item 命中 >50 词 | 上下文规则不成为当前目标强约束；无静默截断；真实子集/角色/哈希派发前保存 |
| T60 | 真实技术书完整术语准备→翻译→重建及恢复 | 记录出处有效性、误用/一致性、人工语言抽查、总成本；不以合成 JSON 测试替代 |


### 14.1 最小真实验收方式

选少量实际会翻译且有权使用的书，必须覆盖普通正文、技术代码、表格/脚注和复杂内联；若承诺 EPUB 2/3，两种包适配都须有样本。样本数量不替代内容覆盖。

针对这些样本保留一组固定源句，采用相同模型配置比较旧链路与新链路；人评分别检查忠实度、自然度、术语与格式归属，重点包含真实失败案例和结构合法的错译反例。术语功能另比较固定源样本中的候选出处有效性、采用词条的实际适用性、词表一致性和提取费用；成功扫描比例不能代替术语召回率。没有数据不承诺“成功率 99%”或“成本下降一半”。

结构、源归属、资源引用、未决状态和发布伪成功这些确定性错误的验收目标为 0；语义质量通过真实源译抽查评估，不声称全书绝对零错。统计自动完成率和人工待处理比例，发现大面积锁序/拒绝就不能宣布产品可用。

---

## 15. 给开发 Agent 的实施约束

```text
本次基准：epubox_mvp_design_v2_5.md。
旧版 68 项分析仅为历史线索；不将其全文纳入本次验收需求。

先读实际仓库规则、入口、配置、锁文件和相关测试。
只在有源码/测试证据时声称现存 bug；保留已有效的实现。
按照 A—E 完成整条 MVP 翻译链路，不另建平台或全部换框架。

必须：完整语义 Unit、受限内联协议、自动术语提取/候选保存/冻结、源译校对、长单元重组、
本地结构重建、保存恢复、导航/语言、最终验证与真实输出。
沿用借鉴：只读恢复计划和原因、固定源邻接语境、范围化别名术语匹配、
派发时依赖快照、保存前验证、幂等记录、构建前复核。
禁止复制：Markdown/Calibre中转、认领未知输出、手改只更新哈希、批后变词表、
文件编号猜邻居、50词条静默截断、mtime直接放行、成功自动删除JSON。
复制MIT代码前锁定可核验提交，保留许可和来源；当前文档未锁定上游HEAD。

新增明确要求：所有 HTML/XHTML 基础解析计划先落盘为章节 JSON；
parsed_ready 后允许术语调用；glossary 冻结、bookplan ready 后才翻正文；
DocumentPlan 不绑定生效词条；候选池不回写已经冻结的词表；
提取/合并/预算/恢复都按阶段落盘，不以 bookplan 缺失推断零模型调用；
译文/片段/校对/失败逐项写入结果 JSON；从 JSON 跨进程恢复。
单项/单元/章节局部失败必须让出调度继续后续独立内容；
只有依赖步骤与最终 EPUB 发布等待补齐，不以“有失败”停止全书。
已成功 Segment 不因普通失败续跑作废；record_version 不等于目标 revision。

不允许：模型原 HTML 直接回填、按源偏移猜中文格式、
字符串全局替换校对、静默保留英文当完成、跳过失败项后改分母、
重规划重置重试额度、过期 review 覆盖、以输出路径存在冒充本次成功；
不得只存内存 DOM、只存译文 JSON、等整书完成才存档，
不得以最后成功序号作为唯一断点、因单项异常取消其他正常任务。

不要新增快速/草稿模式、动态词表/实体属性共识平台、动态依赖传播、CSS 引擎、
多模型路由、数据库迁移、OCR、DRM、字体剪枝或 Web 后台。

每一步报告真正执行的命令、结果、样本范围和未覆盖项。
设计检查/mock 测试不等于模型翻译或实书验收。
```

---

## 16. 历史问题承接与本次审查边界

### 16.1 原 68 项的范围去向

此表只核对需求是否被承接，不重新判定项目缺陷，也不恢复旧优先级清单。

| 原编号 | 本版承接 |
|---|---|
| 01 | §0：明确不做多编码扩展 |
| 02—07 | §1、§3、§11：输入范围、源表示、提取与正常内容 |
| 08—18 | §3—§5、§9：保护、格式引用、属性、数据边界 |
| 19—27 | §6—§9：预算、切片、上下文、并发和衔接 |
| 28—36 | §7—§8：术语、语义、自然度与质量边界 |
| 37—47 | §4、§8—§9：响应、结构、校对、修订和回滚 |
| 48—57 | §3—§6、§11：身份、改序、范围、回填和最终核对 |
| 58 | §10：旧状态隔离，不强制迁移 |
| 59—60 | §3、§8、§11：覆盖、装配内容、残留与语义区别 |
| 61—64 | §11—§12：字体、资源、产物、阅读检查 |
| 65—67 | §9—§10、§12：恢复、调度、日志与失败返回 |
| 68 | §14：真实项目回归与阅读样本验收 |

### 16.2 本版核查与证据边界

本次完整阅读 v2.4，按用户确认的术语准备逻辑修订完整正文，交叉检查准备门槛、JSON 所有权、候选/快照、提示子集、计数恢复、错误作用域和发布条件。静态检查结果与例子见随包 checks；不将旧版本探针数字继承为本次成绩。

没有修改或运行 epubox 源码，没有重新执行上游测试/真实模型/EPUBCheck/阅读器，也没有重新锁定上游提交。T01—T60 是实际项目待执行的验收要求，文档检查不能证明实现无错误或全书语义正确。参考来源沿用历史核对记录；此次不声称重新验证远端最新代码。当前主文替代旧版，其余附属记录不构成并行需求。

---

## 17. translate-book 的实际流程与移植裁决

### 17.1 历史研究记录：上游运行边界

上游 Skill 驱动的流程可概括为：

```text
宿主 Agent 读取 SKILL.md
  → convert.py：Calibre → HTMLZ/HTML → Pandoc Markdown
  → 清理转换痕迹；按 Markdown 结构块形成约 6000 字符的 chunk 文件
  → manifest.json：源文件、chunk 顺序与源哈希
  → glossary.json：用户词表/抽样形成的词表，扫描频次
  → run_state.plan：哪些要翻译、哪些只登记、哪些保留
  → 为 chunk 选择词条 + 前后原文摘录
  → 宿主启动独立上下文的子 Agent，按波次翻译并写 output_chunk 与 meta
  → 先按本批词表 record 输出，再 prepare/apply meta，更新后续批的词表
  → 核查缺失输出，合并 Markdown、检查图片、生成 HTML 和新目录
  → Calibre 输出多种格式；Skill 的构建命令带 cleanup
```

这不是一套由脚本内完整实现的 LLM 调度、语义校对、原 DOM 回填系统。readme/Skill 的“保持格式”提示不等于程序对每个 HTML 节点存在保真契约；manifest/source/output 文件检查也不等于完整源译审校。[^U1][^U2][^U6][^U8]

### 17.2 为什么不把 epubox 改成它的转换路线

上游中间表示是 Markdown，输出模板/目录可以重建新的阅读结构；epubox 的目标却是**保留原始 EPUB 资源、章节、锚点和嵌套语义，允许受约束的段内改序**。因此可以借鉴任务数据与依赖管理，不能把 Markdown 转换器当作保真解析器。

主架构继续为 `source EPUB → 原HTML+解析JSON → 受限文本翻译 → 本地装配 → 原包结构内的目标EPUB`。JSON 主流程、失败后继续、成功切片恢复、结构与语义分别检查，不因上游已有工具而被删除。

### 17.3 函数级移植表

| 上游位置 | 裁决 | 移植到 epubox 的边界 |
|---|---|---|
| `manifest.file_hash` | 小函数可复用 | 若现有源码无可靠流式 SHA-256，再适配到 store/reader；不复制整份 manifest 模型 |
| `glossary._canonical_json` | 可复用核心思路，补严格规则 | 字典稳定编码可用；数组按业务含义处理、拒绝非法数值；见 §2.5 |
| Skill 的种子词条发现 | 转成 TermExtractor 正式能力 | 默认逐章源视图覆盖；不照搬五点抽样为完整扫描，不启动宿主自主 Agent |
| `glossary.load_glossary/count_frequencies/select_terms_for_chunk` | 适配读写、词频、选词逻辑 | 输入为源 JSON；范围/用户优先/无静默截断；证据与频次不代表正确率 |
| `glossary._count_in_text` | 可改造移植 | 英语边界/正则转义/aliases；增加 scope、match_policy、参数与反例；见 §7 |
| `chunk_context.get_neighbor_context/_read_excerpt` | 保留接口思想，重写数据来源 | 从 JSON 阅读关系取邻接，修复零预算路径；不移植文件编号推邻居 |
| `run_state.plan` | 借鉴 action/reasons 分层，改写规则 | §9.6 的纯计划；禁止未知/手改输出 record_only；不引入动态词表失效平台 |
| `run_state.build_chunk_record` | 借鉴结果来源字段 | 使用派发前 RequestManifest 快照，不在结果登记时重算当前词表 |
| `save_run_state/save_glossary` | 复用临时文件替换模式，不照搬完整函数 | 用现有单写入器，加 fsync、版本比较和恢复计数；不把原子rename当全部持久性 |
| `meta_content_hash` / consumed-hash 记录 | 适配幂等候选反馈 | 用于 request/item/候选证据去重；准备池与冻结后 Unit feedback 分离 |
| `merge_meta` 的校验后统一提交 | 适配候选验证、用户优先归并、有限核对 | 仅冻结前执行；不移植性别共识、批后改表或全书 source 唯一约束 |
| `merge_and_build._validate_chunk_images` | 借鉴差异聚合和多重集检查 | 对实际 XML 资源清单进行节点身份+计数检查；不把 Markdown 正则移入装配器 |
| `manifest.validate_for_merge` | 借鉴构建前重复核验，不复制宽松分支 | ready 计划必须可信；校验当前 accepted、目标哈希、全部依赖，不允许缺 manifest 的回退 |
| `convert.parse_structural_blocks` | 只借鉴“先语义块后批次” | EPUB 从本地 DOM 提取 Unit；不二次解析 Markdown |
| `convert._force_split_block` / 数字清理 | 不移植 | 字符阈值非硬预算；无换行长段可能仍超限，数字行启发式会误伤正常正文 |
| Skill 的独立上下文、成功再登记 | 适配为受控请求+逐项落盘 | 不启动可自由写文件的子 Agent、不等整个波次、不让模型生成节点身份 |
| `convert / merge_and_build / calibre_html_publish` 完整路线 | 不移植 | 不新增 HTMLZ、DOCX/PDF、模板重出版、自动目录重建或 cleanup |

**整文件原样移植项为 0；可以直接评估复用的是很小的无业务副作用函数，其他均为适配。** 这不是否定该项目，而是两者输入保真目标、状态标准和运行方式不同。[^U2][^U3][^U4][^U5][^U6][^U7][^U8]

### 17.4 已发现的关键边界，不带入 MVP

1. `chunk_context._read_excerpt` 在 tail=True、chars=0 时取全文；修为显式空摘录。
2. `select_terms_for_chunk` 本地命中达到 max_terms 时切片截断；MVP 不丢用户约束。
3. `term_hash` 字段连接编码存在歧义；改为有类型的规范 JSON。
4. `_force_split_block` 对单行超长段没有继续细分硬保证；使用源事件与双阶段 token 预算。
5. 数字行单调序列不证明是页码；不删除源正文、章节号或代码数字。
6. `plan` 对未登记输出/仅输出改动有 record 分支；MVP 不认领成已审校。
7. `_check_evidence` 只验证字符串类型与长度，不验证引文真在源中；不能把元数据合法当事实正确。
8. manifest 合并门禁没有完整语义与最终目标来源检查；现有 epubox 门禁不能降级。
9. 批后动态词表可以使早期结果使用不同词表；本轮必须固定，不依赖下一次运行补救一致性。
10. 产物 mtime 和完成日志不证明配置/校对/模板一致；重新装配和正式提交凭证为准。

以上是 v2.4 历史源码研究记录，不是本次新运行的检查；旧版分支探针不随本次作为验证成绩提交。未在上游代码里直接见到完整五项独立校对或 EPUBCheck 链路，不能从 README 的“验证”宣传推定已经具备；也不据此宣称所有用户产物均有错误。

### 17.5 实施顺序与许可

在 §13 A—E 中依次落地：先保住 JSON 往返；再完成模型术语提取、候选验证保存和冻结，将源上下文/按需选词接入请求准备；将恢复规则写成可预览 action/reasons；最后强化输出来源/资源诊断和发布验证。不能先移植 Markdown 流程，再试图补回已丢失的原 EPUB 结构。

仓库许可证为 MIT，含 Copyright (c) 2025 Rainman。复制/改造受其覆盖的代码时保留对应版权与许可文字；在 THIRD_PARTY_NOTICES 记录仓库、实际提交、源文件/函数和改动。依赖与原书资源各自许可不由 MIT 自动覆盖。本版提供设计、审查记录、合成 JSON 与文档检查脚本，不包含字体、书籍资源或实际移植后的 epubox 源码。[^U9]

---

## 参考依据

这些来源用于核对技术约束，不证明 epubox 已实现或已存在对应问题。接口、限制、默认值和 MVP 取舍是本方案的工程设计。R1—R10 是沿用基线的规范依据；历史版本于 2026-09-28 重点审阅 U0—U9 所列上游源码；2026-09-29 本版更新按已确认逻辑整合，不声称重新核查了所有规范全文或其最新修订。

[^R1]: W3C EPUB 3.3，资源与容器、包级修改时间、字体混淆、签名与媒体同步。`https://www.w3.org/TR/epub-33/`
[^R2]: lxml 官方教程，混合内容、text/tail 和序列化。`https://lxml.de/tutorial.html`
[^R3]: W3C Selectors Level 4，first-child、位置伪类和兄弟关系。`https://www.w3.org/TR/selectors-4/`
[^R4]: W3C EPUBCheck，规范一致性检查范围。`https://www.w3.org/publishing/epubcheck/`
[^R5]: WHATWG HTML，translate 的模式继承和可译内容。`https://html.spec.whatwg.org/multipage/dom.html#the-translate-attribute`
[^R6]: OASIS XLIFF 2.1，原始数据、内联引用和移动/复制/删除约束。`https://docs.oasis-open.org/xliff/xliff-core/v2.1/os/xliff-core-v2.1-os.html`
[^R7]: Python json 文档，重复键、非标准数值和解析行为；不据此指定项目 Python 版本。`https://docs.python.org/3/library/json.html`
[^R8]: Unicode UAX #29，文本边界和字符簇；只是切分基础，不保证专业句子识别无误。`https://www.unicode.org/reports/tr29/`
[^R9]: Python os 文档，replace/fsync 与文件系统边界；本项目仍需崩溃测试。`https://docs.python.org/3/library/os.html`

[^R10]: Python asyncio 官方文档，TaskGroup 的异常取消语义、gather 的差异和取消传播；实际版本仍以仓库为准。`https://docs.python.org/3/library/asyncio-task.html`


[^U0]: translate-book 仓库入口与 README。`https://github.com/deusyu/translate-book`
[^U1]: 上游 Skill 调用顺序、并行波次、提示、批后术语合并和构建步骤。`https://github.com/deusyu/translate-book/blob/main/SKILL.md`
[^U2]: convert.py 的源指纹、HTML/Markdown转换、结构块、超长切分和数字行清理。`https://github.com/deusyu/translate-book/blob/main/scripts/convert.py`
[^U3]: run_state.py 的 plan/build_chunk_record/record_chunks/save_run_state。`https://github.com/deusyu/translate-book/blob/main/scripts/run_state.py`
[^U4]: chunk_context.py 的 get_neighbor_context/_read_excerpt。`https://github.com/deusyu/translate-book/blob/main/scripts/chunk_context.py`
[^U5]: glossary.py 的 select_terms_for_chunk/_count_in_text/term_hash/校验与保存。`https://github.com/deusyu/translate-book/blob/main/scripts/glossary.py`
[^U6]: manifest.py 的 create_manifest/validate_for_merge/file_hash。`https://github.com/deusyu/translate-book/blob/main/scripts/manifest.py`
[^U7]: meta.py 与 merge_meta.py 的证据字段、prepare/apply和幂等记录。`https://github.com/deusyu/translate-book/blob/main/scripts/meta.py`；`https://github.com/deusyu/translate-book/blob/main/scripts/merge_meta.py`
[^U8]: merge_and_build.py 的图像检查、合并/缓存、格式构建与退出/cleanup。`https://github.com/deusyu/translate-book/blob/main/scripts/merge_and_build.py`
[^U9]: 上游 MIT LICENSE，Copyright (c) 2025 Rainman。`https://github.com/deusyu/translate-book/blob/main/LICENSE`
