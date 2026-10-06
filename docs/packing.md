# 正文请求投影与预算合批

日期：2026-10-06\
范围：T13、T14\
状态：独立接口已实现；生产接线仍属于 T15/T16/T19。

本阶段把冻结术语、原子源清单和预算合同组合成可发送的正文请求计划。它只做确定性投影与合批，不调用模型、不写断点，也不修改已有翻译。

## 1. 独立接口

[request.py](../engine/item/request.py) 提供：

- `SourceIndex(inventories)`：验证原子清单同源及所有权，保留调用方提供的文档顺序；T15 必须从冻结准备记录的 reading_order 构建此顺序。
- `build_payload(stage, items, glossary, index, ...)`：为一次 translate 或 review 候选集合构造完整模型载荷。

[packing.py](../engine/item/packing.py) 提供：

- `pack_requests(stage, items, glossary, index, limits, ...)`：按阅读顺序贪心构造 RequestBatch。
- `PackingResult`：返回 batches、boundaries、blocked 和 skipped；`ready` 仅表示本次规划没有单项阻断。

调用方必须提供冻结词表、原子清单及当前阶段的断点状态。函数不会读取或写入运行目录。

## 2. 请求投影

一次候选请求只能包含同一文档、同一内容通道、按阅读顺序排列的完整原子项。每项发送：

- item_id 与完整 source projection。
- 当前目标或可见前文实际相关的词条。
- 本项结构校验所需的标记提示与约束；g 范围的正文已在 source 中，不重复附带 excerpt，保护对象仅附带有界只读说明。
- review 阶段的当前已保存 target、base_revision、适用检查和绑定对照。

词条保留 source、target、aliases、mode、match_policy 和完整 note。`target` 角色适用于当前项；`context` 角色只说明前文用词，不能提升为当前项的硬约束。

载荷不包含 XPath、源哈希、节点定位、祖先结构或本地证据清单。初译要求模型只返回 item_id 和完整 target；review 只针对调用方传入的当前目标，不猜测未来译文。

## 3. 共享上下文

上下文由整个候选批次共享一次：

- 只取同一文档、同一内容通道中位于批次之前的源原子项。
- 排除候选批次内的全部成员。
- 最多两个片段，每个取联合源文本的末尾最多 400 字符。
- 保持源阅读顺序；零个或一个前文同样合法。

上下文只出现在载荷根部，不复制到每个 item。预算不合适时，T14 依次去掉较早上下文，再缩小候选批次。

## 4. 顺序预算合批

`pack_requests` 对每个待处理完整原子项执行以下规则：

1. 调用 T13 为当前候选集合重建完整载荷。
2. 调用 T01 对该载荷计量 S/I/R/M。
3. 预算通过则继续尝试加入下一个相邻原子项。
4. 超限则结束当前批次，并从该完整原子项开始下一批。
5. 单个原子仍超限时返回 BlockedItem，不切开 p、table、em、i、ul 或 ol。

资源变化、内容通道变化、序号不相邻、阶段完成项或预算边界都会关闭当前批次。若标题与后续首段可同批，而加入当前批次后无法一起容纳，会在标题前结束当前批次，保留标题与后续完整项的关联。没有固定 8 项上限，也没有固定 1200-token 正文上限；实际限制来自冻结的 BudgetLimits。

translate 和 review 可形成不同分组。review 必须使用真实已保存目标及 revision 重新构造、重新计量；如果单项 review 超限，结果只报告阻断，调用方保存的初译不被修改。

## 5. 身份与恢复边界

每个 RequestBatch 清单绑定：

- stage、source_hash、freeze_id 和冻结词表文件身份。
- 有序 item_ids、逐项输入身份和完整 wire_hash。
- record version、plan epoch、revision 与 review target hash。
- 逐项 term IDs/term hash 和整批 context hash。

阶段完成 ID 必须由调用方从可信断点传入。它们进入 skipped，不会被重新合批覆盖。输入中的每个原子 ID 必须恰好落入 batches、blocked 或 skipped 之一。

## 6. 尚未接线的范围

T15 负责让生产准备管线读取冻结词表和原子清单、调用 `pack_requests` 并持久化最终 ready 计划。T16 负责按 RequestBatch 执行 translate、保存初译、用真实目标重新规划 review，再应用有效修订。接线时须用版本明确的提示解释 target/context 术语角色；context 模式为 required 时仍只读，不改变当前项硬约束，不能直接修改旧提示而破坏历史 wire_hash。T17 负责阶段断点与回放，T19 负责 CLI 和进度日志。

在这些任务完成前，T13/T14 是经过固定夹具调用的独立规划能力，不能据此宣称 CLI 已使用新正文路径，也不能宣称最终中文 EPUB 已生成。

## 7. 虚拟分段的后续交接

T08 允许在已验证切点把过大的普通虚拟块规划为 PreflightPiece；T14 本阶段只接受 SourceIndex 中的完整 AtomicItem。前者通过预检不意味着原始大块可以直接通过 T14。

T15 接线前须明确虚拟 piece 的请求成员及保存合同：记录原父单元、稳定 piece ID、局部 registry、真实源范围和顺序，证明全部 piece 无漏重并能按原父单元合并回填。不能把 PreflightPiece 强制伪装为原始 AtomicItem，不能改写 p/table/list 等硬原子。此合同同时约束 T16 的逐阶段保存入口和 T17 的恢复实现。

## 8. 本阶段验收

基线 feature/preflight@697bd14。全量 `pytest -q`：**572 passed（51.14 秒）**；Ruff、129 个 Python 文件的格式、Pyright（0 errors/0 warnings）、阶段文件约束和 diff 检查通过。

独立代码审查为 APPROVE；架构审查为 WATCH，唯一待接线项是第 7 节已登记的虚拟分段成员合同。测试使用固定源文和已保存目标夹具，没有真实模型请求。
