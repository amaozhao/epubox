# v2.5 实施状态

分支：`codex/epubox-v25-implementation`。需求依据：[v2.5 设计](epubox_mvp_design_v2_5.md)（SHA256 `d8b9a4dd99608c880a9b0efb148574bc5f948675fc29bcbb702eaafb507ec376`）；范围和验收见[审定计划](epubox_v25_implementation_plan.md)及[T01—T60 矩阵](epubox_v25_test_matrix.md)。设计文档是需求材料，实际执行范围还包括用户后续确认的“只保留一条翻译逻辑”“失败片段不阻塞独立任务”“每个模型源片段不超过 1200 token”和“同一容器内合并连续段落”。

| 范围 | 状态 | 当前证据 |
|---|---|---|
| 旧模式清理 | 完成 | 旧 HTML 翻译链、旧 v2.3 JSON CLI/Store/执行器及独立词表命令已物理删除；`main.py translate` 是唯一整书入口。磁盘格式号仅用于拒绝旧 checkpoint。 |
| P1—P4 准备、术语与规划 | 已实现 | 源书检查、完整源视图、默认自动术语、用户规则优先、证据核对、冻结词表、选词、CutPlan 和可恢复 BookPlan 已接通。自动术语可在确定性基准中显式关闭。 |
| 翻译、校对、衔接与出版 | 已实现，实书质量待验收 | 单一投影协议、局部失败续跑、显式重试/额度、review-2、章节衔接、原子出版和 EPUBCheck 已接通。模型不生成 XHTML。 |
| 段落复合 Unit | 完成 | 同父连续 `<p>` 最多 8 段、约 700 token 可见源文合为真实 Unit；每段仍保留独立 SourceTextView，段落顺序锁定；列表、标题及容器边界不跨越。过长 Unit 可再切 Segment；发送前源投影硬限制 1200 token。 |
| V13 质量验收 | 进行中 | 自动结构/协议验证通过；完整 Leadership 付费翻译、人评及阅读器抽查尚未完成。不能用 identity 装配或模拟模型替代译文质量验收。 |

用户指定的 Leadership EPUB 使用经源 EPUBCheck 修复后的源快照做**无模型**全书结构基准：25 份 DocumentPlan、6785 个主 SourceTextView（与合并前相同）、5800 个 Unit（合并前 6736）、337 个复合 Unit 容纳 1273 段，5091 个可发送 Segment、732 个派生导航 Unit、0 个未规划普通 Unit、0 次 HTTP。所有 Segment 源投影最高 507 token，低于 1200 的硬上限；BookPlan 状态 `ready`。25 份文档 identity 装配后，EPUBCheck 为 0 fatal、0 error、0 warning；输出 SHA256 `08819aaf629a73f6dcd4834debf317704087de42d0610ff62e263443d0ae10a6`。运行资料位于 `work/v25-benchmark/.../leadership-composite-final/`（已忽略，不纳入提交）。

该书两段真实摘录已按**最新复合 Unit** 走完单命令模型翻译：9/9 Unit 接受（其中 1 个 Unit 含 2 个段落）、12 次 HTTP、正式 EPUB 经 EPUBCheck 5.4.0 检查为 0 fatal、0 error、0 warning；输出 SHA256 `68baaf69f2c78097845e51b0f5a505a6b30be0dc885706b38ea1e5446e8313a2`。这是摘录验收，不是整书译文质量结论；`reader_check=not_run`。

最新全库验证：**269 passed**，Pyright **0 errors**，Ruff check/format 通过。段落合并及表头上下文方向修正经独立代码复核 **APPROVE**。仍需完成 Leadership 整书真实模型翻译、逐项语义抽查、阅读器检查和用户人评，才能给出完整质量结论。
