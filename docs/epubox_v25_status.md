# v2.5 实施状态

分支：`codex/epubox-v25-implementation`。主文：[v2.5 设计](epubox_mvp_design_v2_5.md)，SHA256 `d8b9a4dd99608c880a9b0efb148574bc5f948675fc29bcbb702eaafb507ec376`。任务与验收：[审定计划](epubox_v25_implementation_plan.md)、[T01—T60 矩阵](epubox_v25_test_matrix.md)。架构审查与遗漏审查均最终 **APPROVE**。

| 任务 | 当前状态 | 已有证据/下一门槛 |
|---|---|---|
| V00 | 完成 | 起点全库 616 passed；主文/计划提交 `497be82`；新分支建立 |
| V01 | 公开入口与共享能力完成，物理删除待 V12 | `generate-glossary`、旧兼容 CLI 参数移除；token/装配/质量函数已迁中性模块，提交 `9e6c264`；装配与质量迁移独立 review APPROVE。旧 orchestrator/workflow/Parser/Builder 仍在仓库，尚不能写“只剩新代码” |
| V02 | 契约实现与独立复核进行中 | `engine/schemas/v25.py`、`tests/v25/test_contracts.py` 未提交；review 已找出冻结/哈希/来源身份反例，修复及复核未结束 |
| V03—V13 | 待按依赖实施 | 以审定计划和逐号矩阵为准；不跳过真实模型/阅读/人评证据门槛 |

当前不运行 v2.5 自动术语调用，也不把 v2.3 的无词表运行冒充新流程。V01 验证：`uv run pytest -q --ignore=tests/v25/test_contracts.py` 为 **623 passed**，Pyright **0 errors**；本轮新代码 Ruff/format 与差异空白检查通过。旧 `engine/item/chunker.py` 有 26 项原有 Ruff 债务，V12 物理删除后再核对全仓；不为清理临时旧文件改写旧行为。

旧 HTML checkpoint 与旧 v2.3 -1/-2 磁盘记录不混用新协议；V02/V03/V10 将把可定位的 `unsupported-format`、准备期和冻结期恢复处理接入正式 Store/CLI。
