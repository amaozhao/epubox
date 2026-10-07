# 原子准备与三步 workflow

状态：T15/T16 已实施；本文记录新准备路径、ready 身份和正文三步接口。逐阶段持久化、最终 EPUB 出版和 CLI 贯通分别属于 T17、T18 和 T19。

## 1. 准备顺序

`prepare_translation()` 和 `resume_preparation()` 的新默认库接口使用 `epubox-term-planner-3`（原子 extractor 为 `epubox-atomic-1`）：

```text
固定 source.epub 与 preparation.json
→ 从原字节资源重建 AtomicDocument
→ 原子容量预检与恒等回填验证
→ 用户术语、自动候选、冲突决定与冻结词表
→ 将通过预检的完整原子或虚拟 piece 实体化为 RequestMember
→ 按资源、通道、顺序和完整 token 预算生成 MemberBatch
→ 写入并回读所有依赖
→ 最后写入 prepared.json
```

原子预检失败时停在 `preflight`，不进入术语或正文模型请求。术语暂停保留已有请求、响应和计量记录。`closed_with_gaps` 可以产生 ready，但结果明确标为 `needs_attention`。

显式选择 strategy v2 时仍走旧 `bookplan.json` 路径。旧计划不会被重解释为原子计划，也不会在恢复时被暗中改写。当前 CLI 仍显式使用 strategy v2，切换完整新链路由 T19 完成。

## 2. RequestMember 与虚拟 piece

`RequestMember` 是正文请求的最小不可再拆单位，包含：

- 稳定的 member ID、父原子 ID 和父原子哈希。
- 文档、Unit、内容通道、顺序及原字节范围。
- 完整 source projection 或仅属于该 piece 的 projection。
- 完整原子 registry 或由父 registry 派生的局部 registry。
- 预检身份、piece 索引和兄弟总数。

`p/table/em/i/ul/ol` 等硬原子不能产生多个 member。只有 T05 已登记安全切点的普通虚拟文本块可以生成 piece。piece 必须连续、无缺口、无重复，串接 source projection 后必须精确还原父原子。

翻译结果只能与同一父原子的全部兄弟 piece 合并。合并前逐个验证局部 registry，合并后再使用父原子的完整 registry 验证。不允许用局部通过代替父项完整性。

Unit 仍是来源所有权和最终回填的父边界。`AtomicPlan.unit_members` 记录 Unit 到 member 的完整映射，`derived_sources` 记录导航派生来源。这些字段为 T18 出版保留来源证据，本阶段不生成最终 EPUB。

## 3. ready 提交与回读

`AtomicPreparedInput` 绑定同一 source/run 下的：

- `PreparationPlan` 及其翻译配置。
- `GlossarySnapshot`、freeze ID 和 freeze 文件哈希。
- `PreflightCheck` 及原子、映射、模型和预算身份。
- inventory、member、batch 和 Unit 所有权的完整哈希清单。
- 导航派生关系和输出策略身份。

`write_ready()` 先写入 `plans/book.json`、`members/*.json`、`batches/*.json` 和初始 `results/*.json`，然后通过公开读取路径验证文件集没有缺失、额外项或身份变化。只有全部回读成功后才写入 `prepared.json`。

`ReadySession` 在 workflow 开始及每次模型请求前重新核对 ready 依赖。修改 source、词表、预检、member、batch、准备配置或文件清单都会拒绝继续请求。已有依赖可重复回读，不会因恢复而重新运行术语模型请求。

## 4. MemberBatch 与提示协议

`MemberBatch` 使用 `epubox-batch-2`，payload 使用 `epubox-members-1`。批次只合并同资源、同内容通道且顺序相邻的 member，整批共享最多两段前文。每次加入成员都重建完整请求并检查源文、输入、输出和模型上下文 token 限制。

原子提示语明确区分 target/context 术语角色，并要求响应只返回 item ID 与完整 target。它使用独立版本，旧 `epubox-text-1` wire 和旧提示内容保持不变，因此旧请求哈希不会被新语义重写。

## 5. 三步 workflow

`run_workflow()` 是唯一公开的原子模型执行入口；三个内部步骤不能作为独立派发 API。它对一个已提交的 translate batch 执行：

1. 内部初译步骤：复用身份完整且通过本地验证的已保存初译；其余成员调用模型，验证结构、ID、占位符、术语和未译文本后产生当前稿。
2. 内部校对步骤：使用当前已保存 target 重新构造 review payload 和 token 预算。初译批次不能放入同样大小的 review 时，只在 member 之间重新合批。
3. 内部应用修订步骤：`no_change` 接受当前稿；`replace` 只在新 target 通过完整本地验证时替换。缺项、截断、未知 ID 或无效替换保留当前译文并进入 `needs_attention`。

每个初译和每个校对结果都立即调用调用方传入的 `save(ItemRecord)`。同步和异步回调都可用。回调是 T16 的保存边界；T17 负责将它连接到持久化、响应回放和重启阶段判定。本阶段不在 workflow 内新建另一套存储器。

## 6. 本阶段停止边界

- 不发布 EPUB，不更改最终 `-cn.epub` 文件；该工作属于 T18。
- 不将 `save` 回调冒充为已持久化的断点；实际落盘与回放属于 T17。
- 不将库内新默认误报为 CLI 已切换；CLI 完整贯通属于 T19。
- 本阶段的请求门禁是源身份、ready 身份和 token 容量校验，不是费用或支付逻辑。

## 7. 验收记录

基线 feature/preflight@48de8ab。全量 `pytest -q`：**616 passed（59.70 秒）**；Ruff、154 个 Python 文件的格式、Pyright（0 errors/0 warnings）、T00—T16 文件约束和 diff 检查通过。独立代码审查 APPROVE，架构审查 CLEAR。

验证覆盖真实临时 EPUB 准备到 ready、虚拟分段物化与合并、无请求恢复、提交中断、依赖与规范请求篡改、输出策略变更、完整预算和每次重试校验，以及初译保存、真实当前稿校对、有效修订应用、无效结果保留和部分批次续跑。模型使用假 transport，没有真实接口请求。

## 8. 正文请求失败的继续策略

2026-10-07 按用户要求更新：初译和校对的接口错误，包括超时、未知响应结果、鉴权及服务异常，只将当前请求涉及的成员记录为 `needs_attention`，保留已有初译与错误证据，并继续后续独立批次。每个请求仍遵守有限重试；未落盘的未知响应不能在同一轮盲目重发。连续接口错误不再触发正文的整书暂停。

普通 `translate` 命令再次运行时，沿用已保存进度，按既有自动恢复逻辑重试待处理项；已接受内容不重翻。源身份、持久化、输入容量、用户取消及显式运行限额仍保持原来的保护边界。失败项未修复时不会发布伪装完整的中文 EPUB。此处覆盖历史设计文档中正文接口错误触发全局暂停的旧规则，术语及其他阶段的策略保持原样。

本次验收：全量测试 792 passed（177.79 秒），Ruff/格式检查及 Pyright 通过；独立代码审查 APPROVE、架构审查 CLEAR。覆盖连续三批初译/校对超时后继续处理、未知请求不重复派发及普通命令恢复失败校对。真实书籍断点只读验证保留 106 个已接受单元、198 次累计 HTTP；本次未发送真实翻译请求。
