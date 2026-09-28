# EPUBox v2.3 架构与任务计划审查记录

日期：2026-09-28。状态：**两路独立 review 均 APPROVE；等待用户确认实施。**

本记录审查的是设计及任务拆分，不是对尚未实现的新引擎作验收保证。在本次需求与可执行性审查范围内，没有发现未解决的阻断性错漏。实现仍须执行需求 T01–T32、真实模型与阅读器验收。

## 1. 受审版本

| 对象 | 路径/版本 | SHA-256 |
|---|---|---|
| 唯一需求文档 | `docs/epubox_redesign.md`，v2.3，860 行 | `bbd1aed9a313f34d5af23514253b48218a8552253f727e4219e0fd3ee084f2b0` |
| 架构与任务计划 | `docs/epubox_architecture_plan.md`，417 行，P00–P14 | `1214ccf216fa06f50c0a759ea959f229c8e6ff7b3a2b9382c23f338f29fad2f7` |
| 源码 | HEAD `13ece3a` | 业务代码与受审基线无 diff |
| 分支 | `codex/translation-architecture-redesign` | 沿用上一轮分支，没有提交/推送 |

两位 reviewer 均只读且与计划主作者分离，完整阅读新版需求与计划后独立给出结论。它们针对同一计划哈希审批；此后主作者没有修改受审计划。

## 2. 独立结论

### 2.1 架构与可执行性：APPROVE

Reviewer：`review_plan`（critic）。

复核重点及结论：

- A–E 阶段各有可运行闭环，P00–P14 依赖无环；普通单元闭环先于长单元完善，不提前宣称 MVP 完成。
- DocumentPlan/初始 UnitRecord 落盘后才提交 ready；派发前保存 RequestManifest、attempt 与 Unit 预留。
- `record_version`、`revision`、`plan_epoch` 分工明确，同代兄弟 Segment 按 delta 合并，不覆盖正常结果。
- attempt/累计额度可以保守对账；重规划和修订不清零，局部额度不停止全书。
- 已知局部失败隔离、全局错误传播、衔接 blocked_dependency 与公平重试均有执行及验收落点。
- publish 意图、目标锁、已验证临时文件、替换后崩溃补记构成完整发布协议。

原结论：未发现架构或执行层面的阻断；仍需用户确认后才能实施。

### 2.2 需求符合性与遗漏：APPROVE

Reviewer：`review_v23_coverage`（verifier）。

按全部需求章节及 T01–T32 核对，确认：

- **强制 HTML→JSON**：每份 HTML/XHTML 的 source_markup、节点、槽位、Unit、g/x/b、边界、属性定位先完整落盘；跨进程从 JSON 恢复，不靠内存 DOM。
- **强制局部失败继续**：同章、跨章、长 Unit 剩余片段和其他批次继续；成功/失败逐项保存；依赖不占 worker，完整分母不缩小。
- **MVP 禁止项**：未引入快速/草稿/跳过校对、动态译文依赖图、配置增量缓存、CSS 引擎、术语平台、字体剪枝系统或数据库迁移。
- **协议与格式**：g/x/b、父域/硬边界、严格 wire 身份、字符簇/原子切片、虚拟边界与整 Unit 重规划都有对应任务。
- **质量与发布**：五项完整校对、版本接受条件、付费前规范预检、独立目标文字/结构核对、唯一 completed 和发布恢复均落实。
- **修复与恢复**：人工完整目标导入、固定额度及显式追加、实际 attempt 计数、全清单补洞和损坏隔离均覆盖。

原结论：无阻断遗漏或冲突；没有发现计划弱化需求或影响完成条件的未覆盖约束。

## 3. 机械检查与源码基线

主作者对最终受审版本执行了只读检查；reviewer 另行核对编号与依赖：

| 检查 | 结果 |
|---|---|
| P00–P14 任务主表 | 15 项，连续且无重复 |
| 任务依赖 | 所有引用存在、无环 |
| T01–T32 主责验收表 | 32 项，连续且各有一个主责任务 |
| 正文需求覆盖表 | §0–§16 均有任务落点；不是只写验收编号 |
| 代码证据路径/行号 | 21 个显式引用存在且在文件范围内 |
| Markdown 代码围栏 | 成对闭合 |
| 源需求文件 | 哈希保持不变，未修改用户文档 |
| 当前源码测试 | `435 passed in 1.55s` |
| 类型检查 | `0 errors, 0 warnings` |
| Ruff / 格式 | 239 项既存 lint 问题；5 文件需格式化、51 已符合；未冒充全绿 |

基线命令为 `.venv/bin/python -m pytest -q`、`.venv/bin/pyright`、`.venv/bin/ruff check --config pyproject.toml main.py engine tests --statistics`、`.venv/bin/ruff format --config pyproject.toml --check main.py engine tests --output-format concise`。

这些是旧源码当前基线。T01–T32 是新引擎实施后验收，**本轮没有声称它们已经通过**。

## 4. 源文档的两处非阻断差异

1. 需求第 787 行称基准文件为 `epubox_mvp_refactoring_v2_3.md`，实际用户提供文件为 `docs/epubox_redesign.md`。计划使用实际路径、版本与完整哈希锁定基准，不臆造另一份需求。
2. 需求第 532 行引用 `examples/document.json`，当前仓库未提供。计划明确以 §10.2 完整字段表为准，P00/P01 建立自己的合成契约 fixture；不把不存在的示例或探针当作项目实测。

两点均不要求扩大实施范围，也未修改原需求文件。

## 5. 仍需实施阶段提供的证据

完整授权书样、真实 provider 容量/费用预算与响应、EPUBCheck 环境、人工源译抽查及实际目标阅读器仍按 P00/P06/P14 落实。它们影响真实调用或默认切换的验收，不阻止本次架构与任务计划完成。

本轮只更新计划并新增此审查记录，没有业务代码、测试实现或依赖修改，也未调用真实模型。**review 通过不替代用户对任务拆分的确认；确认后才从 P00 开始。**
