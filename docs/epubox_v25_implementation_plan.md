# EPUBox v2.5 实施计划（审定版）

基准为 [v2.5 主文](epubox_mvp_design_v2_5.md)，原始 SHA256 `d8b9a4dd99608c880a9b0efb148574bc5f948675fc29bcbb702eaafb507ec376`。旧版设计及其评审只供比较，不叠加为本轮新需求。2026-09-29 基线：`codex/epubox-v23-implementation`、工作树无已有代码改动、`uv run pytest -q` 为 **616 passed**。

**用户本轮直接覆盖一项 v2.5 主文中的历史兼容建议：只保留新的翻译逻辑。** 主文 §10.5/§13 提议旧引擎继续恢复旧 HTML checkpoint；本轮按用户指令删除旧翻译执行路径。旧 checkpoint 不能混入 v2.5，也不启动旧代码；读取到时明确报不支持并要求建立新运行。保留可复用的安全解析、模型连接、格式校验、EPUB 包装功能，不因为来源于旧模块就删除正确性能力。

## 已证实的现状与增量

| 范围 | 当前仓库证据 | v2.5 要求 |
|---|---|---|
| 翻译入口 | `main.py` 已仅调 `translate_v23`，但还留旧 `generate-glossary`、`--limit`、`--preserve-fonts` 兼容入口 | 唯一新流程；默认术语提取随 `translate` 运行；旧入口/旧模块移除 |
| 用户词表 | `cli_v23.load_terms()` 把词条直接传给 `prepare_book()`；旧 map 默认映射为 `required` | 先存标准化用户副本，旧 map 默认 `preferred`，明确硬规则保留；用户优先于自动候选 |
| 源文档 | `extractor._attach_context()` 把已配置词条和 logical_hash 写入 `DocumentPlan.units` | DocumentPlan 仅存源定义与 SourceTextView；冻结后的词条、正式 logical_hash 放 UnitRecord/CutPlan |
| 准备与恢复 | 目前只有 `bookplan` building/ready；ready 前假设基本无模型账本 | `parsed_ready` 后可付费提取；freeze 后才建 ready bookplan；各提交点 JSON 恢复及累计费用 |
| 术语调用 | 只有 translate/review/coherence；旧 `GlossaryExtractor` 是独立 NLTK/TF-IDF 路线 | 默认覆盖整书源视图的 `epubox-terms-1`，严格候选/源证据，本地合并/核对/冻结 |
| 校对反馈 | `epubox-review-1`，无术语建议字段 | `epubox-review-2` 可选 term_suggestions，保存到 UnitRecord，不能动态改本轮词表 |

## 按依赖推进的任务

每任务完成时提交可复核的代码、最小必要回归与验证结果；后续任务只依赖已经通过的提交。先完成确定性/无模型路径，再发生受额度约束的真实模型调用。

