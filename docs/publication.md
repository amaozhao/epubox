# 正文恢复、出版与命令行交接

日期：2026-10-06\
范围：T17—T19 的正文日志、原子 EPUB 出版和现有 CLI 接线。\
前置接口：[workflow.md](workflow.md)。全链路真实验收仍属于 T20。

## 1. 正文阶段日志

`BodyJournal` 接入 T16 已有 workflow 的 `save` 回调，没有建立第二套翻译执行器。它以物理回读通过的 `ReadySession` 为身份根，只接受当前 `prepared.json`、member 清单和冻结配置拥有的记录。

每个 member 的当前结果继续保存在 `results/<item_id>.json`。保存时会重新核对 item、来源范围、术语、上下文和 ready 身份；初译可以推进到校对结果，已经终态的 reviewed/needs_attention 记录不能被旧稿回滚。重复写入完全相同的记录是幂等操作。

正文请求继续使用已有 `requests/*.json`、attempt 和 `responses/<stage>/<request_id>/<attempt_id>.json` 格式：

1. 请求清单和预留 attempt 在模型调用前保存。
2. 模型响应及实际 usage 在 attempt 完成状态前保存。
3. 重启时先按相同 request/wire 身份回放已保存响应，再决定是否调用模型。
4. 已完成初译从保存的当前稿继续校对；已完成校对不重新请求。
5. 累计 HTTP 次数和 token 用量来自保存的 attempts，不因恢复而清零。

运行时只从 ready 计划读取冻结的模型、并发、输入/输出限制和运行请求上限。传入的模型身份与冻结值不一致时拒绝继续；来源、原子归属、词表、提示或 ready 身份变化也不能静默复用正文结果。当前实现不提供旧正文 JSON 的通用转换器。

## 2. 完整父项与派生导航

虚拟 piece 只用于模型请求和逐阶段保存。出版前，`parent_targets(require_complete=True)` 要求一个原始父项的所有兄弟 member 都是 reviewed，再按稳定 piece 顺序合并并重新验证父项完整标记清单。

派生导航不增加模型请求。它只在来源标题父项已经形成合法完整目标后，使用既有导航投影规则生成目标。缺少兄弟、未完成校对、父项归属不一致或派生来源不完整都会阻止出版。

## 3. 原子 EPUB 出版

`publish_atomic(store, output_path, checker, overwrite=False)` 保持既有字典返回形状：

```text
{
  "path": 最终路径,
  "sha256": 最终文件哈希,
  "verification": 校验记录,
  "publish": 发布事务记录
}
```

它不引入新的 PublishedBook schema。流程如下：

1. 回读 ready、全部 reviewed parent target 和不可变 `source.epub`。
2. 要求目标集合与原始 inventory 完全一致，不允许漏项或额外项。
3. 通过 `fill_resource` 按已验证的原字节区间回填正文、属性和导航目标。
4. 只对白名单语言字段做字节局部更新：XHTML 根元素的 `lang`/`xml:lang`，以及 OPF 主 `dc:language`。
5. 从源快照建立候选 EPUB；图片、CSS、JS、字体及其他未替换资源复制原始内容。
6. 校验资源清单、EPUB 版本、OPF 路径、spine、字体混淆、文档结构、内部引用、语言字段、目标证据和 EPUBCheck 结果。
7. 再次核对源哈希和正文结果没有变化，然后写发布意图并原子提交到目标路径。

默认目标位于原 EPUB 旁，名称为 `<stem>-cn.epub`。源快照、准备记录或它们的硬链接/别名不能作为输出；已有目标只有显式 `overwrite` 才能替换。候选校验失败时不会覆盖旧成品。`publish.json` 绑定计划指纹、每个目标的版本向量、目标路径和目标哈希，因此进程在提交附近中断后可以核对并恢复同一次发布。

## 4. CLI 接线

现有三个命令保持不变：

- `translate`：准备、正文初译、校对、修订、校验和出版。
- `resume`：只使用工作目录内冻结的源快照和配置继续。
- `plan-resume`：只读判断下一步，不发模型请求，也不修改记录。

新任务使用原子 strategy、输入预算 v2 和输出预算 v3。输出预算 v3 完整预留配置的 provider 输出额度，并为完整响应估算加入分词差异余量；估算超过额度时在 HTTP 前拒绝请求。既有输出预算 v2 的身份保持不变，未完成的真实接口任务暂停，不静默改写预算或重跑。`--limit` 表示单个原子源片段的最大 token 数；显式参数优先于 `EPUB_CHUNK_MAX_TOKENS`，两者都没有时使用已定义默认值。该值与模型上下文、最大输入、最大输出及 HTTP 次数是不同限制。任务建立后，恢复继续使用落盘的 `max_source_tokens`，不会因后来修改环境变量而重切已完成内容。

重复执行同一本书会先查找匹配的冻结任务。有效 `prepared.json` 进入正文恢复；有效 `publish.json` 和成品哈希匹配时直接返回已完成结果。默认工作资料仍位于原书旁的同名目录，最终文件仍为原名加 `-cn.epub`。

准备阶段立即报告阶段计数。正文每批报告 request ID、结果状态、完成单元、初译/校对项、待处理项、耗时、正文估算、输入估算、输入/输出预留、实际累计 token 和 HTTP 次数。估算、预留和服务端实际 usage 是不同指标。

## 5. 当前验证边界

T17—T19 的单元和集成测试使用假模型响应，出版测试中的 `StubChecker` 只证明 checker 接口被调用并允许测试继续。它们验证保存顺序、身份拒绝、幂等回放、原字节回填、资源保护、失败不覆盖和 CLI 接线，但不能证明中文语义质量，也不能代替真实 EPUBCheck。

T20 仍需完成：全量工程检查、真实 EPUBCheck、小书真实三步翻译/恢复/出版、大书只读预检和最终文件约束复核。测试不得使用或修改现有用户书籍及其工作目录。

最终本地验证：全量 pytest **664 passed（126.64 秒）**；Ruff、格式、Pyright（0 errors/0 warnings）、本阶段单词命名/≤1000 行与 diff 检查通过。独立代码审查 **APPROVE（0 issues）**，架构审查 **CLEAR**。详细恢复反例见 [review.md](review.md)。
