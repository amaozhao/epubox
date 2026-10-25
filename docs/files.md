# 文件约束清单

基线：main@61ebb35；范围为版本管理文件及本轮需求文档。表中行数为实施前快照；本轮与后续任务均需核对实际行数。

用户已授权固定工具名称例外：__init__.py、pyproject.toml、.gitignore、.env.example；uv.lock 使用依赖工具生成的既有名称。测试文件统一使用 `test_<单词>.py`，其中 `test_` 是 pytest 工具前缀，主体仍必须是一个英文单词；已配置 `python_files = ["test_*.py"]`。

“待实施”行必须由责任任务完成；T00 验收检查全部违规均有归属，T20 验收时待实施项必须为零。仅为改名同步 import 的文件不提前转移业务归属。

| 基线路径 | 目标路径/拆分边界 | 基线行数 | 责任 | 本轮状态 |
|---|---|---:|---|---|
| .env.example | .env.example | 17 | T00 | 工具固定名称 |
| .gitignore | .gitignore | 224 | T00 | 工具固定名称 |
| LICENSE | LICENSE | 21 | T00 | 保留 |
| README.md | README.md | 92 | T00 | 保留 |
| clean.sh | clean.sh | 28 | T00 | 保留 |
| docs/epub_chunking_plan.md | docs/chunking.md | 401 | T00 | 完成 |
| docs/epub_preflight_tasks.md | docs/tasks.md | 388 | T00 | 完成 |
| docs/epub_pretranslation_migration.md | docs/migration.md | 436 | T00 | 完成 |
| docs/epubox_architecture_plan.md | docs/history/architecture/plan.md | 417 | T00 | 完成 |
| docs/epubox_architecture_review.md | docs/history/architecture/review.md | 82 | T00 | 完成 |
| docs/epubox_blind_review.md | docs/history/review.md | 47 | T00 | 完成 |
| docs/epubox_implementation_status.md | docs/history/status.md | 124 | T00 | 完成 |
| docs/epubox_leadership_book_test.md | docs/history/leadership/test.md | 37 | T00 | 完成 |
| docs/epubox_mvp_design_v2_5.md | docs/history/design/plan.md；后半 storage.md（第10节起） | 1320 | T00 | 完成 |
| docs/epubox_p14_acceptance.md | docs/history/publication/acceptance.md | 137 | T00 | 完成 |
| docs/epubox_p14_blind_review.md | docs/history/publication/review.md | 54 | T00 | 完成 |
| docs/epubox_redesign.md | docs/history/redesign.md | 860 | T00 | 完成 |
| docs/epubox_v25_implementation_plan.md | docs/history/implementation/plan.md | 74 | T00 | 完成 |
| docs/epubox_v25_status.md | docs/history/implementation/status.md | 32 | T00 | 完成 |
| docs/epubox_v25_test_matrix.md | docs/history/implementation/tests.md | 78 | T00 | 完成 |
| docs/设计文档.md | docs/history/design.md | 476 | T00 | 完成 |
| docs/重构文档.md | docs/history/refactor/plan.md；后半 translation.md（翻译工作流起） | 1097 | T00 | 完成 |
| engine/__init__.py | engine/__init__.py | 0 | T00 | 工具固定名称 |
| engine/agents/__init__.py | engine/agents/__init__.py | 0 | T00 | 工具固定名称 |
| engine/agents/models.py | engine/agents/models.py | 48 | T16 | 保留 |
| engine/agents/protocol.py | engine/agents/protocol.py | 241 | T16 | 保留 |
| engine/agents/runtime.py | engine/agents/runtime.py | 636 | T16 | 保留 |
| engine/agents/streaming_openai_like.py | engine/agents/streaming.py | 112 | T16 | 完成 |
| engine/agents/term_protocol.py | engine/agents/terms.py | 297 | T11 | 完成 |
| engine/cli.py | engine/cli.py | 674 | T02 | 保留 |
| engine/constant.py | engine/constant.py | 3 | T00 | 保留 |
| engine/core/__init__.py | engine/core/__init__.py | 0 | T00 | 工具固定名称 |
| engine/core/config.py | engine/core/config.py | 93 | T01 | 保留 |
| engine/core/logger.py | engine/core/logger.py | 94 | T00 | 保留 |
| engine/core/markup.py | engine/core/markup.py | 123 | T03 | 保留 |
| engine/core/quality.py | engine/core/quality.py | 525 | T06 | 保留 |
| engine/core/styles.py | engine/core/styles.py | 339 | T07 | 保留 |
| engine/core/tokens.py | engine/core/tokens.py | 27 | T01 | 保留 |
| engine/epub/__init__.py | engine/epub/__init__.py | 1 | T00 | 工具固定名称 |
| engine/epub/assembly.py | engine/epub/assembly.py | 220 | T07 | 保留 |
| engine/epub/checker.py | engine/epub/checker.py | 31 | T18 | 保留 |
| engine/epub/derived_bindings.py | engine/epub/bindings.py | 123 | T18 | 已完成 |
| engine/epub/preparation.py | engine/epub/preparation.py | 260 | T02 | 保留 |
| engine/epub/publication.py | engine/epub/publication.py；engine/epub/verification.py；engine/epub/publish.py | 1024 | T18 | 已完成 |
| engine/epub/validation.py | engine/epub/validation.py | 593 | T02 | 保留 |
| engine/item/__init__.py | engine/item/__init__.py | 1 | T00 | 工具固定名称 |
| engine/item/extractor.py | engine/item/extractor.py | 374 | T05 | 保留 |
| engine/item/inline.py | engine/item/inline.py | 337 | T06 | 保留 |
| engine/item/planner.py | engine/item/planner.py | 899 | T14 | 保留 |
| engine/item/source_views.py | engine/item/views.py | 226 | T05 | 完成 |
| engine/item/structural_extractor.py | engine/item/structure.py；metadata.py/projection.py/policy.py 按源归属、元数据、投影与规则拆分 | 1592 | T05 | 完成 |
| engine/item/unit_planner.py | engine/item/context.py | 728 | T13 | 完成 |
| engine/orchestrator.py | engine/orchestrator.py；执行职责拆入 execution/ 下单词文件 | 2487 | T16 | 完成 |
| engine/schemas/__init__.py | engine/schemas/__init__.py | 1 | T00 | 工具固定名称 |
| engine/schemas/contracts.py | engine/schemas/contracts.py；base.py/source.py/terms.py/run.py 按JSON、源、术语、执行职责拆分 | 1406 | T00 | 完成 |
| engine/schemas/source_internal.py | engine/schemas/internal.py | 616 | T00 | 完成 |
| engine/services/__init__.py | engine/services/__init__.py | 1 | T00 | 工具固定名称 |
| engine/services/atomic_store.py | engine/services/atomic.py | 117 | T02 | 完成 |
| engine/services/coherence.py | engine/services/coherence.py | 308 | T16 | 保留 |
| engine/services/preparation_pipeline.py | engine/services/preparation.py | 520 | T15 | 完成 |
| engine/services/report.py | engine/services/report.py | 198 | T19 | 保留 |
| engine/services/resume_plan.py | engine/services/resume.py | 191 | T17 | 已完成 |
| engine/services/store.py | engine/services/store.py | 951 | T17 | 保留 |
| engine/services/term_candidates.py | engine/services/terms/candidates.py | 381 | T12 | 完成 |
| engine/services/term_freeze.py | engine/services/terms/freeze.py | 541 | T12 | 完成 |
| engine/services/term_inputs.py | engine/services/terms/inputs.py | 97 | T09 | 完成 |
| engine/services/term_planning.py | engine/services/terms/planning.py | 591 | T10 | 完成 |
| engine/services/term_resolution.py | engine/services/terms/resolution.py | 585 | T12 | 完成 |
| engine/services/term_runner.py | engine/services/terms/runner.py；存储辅助拆入 storage.py | 568 | T11 | 完成 |
| main.py | main.py | 178 | T02 | 保留 |
| pyproject.toml | pyproject.toml | 92 | T00 | 工具固定名称 |
| tests/__init__.py | tests/__init__.py | 0 | T00 | 工具固定名称 |
| tests/chapter1.xhtml | tests/chapter.xhtml | 277 | T20 | 待实施 |
| tests/engine/__init__.py | tests/engine/__init__.py | 0 | T00 | 工具固定名称 |
| tests/engine/agents/__init__.py | tests/engine/agents/__init__.py | 0 | T00 | 工具固定名称 |
| tests/engine/agents/test_models.py | tests/engine/agents/test_models.py | 79 | T16 | 完成 |
| tests/engine/agents/test_protocol.py | tests/engine/agents/test_protocol.py | 110 | T16 | 完成 |
| tests/engine/agents/test_provider.py | tests/engine/agents/test_provider.py | 20 | T16 | 完成 |
| tests/engine/agents/test_runtime_protocol.py | tests/engine/agents/test_runtime.py | 980 | T16 | 完成 |
| tests/engine/core/__init__.py | tests/engine/core/__init__.py | 0 | T00 | 工具固定名称 |
| tests/engine/core/test_quality.py | tests/engine/core/test_quality.py | 27 | T06 | 完成 |
| tests/engine/core/test_tokens.py | tests/engine/core/test_tokens.py | 13 | T01 | 完成 |
| tests/engine/epub/__init__.py | tests/engine/epub/__init__.py | 0 | T00 | 工具固定名称 |
| tests/engine/epub/book_factory.py | tests/engine/epub/test_factory.py | 65 | T02 | 完成 |
| tests/engine/epub/test_assembly.py | tests/engine/epub/test_assembly.py | 8 | T07 | 完成 |
| tests/engine/epub/test_checker.py | tests/engine/epub/test_checker.py | 31 | T18 | 已完成 |
| tests/engine/epub/test_derived_bindings.py | tests/engine/epub/test_bindings.py | 86 | T18 | 已完成 |
| tests/engine/epub/test_preparation.py | tests/engine/epub/test_preparation.py | 210 | T02 | 完成 |
| tests/engine/epub/test_publication.py | tests/engine/epub/test_publication.py | 537 | T18 | 已完成 |
| tests/engine/item/__init__.py | tests/engine/item/__init__.py | 0 | T00 | 工具固定名称 |
| tests/engine/item/test_extractor.py | tests/engine/item/test_extractor.py | 338 | T05 | 完成 |
| tests/engine/item/test_inline_planner.py | tests/engine/item/test_inline.py | 715 | T06 | 完成 |
| tests/engine/item/test_planner.py | tests/engine/item/test_planner.py | 357 | T14 | 完成 |
| tests/engine/item/test_limit.py | tests/engine/item/test_limit.py | 49 | T20 | 完成 |
| tests/engine/item/test_source_views.py | tests/engine/item/test_views.py | 185 | T05 | 完成 |
| tests/engine/schemas/__init__.py | tests/engine/schemas/__init__.py | 0 | T00 | 工具固定名称 |
| tests/engine/schemas/test_contracts.py | tests/engine/schemas/test_contracts.py | 803 | T00 | 完成 |
| tests/engine/services/__init__.py | tests/engine/services/__init__.py | 0 | T00 | 工具固定名称 |
| tests/engine/services/test_coherence.py | tests/engine/services/test_coherence.py | 106 | T16 | 完成 |
| tests/engine/services/test_preparation_pipeline.py | tests/engine/services/test_preparation.py；新原子准备验收见 test_preparing.py/test_ready.py | 433 | T15 | 完成 |
| tests/engine/services/test_report.py | tests/engine/services/test_report.py | 113 | T19 | 已完成 |
| tests/engine/services/test_response.py | tests/engine/services/test_response.py | 141 | T20 | 完成 |
| tests/engine/services/test_resume_plan.py | tests/engine/services/test_resume.py | 69 | T17 | 已完成 |
| tests/engine/services/test_store.py | tests/engine/services/test_store.py | 522 | T17 | 已完成 |
| tests/engine/services/test_store_contracts.py | tests/engine/services/test_contracts.py | 255 | T17 | 已完成 |
| tests/engine/services/test_term_candidates.py | tests/engine/services/terms/test_candidates.py | 322 | T12 | 完成 |
| tests/engine/services/test_term_freeze.py | tests/engine/services/terms/test_freeze.py | 358 | T12 | 完成 |
| tests/engine/services/test_term_inputs.py | tests/engine/services/terms/test_inputs.py | 118 | T09 | 完成 |
| tests/engine/services/test_term_planning.py | tests/engine/services/terms/test_planning.py | 561 | T10 | 完成 |
| tests/engine/services/test_term_rejection_audit.py | tests/engine/services/terms/test_audit.py | 139 | T12 | 完成 |
| tests/engine/services/test_term_resolution.py | tests/engine/services/terms/test_resolution.py | 489 | T12 | 完成 |
| tests/engine/services/test_term_response_journal.py | tests/engine/services/terms/test_response.py | 184 | T11 | 完成 |
| tests/engine/services/test_term_runner.py | tests/engine/services/terms/test_runner.py；并发与存储边界测试拆入 test_execution.py、test_storage.py | 653 | T11 | 完成 |
| tests/engine/test_cli.py | tests/engine/test_cli.py | 563 | T02 | 完成 |
| tests/engine/test_orchestrator.py | tests/engine/execution/test_translation.py；其余执行职责测试位于 execution/ 下单词文件 | 2095 | T16 | 完成 |
| tests/engine/test_single_command_e2e.py | tests/engine/test_integration.py | 221 | T20 | 完成 |
| tests/engine/test_adversary.py | tests/engine/test_adversary.py | 208 | T20 | 完成 |
| tests/test_image.jpg | tests/image.jpg | 94 | T20 | 待实施 |
| tests/test_main.py | tests/test_main.py | 106 | T02 | 完成 |
| tests/toc.ncx | tests/toc.ncx；以必要导航边界重建小夹具，不机械截断XML | 6139 | T20 | 待实施 |