| ID | 交付与关键文件 | 依赖 | 通过条件 |
|---|---|---|---|
| V00 | 固定主文哈希、基线测试、v2.5 与 v2.3 差异/冲突记录；新实施分支 | 无 | 616 基线可重现；旧计划源文不改；明确用户覆盖旧引擎兼容建议 |
| V01 | 立即移除 `main.generate-glossary`、旧兼容参数及其他旧公开入口，旧 checkpoint 明确拒绝；梳理并先迁新流程仍需的 verifier/装配/token 计数 | V00 | 普通 CLI 只有新引擎；旧日志不可再由 CLI 产生；共享验证/装配回归保持通过；此时不提前物理删除被新代码复用的旧模块 |
| V02 | `epubox-document/unit/book-3`、PreparationPlan、SourceTextView、TermPreparation/Glossary 值对象与 `epubox-request-2` RequestManifest 的 `owner_kind/owner_id` 判别身份；确定性 JSON 与旧协议拒绝 | V00 | DocumentPlan 类型不含生效术语/hash；提取/核对记录不伪造 Unit revision；`epubox-document-1/-2`、`epubox-unit-1/-2`、`epubox-book-1/-2`、`epubox-request-1`、`epubox-review-1` 给清晰 unsupported-format；正文 `epubox-text-1` 仍保留 |
| V03 | Store 增加 preparation、glossary/user、plan/extraction/candidates/freeze、glossary.json 文件协议及原子读写；将准备期文档登记/身份从当前 building BookPlan 迁到 PreparationPlan/TermPreparation，document 写入不依赖 bookplan.json；用户词表先标准化并验证 book/documents/units scope；定义跨阶段累计 HTTP/G 账本 | V02 | 用户坏文件在任何付费调用前拒绝；无词表保存明确空副本；Store 能重放各新类型但**不在此阶段提交 parsed_ready 或 building bookplan** |
| V04 | 从源槽位/Unit 构造连续 SourceTextView 和固定上下文，完整写入 DocumentPlan；源视图/槽位/哈希核验后才提交 P1 parsed_ready；再计划逐章提取窗口、窗口相关 user_terms、初始预算 `G=6N+3×group_limit` 并落盘 | V03 | P1 document 已含全部 views 且之后字节不变；P1/P2/P3 期间 bookplan.json 不存在，preparation.json 是源计划权威；每个主视图区间恰好被负责；硬保护不入模；0 邻接为空；后半书、跨格式词/表格有覆盖，窗口/总额度在模型前已持久化 |
| V05 | `epubox-terms-1` 严格协议，并用同一个 ModelRuntime/Store 请求台账扩展 terms+term_resolution 阶段；逐窗状态、每 HTTP 预留/计数、缩批/局部失败后继续；提取窗初始逻辑调用1+内容/协议修复1，每逻辑请求最多2次传输重试，窗上限6；冲突组仅1次逻辑核对/3次HTTP，按稳定ID自动处理最多20组 | V04 | 合法 `candidates=[]` 与失败分开；缺 item/重复键/截断拒绝；并发 1/2 中间失败后仍执行后窗；全局暂停不把 pending 伪装完成；准备期已耗次数在后续阶段不归零；Batch的全局HTTP只记一次，owner归属额度单独记，不能相加冒充全局次数 |
| V06 | 本地源引文核验、别名证据与同义边界、结构化去重/源词频；用户优先的部分重叠归并、同词异义 scope 与每冲突组一次 `epubox-term-resolution-1` 核对 | V05 | 虚构 quote/错 view/hint-only/未知 ID 被拒且其他候选保留；冲突不按频次强选；用户规则不被自动词条覆盖或缩范围绕过；重复响应幂等 |
| V07 | candidates 收口，closed/closed_with_gaps/disabled/not_required 区分；freeze 意图先行、确定性 glossary 快照、rules/file hash；用纯 TermSelector 按 Unit scope/已证 alias 选词及只读 context 角色，保存 selected_term_ids/terms_hash/context_hash、正式 logical_hash、初始 CutPlan/UnitRecord，最后**一次性创建 ready bookplan** | V06 | freeze 中断只重放同一 payload、不重复付费；冻结前无正文请求；ready 前每个可执行 item 已保存实际词条/语境身份，ready 后不补算或改写；此时计算翻译额度 T：每 Unit `24×max(1,初始Segment数)`、章节衔接 `6×初始窗口数`，无更低硬总限额时运行总上界为已持久化的 G+T；DocumentPlan 字节不变；迟到候选不改变词表；局部提取缺口披露后可继续；auto_extract=false 也经 disabled→freeze→P4 |
| V08 | `epubox-review-2` 提示/协议与可选 term_suggestions；源证据校验和幂等 UnitRecord.term_feedback；重大错义保持 issue | V07 | 无建议合法；坏建议不覆盖质量问题；新建议不写入冻结池/词表；此阶段不产生 review-1 正文结果 |
| V09 | 从已冻结且已规划的 UnitRecord/RequestManifest 构造请求并核验既存 selected IDs/角色/terms/context hash；正式翻译直接用 review-2 校对与强规则检查；CutPlan 升级仅从冻结词表/固定 SourceTextView 确定性重选并原子提高 plan_epoch、废弃该 Unit 所有旧片段及校对，保留其他 Unit；>50 词无静默截断 | V08 | `C++/.NET`、cat/category、大小写、所有格、重叠别名正确；context 词条不成为当前目标硬约束；派发后不重算已冻结的选择身份；预算放不下只挂本 Unit；初译/协议修复/重译、校对/复核和章节修订均按§9.2有界，plan_epoch/revision变化不重置累计Unit/运行额度；无旧 review-1 请求 |
| V10 | 两阶段只读 `plan_resume`、续跑和报告：准备/提取/核对/冻结/正文 action+reasons，CLI 默认 auto_extract=true、显式 false；同一运行端到端 P1→P5 | V01、V09 | 两次只读预览字节不变/0 HTTP；提取已收费而无 bookplan 时准确恢复；全局暂停不提前冻结；`in_flight`按可能收费计数、manifest/owner不同步保守占额度；局部坏Unit/Document仅可定位时隔离，共享source/preparation/glossary/book身份坏则停止；blocked_dependency不占worker，依赖满足后重新入队；不做record_only、glob回退或未知外部目标自动认领；显式追加额度留档且不清零历史；旧checkpoint仅unsupported-format；普通单命令可进入两门槛流程 |
| V11 | 发布前复核 preparation/freeze/glossary/bookplan/Unit/checks 身份；原模板重建、资源多重集、真实 EPUBCheck、原子发布凭证 | V10 | closed_with_gaps 已披露可继续，但正文失败不放行；旧输出/mtime/日志不伪成功；发布失败可从 JSON 重建，不重翻有效 Unit |
| V12 | 新流程的相关验证/装配/恢复回归已通过后，按 V01 逐文件裁决表迁移共享能力后，物理删除旧 orchestrator/workflow/translator/proofer/validator/fallback_runtime/Parser/Builder/DomReplacer、旧schemas、旧GlossaryExtractor及旧item模块的legacy-only实现、旧专属测试与只供旧逻辑使用的依赖；清理生产和测试 import closure | V11 | 无legacy-only源文件或专属测试残留；不能导入旧 orchestrator/workflow；CLI不再暴露 `--engine` 兼容选择；生产/测试 import 闭包、CLI help、wheel/package smoke均只有新翻译路径；共享正确性能力及测试已迁移；旧 checkpoint 明确拒绝而不再启旧代码 |
| V13 | 按 [逐号验收矩阵](epubox_v25_test_matrix.md) 执行 T01—T60，完整全库/类型/lint/静态检查；具名 EPUB2/3 实书术语→翻译→重建及恢复、同模型历史结果比较，成本、人工评阅与阅读器记录 | V12 | 每个T有具名测试节点/fixture/期望状态与命令；确定性错误目标为0；旧结果只用已保存历史证据；自动、代理修订、用户人评分开；未完成外部人评/阅读验收时不得宣称 v2.5 质量放行 |

