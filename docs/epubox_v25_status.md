# v2.5 实施状态

分支：`codex/epubox-v25-implementation`。主文：[v2.5 设计](epubox_mvp_design_v2_5.md)，SHA256 `d8b9a4dd99608c880a9b0efb148574bc5f948675fc29bcbb702eaafb507ec376`。任务与验收：[审定计划](epubox_v25_implementation_plan.md)、[T01—T60 矩阵](epubox_v25_test_matrix.md)。架构审查与遗漏审查均最终 **APPROVE**。

| 任务 | 当前状态 | 已有证据/下一门槛 |
|---|---|---|
| V00 | 完成 | 起点全库 616 passed；主文/计划提交 `497be82`；新分支建立 |
| V01/V12（旧HTML链） | 物理删除完成 | `generate-glossary`、旧兼容 CLI 参数移除；共享token/装配/质量函数先迁移；旧 orchestrator/workflow/Parser/Builder/DomChunker/GlossaryExtractor 与专属测试已由 `a5414bc` 物理删除，旧专用依赖同步移除 |
| V02 | 完成 | `13ee40a` 定义 -3/request-2/术语契约，独立复核 APPROVE；后续按真实书空槽位与只读上下文扩充契约，`171b017`、`0844f1d` 已提交 |
| V03/V04 | 确定性准备已实现，末轮复核中 | `31428f7` 用户规则、`e564232` Store/P1、`d9ff5b9` 冻结源关系与有界窗口；指定实书无模型 P1→P2：25 DocumentPlan、6736 Unit、6785 主视图、175 术语窗口、G=1110，plan持久化成功，bookplan.json 尚不存在 |
| V05—V07 | 术语调用/冻结/Unit计划代码已实现，端到端集成中 | `2340e18` 严格协议、`ca8be2a` 非failfast提取、`cad73ac` 候选冻结、`818a890` 冲突核对、`6fc9f88` 按Unit选词；模拟传输验证，无真实付费调用 |
| V08—V12 | 唯一执行链已贯通，末轮审查中 | main.py 只调用新准备→正文→出版路径；旧 HTML/旧 JSON CLI、orchestrator、Store 和 review-1 请求入口已物理删除。冻结词表、Batch、review-2、章节检查、原子出版及 JSON 恢复由同一 Store 持久化；显式额度/修订和实际CutPlan升级正在收尾，不宣称全部 V09/V10 已通过 |
| V13 | 自动结构验收进行中，真实语义/人评待完成 | fake 模型从单一命令产出正式 EPUB+report；指定 Leadership 书 6736 Unit、0 unplanned、0 HTTP 的P4 ready，原文 identity 装配后EPUBCheck通过，SHA256 `031192e5af8484b1832dac657632ba339c573aba5f94950adc584639755e0e0b`。真实译文、历史结果对照、人评与阅读器抽查尚未完成 |

当前没有运行真实付费模型调用，也不把 fake 模型测试冒充译文质量验收。最近完整验证为 **226 passed**、Pyright **0 errors**、Ruff check/format **通过**；V09/V10 修订与额度工作仍在联调，V13 逐号矩阵继续核对。磁盘格式号用于显式拒绝旧checkpoint，不代表有多条翻译逻辑。

旧 HTML checkpoint 与旧 v2.3 -1/-2 磁盘记录不混用新协议；V02/V03/V10 将把可定位的 `unsupported-format`、准备期和冻结期恢复处理接入正式 Store/CLI。