## 本轮新增文件

| 路径 | 责任 | 约束 |
|---|---|---|
| engine/schemas/bridge.py、engine/schemas/budget.py、tests/engine/schemas/test_bridge.py、tests/engine/schemas/test_files.py | T00 | 交接契约/测试，单词且≤1000行 |
| engine/item/budget.py、tests/engine/item/test_budget.py、tests/engine/core/test_config.py | T01 | 纯预算/配置测试，单词且≤1000行 |
| docs/baseline.md、docs/files.md、docs/tasks.md、docs/chunking.md、docs/migration.md、docs/review.md、docs/extraction.md | T00 | 基线/清单/需求/计划，单词且≤1000行 |

## 引用同步与兼容验证

- Python 文件移动必须同步所有 import、模块别名、测试夹具引用、mock 路径和工具发现配置；不能遗留只为保留复合文件名而添加的转发文件。
- schema 拆分保持 contracts.py 公开入口和既有 JSON 格式/哈希，比较旧模型 JSON Schema，并运行全套回归。
- 文档移动同步相对链接；历史长文档按完整章节拆分并添加前后文入口。
- 任务的基线统计不冒充当前行数；T20 重新遍历全部维护文件并做单词人工复核。

## 第二阶段新增文件

| 路径 | 责任 | 约束 |
|---|---|---|
| engine/epub/parsing.py、tests/engine/epub/test_parsing.py | T03 | 严格资源解析/反例，单词且≤1000行 |
| engine/epub/ranges.py、tests/engine/epub/test_ranges.py | T04 | 原字节区间映射/反例，单词且≤1000行 |
| engine/item/atoms.py、tests/engine/item/test_atoms.py | T05 | 原子提取/覆盖/反例，单词且≤1000行 |
| engine/item/metadata.py、engine/item/projection.py、engine/item/policy.py | T05 | 原有职责拆分，单词且≤1000行 |
| docs/extraction.md | T05 | 本阶段交接合同与验收记录，单词且≤1000行 |

