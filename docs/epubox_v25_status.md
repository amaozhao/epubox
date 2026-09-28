# v2.5 实施状态

分支：`codex/epubox-v25-implementation`。主文：[v2.5 设计](epubox_mvp_design_v2_5.md)，SHA256 `d8b9a4dd99608c880a9b0efb148574bc5f948675fc29bcbb702eaafb507ec376`。任务与验收：[审定计划](epubox_v25_implementation_plan.md)、[T01—T60 矩阵](epubox_v25_test_matrix.md)。架构审查与遗漏审查均最终 **APPROVE**。

| 任务 | 当前状态 | 已有证据/下一门槛 |
|---|---|---|
| V00 | 完成 | 起点全库 616 passed；主文/计划提交 `497be82`；新分支建立 |
| V01 | 公开入口与共享能力完成，物理删除待 V12 | `generate-glossary`、旧兼容 CLI 参数移除；token/装配/质量函数已迁中性模块，提交 `9e6c264`；装配与质量迁移独立 review APPROVE。旧 orchestrator/workflow/Parser/Builder 仍在仓库，尚不能写“只剩新代码” |
| V02 | 完成 | `13ee40a` 定义 -3/request-2/术语契约，独立复核 APPROVE；后续按真实书空槽位与只读上下文扩充契约，`171b017`、`0844f1d` 已提交 |
| V03 | Store/用户输入已实现，独立复核中 | 标准化用户规则 `31428f7`；新 Store P1/P2/P3 协议无 building BookPlan，发现的计划漏覆盖、冻结后篡改等反例已修；待最新复核后提交 |
| V04 | P1 源解析与术语窗口已实现，关系/身份边界复核中 | `198afb6` 源视图重放、`171b017` v2.5 适配；真实书无模型 P1：25 DocumentPlan、6736 Unit、6785 SourceTextView，bookplan.json 不存在。长视图窗口、阅读关系和上下文来源正在收紧，未宣称整个 V04 完成 |
| V05—V13 | 按依赖推进 | `2340e18` 严格术语协议、`5b56e1b` 同一计费入口；提取 runner 和候选/冻结仍在验证，不跳过真实模型/阅读/人评证据门槛 |

当前不运行真实 v2.5 术语模型调用，也不把 v2.3 的无词表运行冒充新流程。最近一次 v2.5 定向回归 **84 passed**，Pyright **0 errors**；独立审查仍在处理术语上下文/范围/配置身份反例。旧 `engine/item/chunker.py` 有 26 项原有 Ruff 债务，V12 物理删除后再核对全仓；不为清理临时旧文件改写旧行为。

旧 HTML checkpoint 与旧 v2.3 -1/-2 磁盘记录不混用新协议；V02/V03/V10 将把可定位的 `unsupported-format`、准备期和冻结期恢复处理接入正式 Store/CLI。