## V01/V12 旧代码逐文件裁决

此表是 V01 的产物清单，V12 逐项复核并删除。共享函数迁移后的测试必须先通过，才删除旧测试；不因模块名来自旧项目就删除新流程仍需要的能力。

| 现有位置 | 裁决与完成阶段 |
|---|---|
| `main.py` 中 `generate-glossary`、`--limit`、`--preserve-fonts`、`--engine` 兼容选择 | V01 删除；`translate` 与 `resume` 只接唯一 v2.5 引擎 |
| `engine/orchestrator.py`、`engine/agents/{workflow,translator,proofer,validator,fallback_runtime}.py`、旧 `agents/schemas.py` | V12 删除；模型连接 `agents/models.py` 与已用的新 runtime 保留 |
| `engine/agents/verifier.py` | V01 先把新流程实际使用的英文残留/退化/最终 HTML/硬词表校验迁到中性质量模块；V12 删除旧 HTML chunk 专属校验，迁移对应回归 |
| `engine/epub/{parser,builder}.py`、`engine/epub/replacer.py` 的 `DomReplacer` | V12 删除；先把 `assemble_document` 迁到新装配模块并使 publication/tests 使用它，保留安全 ZIP/EPUB 校验与发布代码 |
| `engine/item/{chunker,precode,replacer,xpath}.py` | `count_tokens` 先迁 `core/tokens.py`；其余旧 DOM chunk/placeholder/xpath 路线在 V12 按最终 import 图删除，保留新版 extractor/inline/planner |
| `engine/services/glossary.py` | V01 移除独立命令；V12 删除旧 NLTK/TF-IDF 提词和 Loader，v2.5 新 TermExtractor/候选/冻结服务取代它 |
| `engine/schemas/{chunk,epub,translator}.py`、旧 `__init__` 导出 | V12 删除；新格式模型只留 v2.5 所需对象，旧检查点读取得明确 unsupported-format |
| `tests/test_orchestrator.py`、`tests/engine/agents/test_workflow.py`、旧 Parser/Builder/Glossary/Chunker/Precode/XPath 专属测试 | V12 删除旧行为断言前迁移局部失败、结构校验、装配、断点恢复对应断言到新测试；Provider/共享验证测试保留并改用新模块 |
| `pyproject.toml`/`uv.lock` 中仅旧链路使用的包 | V12 在生产 import closure 证实后删除；不凭包名猜依赖，不新增术语专用第三方包 |