## 第三阶段新增文件

| 路径 | 责任 | 约束 |
|---|---|---|
| engine/epub/fill.py、tests/engine/epub/test_fill.py | T07 | 原字节局部回填/反例，单词且≤1000行 |
| tests/engine/item/test_markers.py | T06 | 目标标记与字面标记反例，单词且≤1000行 |
| engine/services/terms/__init__.py、tests/engine/services/terms/__init__.py | T09/T10 | 明确的Python包固定名称例外 |
| docs/translation.md | T06/T07/T09/T10 | 本批次交接与验收，单词且≤1000行 |

## 第四阶段新增文件

| 路径 | 责任 | 约束 |
|---|---|---|
| engine/services/preflight.py、tests/engine/services/test_preflight.py | T08 | 零模型原子预检、凭据校验及反例，单词且≤1000行 |
| engine/agents/terms.py、engine/services/terms/runner.py、engine/services/terms/storage.py | T11 | 术语协议、批量调度和日志存储，单词且≤1000行 |
| tests/engine/services/terms/test_runner.py、tests/engine/services/terms/test_execution.py、tests/engine/services/terms/test_response.py、tests/engine/services/terms/test_storage.py | T11 | 计量、并发、恢复和派发门禁测试，单词且≤1000行 |
| engine/services/terms/candidates.py、engine/services/terms/freeze.py、engine/services/terms/resolution.py | T12 | 候选、冻结和冲突核对，单词且≤1000行 |
| tests/engine/services/terms/test_candidates.py、tests/engine/services/terms/test_freeze.py、tests/engine/services/terms/test_resolution.py、tests/engine/services/terms/test_audit.py | T12 | 证据、终态和冻结测试，单词且≤1000行 |
| tests/engine/agents/test_budget.py | T11 | 完整模型消息 token 预算测试，单词且≤1000行 |
| docs/terminology.md | T08/T11/T12 | 本批次交接与验收，单词且≤1000行 |

