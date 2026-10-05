# 第一阶段基线与交接合同

范围：T00、T01、T02。需求来自 [分块方案](chunking.md)、[迁移清单](migration.md)、[任务计划](tasks.md)。文件整改归属见 [完整清单](files.md)。

## 固定代码来源

| 来源 | 完整提交 | 用途 |
|---|---|---|
| main | 61ebb35ea027c9b7abe80b2603c2850afb6b9ea3 | feature/preflight 实施基线 |
| codex/epubox-v25-implementation | 7d882be980fffd7892419304be24480860c60c18 | 选择性复用目录迁移与预算 v2 |
| main 历史 | ec8b22e8af1a13286ae46f9b17933619aaf4158d | 核对原 workflow 三阶段及历史测试 |

历史提交是 main 的祖先。当前 main 与 donor 均没有 engine/agents/workflow.py；恢复可执行入口归 T16。先前 WIP 不在来源清单中，不作为已验收实现。

## 文件合同

- 单个单词文件名，扩展名除外；每个文本文件最多 1000 个物理行，包含注释、空行与无末尾换行的最后一行。
- 用户已授权工具固定名称例外。明确保留 __init__.py（Python 包）、pyproject.toml（项目和工具配置）、.gitignore（Git）、.env.example（既有 dotenv 模板）。uv.lock 为既有依赖锁文件；本轮不改依赖。
- pytest 已使用 python_files = ["*.py"]，无需以 test_ 为文件名前缀。函数名仍沿用 pytest 的测试约定。
- contracts.py 按共享 JSON/基础、源、术语、执行记录拆为 base.py、source.py、terms.py、run.py；旧公开入口仍从 contracts.py 导出，JSON 格式、字段、验证和哈希语义不变。
- source_internal.py 改名 internal.py；atomic_store.py 改名 atomic.py；所有导入同步。文档改名及历史长文档按章节拆分，历史内容仍可阅读。
- 其余文件的改名/拆分由清单中的责任任务执行；本阶段不宣称整个仓库已经满足最后的全量验收。

## 最小交接字段与版本

交接契约位于 engine/schemas/bridge.py；预算纯值合同位于 engine/schemas/budget.py，归 T00 冻结，实际计量算法归 T01。沿用已有模型，只有原字节位置、完整原子项与阶段交接缺失的字段新增。没有新增执行器。

| 逻辑对象 | 实际对象/最小新增字段 | 版本与边界 |
|---|---|---|
| SourceContext | 直接复用 PreparationPlan；快照路径、source_hash、run_id、阅读顺序、文档与源单元清单、冻结配置 | epubox-preparation-1，不修改旧格式 |
| SourceMap | document_id、source_hash、document_hash、encoding、source_size；SourceLocation 含 node_key、SourceRef、ByteSpan、field/attribute_name；保护字节区间 | epubox-map-1；byte_start/byte_end 是原始字节半开区间，SourceRef.start/end 是已解码槽位字符位置，不能互换 |
| AtomicItem | 扩展 Unit；item_id、ordinal、channel、atomic_tag、source_span；复用 projection/registry/slot_ids | 最外层 p/table/em/i/ul/ol；原子生产和覆盖校验归 T05/T06，不由值对象假装完成 |
| FrozenTerms | 直接复用 GlossarySnapshot；source_hash、freeze_id、用户规则身份、证据/作用域、缺口状态 | epubox-glossary-1；closed/closed_with_gaps/disabled/not_required 可交接，open/paused 不可冒充冻结 |
| RequestBatch | 复用 RequestManifest；有序 AtomicItem、整批共享 context、完整 payload、严格 BudgetResult | epubox-batch-1；同文档/通道、顺序、每个原子一次、最多两个片段且各≤400字符 |
| PreparedInput | 原 PreparationPlan/GlossarySnapshot/BookPlan 引用；PreflightCheck、map_hashes、plan_hashes | epubox-prepared-1；源/run/freeze/文档/单元/计划身份一致，预检必须通过 |
| ItemResult | 直接复用 ItemRecord；stage/status、当前译文与哈希、请求/响应身份、校验和错误 | 保持当前 Unit/请求合同；逐阶段实际保存归 T17 |

PreflightCheck 绑定 source_hash、map_hashes、atoms_hash、budget_hash、passed=True。T08 负责实际计算并落盘；T15 负责必需文件提交与回读（包括对象内无法独立验证的 freeze 文件 SHA），T11/T12/T16 负责派发前检查。准备与词表的 canonical hash、翻译配置相等和提取配置 hash 已在交接对象校验。构造一个 PreparedInput 值对象不能替代磁盘回读和预检。

RequestBatch.budget 使用 T01 的严格 BudgetResult，保存时包含 S、I 估算与预留、R、总上下文、失败原因、stage/review_targets 和完整 BudgetIdentity。RequestBatch 必须匹配当前阶段、fits=True 及既有 runtime.wire_hash；完整载荷、系统提示和输出上限共同绑定身份。review 必须使用已保存目标及对应哈希，不能派发预估。