## 失败预演与最早信号

| 失败方式 | 最早可观察信号 | 恢复/防回归节点 |
|---|---|---|
| P2 已有收费提取，但进程在 bookplan 创建前退出 | preparation=parsed_ready，requests 存在 sent attempt，bookplan.json 不存在 | T49/T30：恢复已成功窗口及费用，只补缺项，不把“无 BookPlan”当零调用 |
| freeze 意图、glossary 快照或 P4 初始 Unit 写入时中断 | freeze.json 有效而 glossary.json/bookplan 不齐，或文件哈希与意图不符 | T52/T57：只重放同一 snapshot_payload；损坏身份显式停，不重新提取或猜词表 |
| CutPlan 升级后使用旧术语选择或清零额度 | plan_epoch 已变，但 segment term IDs/hash 与冻结词表/视图不一致 | T09/T24/T31/T37/T39/T59：同一 Unit 全片重绑、旧代次拒绝、累计次数不归零 |
| 清理旧模块时共享装配/验证函数被删，或仍能启旧工作流 | import 闭包断裂、CLI help 暴露 legacy、wheel smoke 能导入旧 orchestrator/workflow | T17/T23/T26/T42 与 V12 import/CLI/package smoke；先迁共享测试再删模块 |
| 技术书术语阶段触及硬总额却把剩余窗口记成局部缺口 | run.paused 与未尝试窗口并存，但 extraction_status 错记 closed_with_gaps | T50/T51/T56/T58、真实 `work/v25-acceptance/report.json`：停新派发、保持 pending、追加额度后仅补缺口 |

## 审查重点与边界

- **术语覆盖 ≠ 术语召回率。** V04 验证输入覆盖；V06 验证候选证据；V13 才评估真实译法与成本。任何一步不把模型自报置信度当成证据。
- **阶段身份不能循环。** preparation 不依赖未来 glossary；freeze 意图引用源/候选池，glossary 只引用 freeze_id；BookPlan 最后引用前述文件。文档 `source_markup` 与 Unit 源 ID 不随术语改变。
- **局部失败继续。** 提取窗口与正文 Unit 共用有界请求调度/日志，准备期失败不减少正文分母；只有账号、硬预算、存储或不可信共享输入使运行全局暂停/失败。
- **旧逻辑删除的真实边界。** 新引擎复用的 `verify_final_html`、`classify_untranslated_english_texts`、`find_degenerate_translation`、`assemble_document`、`count_tokens` 先迁入中性模块；删除旧工作流不等于删掉验证/装配能力。
- **不新增依赖和外部代码搬运。** 当前模型 SDK、lxml、regex、tiktoken、Pydantic 和原子 Store 足以构建；若后来确需第三方代码，再固定上游提交和许可证，而不是照文档示例复制旧项目。
- **不承诺一次模型调用全部成功。** 一条普通 CLI 命令启动完整有界运行；已成功结果逐项保存，局部失败后自动继续，最终只在全部质量/结构条件通过后发布。真实模型及人工质量仍可能需要后续修订。

审查状态：Architect 与 Critic 均已在补齐预算、重规划、旧代码清单、恢复和逐项验收边界后 **APPROVE**。按 V00→V13 顺序执行；可并行的独立文件改动须明确所有权、先跑依赖门槛。