## 第五阶段新增文件

| 路径 | 责任 | 约束 |
|---|---|---|
| engine/item/context.py、tests/engine/item/test_planner.py | T13 | 原正文术语与上下文规划模块改名及兼容测试，单词且≤1000行 |
| engine/item/request.py、tests/engine/item/test_request.py | T13 | 原子正文的最小模型请求投影及反例，单词且≤1000行 |
| engine/item/packing.py、tests/engine/item/test_packing.py | T14 | 完整原子项的顺序预算合批及反例，单词且≤1000行 |
| docs/packing.md | T13/T14 | 本批次接口、边界与后续接线说明，单词且≤1000行 |

## 第六阶段新增文件

| 路径 | 责任 | 约束 |
|---|---|---|
| engine/schemas/members.py、engine/schemas/ready.py、tests/engine/item/test_members.py、tests/engine/services/test_ready.py | T15 | 请求成员、ready 身份与物理回读验证，单词且≤1000行 |
| engine/item/members.py | T15 | 预检 piece 实体化、局部 registry、合并与完整预算合批，单词且≤1000行 |
| engine/services/ready.py、tests/engine/services/test_preparing.py | T15 | 准备依赖提交、恢复和 prepared.json 最后写入验收，单词且≤1000行 |
| engine/agents/workflow.py、tests/engine/agents/test_workflow.py | T16 | 原子初译、真实译文校对及修订应用，单词且≤1000行 |
| engine/execution/*.py、tests/engine/execution/*.py | T16 | 旧执行器按原职责拆分的单词模块，每个文件≤1000行；__init__.py 为固定包名例外 |
| docs/workflow.md | T15/T16 | 准备、ready 和三步 workflow 交接边界，单词且≤1000行 |

## 第七阶段新增文件

| 路径 | 责任 | 约束 |
|---|---|---|
| engine/services/journal.py、engine/services/custody.py、tests/engine/services/test_journal.py | T17 | 正文结果、证据校验、请求、响应、用量和回放，单词且≤1000行 |
| engine/services/resume.py、tests/engine/services/test_resume.py | T17 | 只读恢复计划及原复合文件改名，单词且≤1000行 |
| tests/engine/services/test_store.py、tests/engine/services/test_contracts.py | T17 | 原存储测试改名，单词且≤1000行 |
| engine/epub/publish.py、engine/epub/verification.py、tests/engine/epub/test_publish.py | T18 | 原字节回填后的候选校验和事务发布，单词且≤1000行 |
| engine/epub/bindings.py、tests/engine/epub/test_bindings.py、tests/engine/epub/test_checker.py、tests/engine/epub/test_publication.py | T18 | 原出版模块拆分及测试改名，单词且≤1000行 |
| engine/execution/atomic.py、tests/engine/execution/test_atomic.py | T19 | 原子 workflow 调度与每批进度接线，单词且≤1000行 |
| tests/engine/test_command.py、tests/engine/services/test_report.py | T19 | CLI 参数/进度及报告回归，单词且≤1000行 |
| docs/publication.md | T17/T18/T19 | 正文恢复、出版、CLI 和验收边界，单词且≤1000行 |

## 真实书籍测试记录

`docs/validation.md` 由 T20 记录已授权测试的原书诊断、测试副本、代码修复、实际请求和恢复证据。提供的小书已完成真实全书出版；大书预检及最终全仓约束验收仍待完成；文档名称为单个单词且≤1000行。

2026-10-07 新增 `engine/services/session.py`、`tests/engine/services/test_session.py`，负责原书任务定位、身份验证和无参数续传；单词命名且≤1000行。

2026-10-07 新增 `engine/epub/diagnostics.py`、`tests/engine/epub/test_diagnostics.py`，负责源书及成品规范诊断的稳定位置比较；单词命名且≤1000行。

2026-10-07 单 JSON 及预检修复新增 `engine/services/state.py`、`engine/services/legacy.py`、`tests/engine/test_compact.py`、`tests/engine/services/test_compact.py`、`tests/engine/services/test_state.py`；分别负责统一持久化、历史任务发现及紧凑目录回归。源码、测试及维护文档均为单词命名且≤1000行。

`tests/engine/services/test_replay.py` 验证缓存响应分批回放、提交失败回滚和零重复HTTP；文件为单词命名且≤1000行。

2026-10-21 新增 `engine/services/tables.py`、`tests/engine/item/test_tables.py`、`tests/engine/services/test_tables.py`，负责超长表格完整行请求、行组包装与父表合并校验、失败预检凭据升级及恢复回归。`tables` 已登记到单词清单；三个文件均不超过 1000 行。

2026-10-22 新增 `engine/epub/cleanup.py`、`tests/engine/epub/test_cleanup.py`，负责出版候选中的空装饰标签及中文普通空白整理，并保护索引、代码、实体、链接及 CSS 布局。`cleanup` 已登记到单词清单；两个文件均不超过 1000 行。

2026-10-24 新增 `engine/epub/preservation.py`、`tests/engine/epub/test_preservation.py`，负责显式授权的原文代码区域清单、源哈希绑定、出版覆盖及逐字节回读验证。`preservation` 已登记到单词清单；两个文件均不超过 1000 行。

2026-10-24 新增 `engine/core/code.py`、`tests/engine/core/test_code.py`、`tests/engine/agents/test_code.py`、`tests/engine/epub/test_code.py`，负责通用代码/CSS 识别、不可改写投影、初译校对攻击回归及自动出版核验。`code` 为已登记单词；各文件均不超过 1000 行。`preservation.py` 同步改为所有书籍自动发现代码，兼容旧显式计划。

2026-10-25 新增 `engine/epub/mathjax.json`，保存官方 MathJax 2.6.1、2.7.9 packed JavaScript 的离线 SHA-256 清单、来源及归档哈希，由 `validation.py` 负责核验。新增 `engine/epub/metadata.py`、`tests/engine/epub/test_metadata.py`，负责基于已有 refinement 恢复空作者、同步 NCX 标识及精确字节回归。`metadata` 为已登记单词；文件均不超过 1000 行，校验清单为生成的紧凑 JSON。
