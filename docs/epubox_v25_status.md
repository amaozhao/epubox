# v2.5 实施状态

分支：`codex/epubox-v25-implementation`。需求依据：[v2.5 设计](epubox_mvp_design_v2_5.md)（SHA256 `d8b9a4dd99608c880a9b0efb148574bc5f948675fc29bcbb702eaafb507ec376`）；范围和验收见[审定计划](epubox_v25_implementation_plan.md)及[T01—T60 矩阵](epubox_v25_test_matrix.md)。设计文档是需求材料，实际执行范围还包括用户后续确认的“只保留一条翻译逻辑”“失败片段不阻塞独立任务”“每个模型源片段不超过 1200 token”和“同一容器内合并连续段落”。

| 范围 | 状态 | 当前证据 |
|---|---|---|
| 旧模式清理 | 完成 | 旧 HTML 翻译链、旧 v2.3 JSON CLI/Store/执行器及独立词表命令已物理删除；`main.py translate` 是唯一整书入口。磁盘格式号仅用于拒绝旧 checkpoint。 |
| P1—P4 准备、术语与规划 | 已实现，术语质量重验中 | 源书检查、完整源视图、默认自动术语、用户规则优先、证据核对、冻结词表、选词、CutPlan 和可恢复 BookPlan 已接通。新版术语提示明确 JSON/范围/源引文契约；完整响应在请求成功前落盘，候选全拒会反馈重试并留存拒绝审计。局部提取缺口按 §7.8 冻结并继续，报告区分零词条和失败覆盖。 |
| 同命令断点续跑 | 已实现 | 重复执行同一 `translate` 命令会按源字节、冻结配置和用户词表选择唯一可信运行；已落盘的成功术语/翻译/校对结果复用，已完成输出经发布哈希验证后幂等返回，丢失输出可用已接受记录重发布。参数变化或多个匹配运行会报错；同源执行锁阻止新版本命令并发发起重复请求。 |
| 翻译、校对、衔接与出版 | 已实现，实书质量待验收 | 单一投影协议、局部失败续跑、显式重试/额度、review-2、章节衔接、原子出版和 EPUBCheck 已接通。模型不生成 XHTML。 |
| 段落复合 Unit | 完成 | 同父连续 `<p>` 按实际源投影尽量合并，下一段会超过 1200 源 token 时才分组；每段仍保留独立 SourceTextView，段落顺序锁定，跨组换行保留；列表、标题及容器边界不跨越。模型整体预算仍可能使 Unit 再切 Segment。 |
| V13 质量验收 | 进行中 | 自动结构/协议验证通过；完整 Leadership 付费翻译、人评及阅读器抽查尚未完成。不能用 identity 装配或模拟模型替代译文质量验收。 |

用户指定的 Leadership EPUB 使用经源 EPUBCheck 修复后的源快照、按正式命令默认的 32768 上下文预算做**无模型**全书结构基准：25 份 DocumentPlan、6785 个主 SourceTextView（与合并前相同）、5777 个 Unit（合并前 6736）、320 个复合 Unit 容纳 1279 段，5045 个可发送 Segment、732 个派生导航 Unit、0 个未规划普通 Unit、0 次 HTTP。组合 Unit 和 Segment 的源投影最高均为 1154 token，低于 1200 的硬上限；BookPlan 状态 `ready`。25 份文档 identity 装配后，EPUBCheck 为 0 fatal、0 error、0 warning；输出 SHA256 `08819aaf629a73f6dcd4834debf317704087de42d0610ff62e263443d0ae10a6`。运行资料位于 `work/v25-benchmark/.../leadership-maximal-same-parent-final/`（已忽略，不纳入提交）。其余段落多数独处于列表项、表格单元格或不同容器，按用户确认的同容器范围不再合并；短 Unit 可由 Batch 合并发送，但仍是各自独立的源位置记录。

`not-just-another-model-ai-development-enterprise.epub` 的源校验暴露本机 EPUBCheck 5.4.0 对导航 `<nav>` ARIA 属性的三处版本差异；同一源书在 5.3.0 下 0 error/0 warning。默认检查器现固定优先选择已验证的内置 5.3.0，不再按错误文本特判回退。该书无模型 P1—P4 基准：32 份 DocumentPlan、5228 个 Unit、4545 个 Segment、最大源片段 1168 token、0 个未规划普通 Unit、0 次 HTTP，BookPlan `ready`。32 份文档 identity 装配后 EPUBCheck 0 error/0 warning；输出 SHA256 `d5f527f1d020838f299f94342a49c0c11d8ebb59ebc5beeb1cad3b3eab8144c8`。运行资料位于 `work/v25-preflight/.../not-just-another-model-preflight/`（已忽略，不纳入提交）。这证明结构处理和发布前校验，不代表该书已有译文。

该书实际模型运行在术语阶段曾以 418/428 成功、10 个失败窗口、469 次 HTTP 暂停；10 个失败窗口均已终结，2 次较早超时留下的 `unknown` attempt 已在同一请求的后续尝试中成功，却被旧的全局 attempt 检查误当作未决，导致重复同命令仍暂停。现以逐窗持久化状态收口，不让历史 `unknown` 封锁已成功或已失败终结的窗口；原 attempt 仍保留在费用/诊断台账中。

修复后以同一工作目录真实续跑，术语 HTTP 保持 469 次且成功进入冻结/P4，证明没有重发 418 个已成功窗口。P4 原进度把 683 个正常派生导航 Unit 计入“局部问题”，现改为已规划成功；该书实际 P4 没有未规划普通 Unit。另发现 418 个成功窗口的候选最终全部被拒绝（主要是缺少必需的源证据或无效 scope_hint），冻结词条为 0。为避免继续无术语约束的整书付费翻译，本次验证进程在正文初始 7 次 HTTP 后已暂停，保留全部运行记录；术语质量处置待明确，不能把这次运行写成整书译文验收。

术语修复试验：同一短技术窗口旧提示为 0 合格/4 拒绝，新提示为 4 合格/0 拒绝且均通过本地源证据检查。`epubox-v25-3` 在真实技术节选的 6 窗 canary 中以 6 次 HTTP 冻结 4 个词条（含 SOX、Basel III、IFRS 9），6 条响应均已落盘；一次候选引用抄错引发的拒绝证据持久化矛盾也已修正，并从 JSON 原样续跑到 ready、没有再次发起模型调用。旧空词表运行保持不变；识别到符合条件的旧运行时，默认停止并提示一次性 `--repair-terms`，明确授权后先写新旧运行关联记录，再创建新的术语修复运行。之后普通同命令可续跑。完整书籍新轮次尚未发起。

该书两段真实摘录此前已按同一复合 Unit 投影协议走完单命令模型翻译：9/9 Unit 接受（其中 1 个 Unit 含 2 个段落）、12 次 HTTP、正式 EPUB 经 EPUBCheck 5.4.0 检查为 0 fatal、0 error、0 warning；输出 SHA256 `68baaf69f2c78097845e51b0f5a505a6b30be0dc885706b38ea1e5446e8313a2`。这是摘录验收，不是整书译文质量结论；`reader_check=not_run`。

最新全库验证：**291 passed**，Pyright **0 errors**，Ruff check/format 通过。段落合并、跨组合尾部保留及表头上下文方向修正经独立代码复核 **APPROVE**。仍需完成实书整本真实模型翻译、逐项语义抽查、阅读器检查和用户人评，才能给出完整质量结论。