## QName 与字段白名单

| 资源 | 可翻译位置 | 保护范围 |
|---|---|---|
| XHTML（http://www.w3.org/1999/xhtml） | body 的登记文本；head/title 的文本；head/meta[@name="description"] 的 content | 其余 head、CSS/JS 原字节；id/href/src 等固定属性 |
| NCX（http://www.daisy.org/z3986/2005/ncx/） | docTitle/text、navLabel/text | content@src、导航顺序、锚点和标识符 |
| OPF 中 DC 元数据（http://purl.org/dc/elements/1.1/） | metadata 下选定的 title、纯文本 description | creator、identifier、manifest、spine、路径及链接关系 |
| 可译 HTML/XHTML 属性 | alt、title、aria-label、aria-description | 非白名单属性、保护子树的固定结构 |

前缀任意，匹配使用 namespace URI 和 local name。真正 HTML 的同名位置仍需 T03/T04 证明映射安全。多书名/简介沿用已有唯一主字段选择规则；富文本简介不自动拉平。translate=no/yes 的岛、父原子与属性同时回填、保护嵌套由 T05—T07 验证；不能擅自扩展白名单。

## 原 workflow 行为与新合同的区别

历史位置均相对于 ec8b22e 的 engine/agents/workflow.py 及 tests/engine/agents/test_workflow.py：

| 情形 | 历史事实 | 新任务验收期望 |
|---|---|---|
| 正常初译 | translate_step:722；有效初译进入 TRANSLATED | 保留同一初译→校对→应用修订顺序，保存完整结果 |
| 已有有效译文 | :728—729 直接复用 | 先核对身份，复用已付费成功结果 |
| 无修订 | 空 corrections，应用后 COMPLETED | 保留已校验候选并保存，不重复翻译 |
| 有效修订 | apply_corrections_step:884—926，文本节点修订后校验 | 有效修订实际成为当前版本并保存，不仅记录建议 |
| 标记损坏修订 | :887—890 过滤占位符；:913—921 结构失败回滚 | 拒绝损坏结果，保留已付费初译并明确失败/待处理，不能伪报校对成功 |
| 初译全部失败 | :715—719 保存空 translated、TRANSLATION_FAILED；旧注释与代码不一致 | 不回填空结果或原文来冒充完成 |
| 校对重试全部失败 | :854—858 保留初译并创建空修订，继续 | 按新合同明确失败，不能把“未完成校对”标记为已完成 |
| 导航文本 | :761、:880—882 跳过校对/修订 | 依文档规定复用合法标题或处理白名单，不引入无限依赖 |
| 模型安全异常 | :811—817/:842—846 使用备用模型 | 不整包恢复旧备用供应商策略 |

Workflow 的三个步骤在历史 :929—938 串联。历史成功、无修订、修订、无效修订、异常测试分别位于 test_workflow.py:88、1514、1618、1637、1799、1825 等位置；T16 复用行为意图并增加准备与身份守卫。

本阶段的 contracts.py/bridge.py 测试验证已有 JSON 往返、版本、坐标和局部标记守卫；修订夹具明确列出上述成功/失败期望。它们不是恢复后的完整 workflow 运行测试，后者必须由 T16 实施并验收。

## T01 与 T02 的实施边界

- Settings.EPUB_CHUNK_MAX_TOKENS 默认 2000，可通过 .env 改为 5000；resolve_chunk_limit 明确值优先，非法值拒绝。
- measure_budget(stage, payload, limits, ...) 从唯一完整载荷生成实际请求消息，是纯计量，不发模型请求。预算 v2 保存 tokenizer/版本/模型匹配情况及 50%+256 输入余量，完整输出容量和 provider/context 限制独立检查；分词失败不继续。
- 历史 PlannerConfig 的 1200 限制只为旧计划校验保留；新纯预算没有此隐藏上限。--limit CLI 绑定、新原子提取/合批和正文执行接通分别归 T19、T05/T14、T16，不能将本阶段宣称为这些功能已上线。
- CLI 默认工作目录为源书旁同 stem 目录，默认成品为 stem-cn.epub；显式 work-root/output 与 overwrite 合同保持。
- 迁移只沿已验证的旧位置进行同文件系统 rename，持有父/子任务锁，不覆盖竞争目录；拒绝悬空链接。跨文件系统明确报错，保留旧目录，不增设自动复制迁移器。
- 稳定 source.epub 快照、before/after/snapshot 哈希核对及 EPUB/ZIP 校验复用已有实现；CLI 的首次哈希通过运行期 expected_source_hash 绑定快照，身份变化在创建新运行目录前拒绝。测试使用隔离目录，不修改真实书籍或其保存记录。

## 验证记录

实施前基线：pytest 375 passed（49.91 秒）；Ruff 和 Pyright 无错误。

最终测试结果与提交记录见 [任务进度](tasks.md)。本阶段不调用付费模型；真实中文 EPUB 完整闭环属于后续整体功能验收。
