# P14 后续验收记录

日期：2026-09-28。此文件承接 `epubox_implementation_status.md`，记录真实检查及其边界。P14仍在进行，不以模型替身、人评缺失或单次成功推断版本可切默认。

## 阅读器阻塞曾解除及已有观察

本轮早些时候，Calibre ebook-viewer 9.15.0 可通过原生UI工具观察。对已有原创小样本 `real-model-zh-v2.epub` 实际查看了正文和目录：

- 书名“可靠的系统”、标题“安全恢复”和中文正文显示正常，无可见缺字或裁切。
- `finally` 显示为等宽代码，“资源”保留斜体。
- 中文目录“第 1 章”可见并可点击；因只有一章且原先已在该章，不将此记为多章跳转证据。
- 证据来自真实窗口的无障碍树与截图，记录为 `work/v23-acceptance/p14/reader-small-sample.json`。尚不覆盖实书、表格或脚注。

这当时关闭了“Mac锁屏无法观察”的旧阻塞，不代表实书阅读或人工语义评分已经通过。技术译本完成后再次尝试时锁屏复现，见后文。

## 用户人评与实际提示修订

用户对第一道匿名对照明确选择 **A可接受、B有问题**。原句和原始结果保存在 `epubox_blind_review.md`；未虚构分项评分或用户理由。

原提示仅泛称accuracy/fluency，未明确中文搭配与修饰关系。新增通用关系要求后，独立审查指出试验版v3错误要求所有谓词带宾语；已改为条件式论元约束并明确不补造参与者。当前提示版本是 `epubox-v23-4`，审查 **APPROVE**。v3已经发过请求，因此v4另建运行，未混用旧accepted结果。

没有硬编码反馈句、替换词表、额外模型路由或自动质量档位。提示变化不等于质量修复已得到证明。

## 真实语义探针（非整书质量结论）

两个独立运行使用同一 Agnes 3.0 Flash、context 32768 / output 1024；每个HTTP均通过正式runtime计数并记录请求。

| 固定案例 | v3实际结果 | v4实际结果 |
|---|---|---|
| 新翻译“失败任务后继续独立工作” | 生成后no_change | 生成后要求replace，理由为自然度 |
| 用户拒绝的旧B | fluency=fail，replace | **no_change：漏掉用户指出的问题** |
| 用户接受的旧A（正向对照） | no_change | **fluency=fail，要求replace：与用户判断存在分歧** |
| 否定/条件反转 | accuracy=fail/critical，replace | accuracy=fail/critical，replace |
| 结构合法但链接绑到错误词语 | 探针脚本错误，未请求 | bindings=fail，替换目标恢复正确绑定；解释文字有倒置，不能视为可靠原因分析 |

v3共5次HTTP；脚本最后错误使用HTML形式查找g标记，实际投影使用`⟦+g1⟧`，所以未执行链接案例。修正脚本后v4共6次HTTP，全部案例执行。v3不是完整通过，v4不是自然度修复成功。单次样本不构成统计成功率。

原始响应、请求、计数保存在：

- `work/v23-acceptance/p14/semantic-probe-result-v3.json`
- `work/v23-acceptance/p14/semantic-probe-result.json`
- 两文件所指向的独立运行目录。

新版生成句与另一条代码/斜体句已发起后续人工复评，未收到的答案保持待评。

## 实书与源规范

已核验的公开实书为 Edgar Allan Poe《The Cask of Amontillado》：

- [Project Gutenberg永久来源页](https://www.gutenberg.org/ebooks/1063)，书目注明“Public domain in the USA”。本次仅本地测试，不发布译本。
- 原始EPUB2下载地址：`https://www.gutenberg.org/ebooks/1063.epub.noimages`。
- 原包SHA256：`4e4e64ae9b36a40c5476737b3a585fb7eaf9a596ed7867b10fd3ff40fecd3327`。
- 源EPUBCheck 5.4.0：0 fatal / 0 error / 0 warning；未修改原包。
- 正文约2329英文词；完整包还包含Gutenberg说明。共4个DocumentPlan、160个Unit，零规划失败。
- 正式运行：prompt v4、context 32768 / output 4096、并发2、RPM10、运行HTTP上限120。此配置与1024上限的两句对照不同，不作同配置成本比较。
- 运行ID：`3ad279aa8e8a4f9388e52304c0b71387`。原包、准备信息、请求记录及结果存于 `work/v23-acceptance/p14/`。

首轮自动运行在94次HTTP后为 `needs_attention`：125/160达到机器门槛，34项局部失败、1项派生依赖未齐，未输出EPUB。随后31份代理修订及3项协议重试经过正式入口继续；追加200次运行额度至累计320，原计数保留。又对剩余6项、开篇1项做有理由的完整修订，累计129次HTTP后逐单元曾达到160/160。此处“代理修订”不算用户人评，也不能计入自动完成率。

衔接检查随后标记书名和作者相邻单元：书名得到修订，但作者单元重审no_change后，旧unit-level `blocking_coherence` 未清除，导致159/160、无ready任务的死锁。新增回归并修正：完整Unit重审通过才能恢复accepted，失败衔接窗口不能当作已通过缓存，旧JSON也须补发该窗口；累计HTTP和一次自动衔接修订上限保持不变。独立复审及恢复运行结果继续记录，尚未写为整本completed。

旧链路对预先选定的两段实书原文另运行了同模型/4096上限的比较，实际2次HTTP、服务商报告2267输入/221输出tokens，返回completed。原始自动候选（含新链路已经拒绝的错译）写入 [实书匿名对照](review.md)，尚待人工评分；后续代理修订不会冒充自动质量提升。

本地技术书的原包均未通过源规范。其中《The New Rules of AI Search》仅有一条真实非法role诊断；另外两书有大量规范问题。不会在正式入口静默修复源文件。技术侧验证仅在独立验收副本上修正该单条role，并明确记录原/副本哈希，原书不变；其identity验证不算真实模型翻译。

验收时发现EPUBCheck文本解析把包含单数“error”的汇总行当成额外诊断，已改为只识别行首严重级别。新增回归验证诊断仍阻止通过，同时不重复计入汇总或书名中的Error。相关测试通过，独立审查通过。

## 技术实书推动的确定性修复

独立技术副本只删除 `OPS/navigation.xhtml` 的 `<ol role="directory">` 中非法role，原书SHA不变。副本SHA256为 `07664e95aafd98d30c308abdec0cd0a8db81a00eebffd58b81a0f99760e3a589`，通过源EPUBCheck。其27份XHTML加OPF/NCX共29份文档、2615 Unit，覆盖87个code、2个pre、20个table、5个noteref、5个endnote、374个复杂内联块。

1. **重复空格误核对。** 索引页 `A2A. <i>See</i> <a>agent‐to‐agent</a>` 的受保护尾空格与前面的普通空格同值。旧验证按值replace误删前者，造成identity假失败。现在从已验证目标事件和不可变SourceSlot范围构造期望，重新读取实际XML比较；不复用装配结果作期望。4项回归和独立review通过，29/29原样装配及成品EPUBCheck通过。
2. **整篇CSS锁序误伤。** 原来1672个g中1665仅因文档级CSS门禁锁定、7个因硬边界固定。已支持合法`@namespace`声明，按保守候选与实际父域处理规则；继承属性约束叠加，未知规则仍回退。独立审查找出的祖先属性、命名空间属性、可移动祖先、继承组合及透明a包含block等漏锁均已补回归并关闭。最终只读探针为1321个same_parent、344个局部locked、7个hard fixed，优先正确性而非最大可移动比例。

提取器版本提升为 `epubox-extractor-2`。新运行使用新版本；ready的v1运行继续消费冻结JSON，不重提取。未ready且提取器版本变化时，在provider前明确拒绝并要求新运行，防止混用计划。v2全量技术验收另建运行保留v1证据：29/29文档原样装配、2615 Unit、零HTTP，成品实际EPUBCheck零错误/警告。输出 `work/v23-acceptance/p14/tech/new-rules-identity-v2.epub`，SHA256 `69104788335425e2caf3931d571b48080dd39d4eab6363a0b4402db1d444d38e`；报告为 `tech/acceptance-report-v2.json`。

技术identity输出已在Calibre实际查看：从封面经目录进入第一章，翻页后两列表格、边框、标题和斜体均可见，无可见裁切。它仍是英文原样输出，不是技术书译文阅读证据。Calibre会添加 `META-INF/calibre_bookmarks.txt`；已保存阅读副本并从经校验的staging恢复正式产物，后续阅读使用单独副本以保持发布哈希。

## 真实技术摘录与衔接契约补齐

从技术实书保留13个原始DOM子树，制作437英文词的EPUB3摘录，覆盖两处代码、两列表格、两个脚注引用/尾注闭环和复杂内联。原文定位与C14N哈希、资源裁剪记录见 `work/v23-acceptance/p14/technical-excerpt/report.json`。摘录源SHA256为 `b447b9de1699e7b732b7c63c28fc177da65670b7c1b7133e472e943792a6accf`，实际EPUBCheck零错误/警告；不是整本技术书自动翻译。

首次v2提取运行在19次HTTP后27/34 Unit通过。真实空pagebreak被当作可移动g而导致错误，已改为固定x及局部硬边界，并兼容多个role token和空锚点；提取器版本提升为 `epubox-extractor-3`，旧运行不重写。v3新运行在31次HTTP后达到34/34；其中两项通过正式修订入口纠正，属于代理修订。随后发现机器遗漏的英文head title，单独提交中文完整目标重审。

独立架构复核发现并确认了以下遗漏，本轮按需求§8.4修正：

- HTML导航派生标签不参加正文相邻检查；非派生长导航仍覆盖全部切片接缝。
- 从冻结DocumentPlan的节点路径和源标记恢复真实table/tr/cell同行关系，当前摘录产生6个表格行窗口。
- 根据同资源fragment引用生成正文→对应脚注窗口，避免正文→错误尾注或尾注之间的伪叙述邻接。外部URL不会当作本地引用；独立属性不进入关系组。跨文档脚注语义窗口尚未覆盖，本次证据不代表该场景通过。
- 所有非派生长Unit都生成接缝。独立属性、元数据、head title和导航接缝单独绑定版本，不阻塞正文，不因独立修订而让正文重扫；章节参与者变化仍使章节窗口整体失效。
- 使用一个DocumentStatus及默认空的 `window_versions`，旧JSON可读取并保守补查；固定窗口集合改变会清除不再适用的检查。累计HTTP、协议修复与一次自动衔接修订上限均保留；旧运行新增窗口不能静默增加额度。

该阶段代码验证：全库 **604 passed（22.12s）**，Pyright **0 errors / 0 warnings**，5个本轮Python文件Ruff和format通过，`git diff --check`通过。全仓既有lint/format债务仍按首轮报告保留。需求与已审架构计划SHA256均未改变。运行状态、数据面及最小契约复核均 **APPROVE**。数据review指出的嵌套列表误拆脚注已修复并通过复核。代码提交 `9ab7fc4`。后续实书报告发现派生目录被计入普通待翻译项，已用小范围修复与回归排除，仍保留等待依赖及失败记录；独立review APPROVE，提交 `109338c`。

## 技术译本正式结果

技术摘录v3已 `completed`，30个普通Unit接受、4个派生导航有效，共34/34。最终累计47次HTTP；再次正式resume保持47次、没有新增调用，pending/review pending均为0。6份文档通过独立装配核验，正式成品通过EPUBCheck 5.4.0，0 fatal / 0 error / 0 warning。

- 成品：`work/v23-acceptance/p14/technical-excerpt/translated-v3.epub`
- SHA256：`6e38e92853b696b2cea48d4a68b10e81e30cc556a578eb82e815dc97c183076b`
- 正式运行：`bd3946dd36e144e49d51bacbbeecbed3`。
- 表格检查显式追加36次doc额度（原6→42），运行上限仍为100，实际累计次数没有重置。
- 代理修订包括日志句、文献标题、漏译的head title、被误译为“推荐人”的referrer；这些经过相同机器门槛，不能计入纯自动翻译成功率或人工盲评。
- 43次HTTP时首个机器通过产物及报告另行保存。随后纠正referrer术语，47次时发布最终产物，保留过程证据。
- 已复制 `translated-v3-reader-copy.epub` 用于阅读验收。2026-09-28再次尝试UI时Mac已锁屏、工具无法自动解锁，已请求用户解锁。此译本尚未取得阅读器目视证据，不把此前英文identity检查挪作中文结果。

## 公开实书最终阻塞与停止记录

完整普通单元157项接受、3项导航派生有效，160/160；但衔接检查尚未通过。针对许可说明中“美国及世界大多数其他地区”被扩大为全世界的实际错译，代理提交完整修订后重审通过。后续模型仍给出两个major阻塞：一项要求把既有一致的Amontillado音译换成另一种；另一项长解释反复确认日期标签没有冲突、应返回空issues，却依旧以major问题返回。

后者是可复查的自相矛盾判定。代理选择停止派发额外付费请求，保留冻结状态，不通过重试碰运气或人工改accepted绕过门槛。最终累计159次HTTP，当前147个衔接窗口仅5个通过、2个阻塞，其余未验收；**没有公开实书译本输出**。运行正式退出为 `paused`。CLI的SIGINT沿用 `stop_reason=user_cancelled`，但此次SIGINT由代理发出，并非用户要求暂停；原因与完整问题保存在 `public-coherence-final-blockers.json`。

此次停止有1个在途attempt最终记为unknown，连同此前2次限流响应，共3项usage不明；不能假定未执行或未收费。没有留下后台付费进程。

## 请求与已知用量

以下按request journal中的sent attempt统计，不把同批Unit归属计数相加。快照位于 `work/v23-acceptance/p14/latest-run-evidence.json`，可用同目录 `snapshot_runs.py` 重建。

| 本轮范围 | HTTP | 已知输入tokens | 已知输出tokens | usage不明 |
|---|---:|---:|---:|---:|
| 公开实书完整运行 | 159 | 222367 | 79443 | 3 |
| 技术摘录v2旧运行 | 19 | 25513 | 9178 | 0 |
| 技术摘录v3正式完成 | 47 | 46938 | 11013 | 2 |
| 语义探针v3 | 5 | 3986 | 1009 | 0 |
| 语义探针v4 | 6 | 4934 | 1436 | 0 |
| 公开实书旧链路两段对照 | 2 | 2267 | 221 | 0 |
| 本轮合计 | 238 | 306005 | 102300 | 5 |

加上首轮已记录的53次HTTP，总计291次；已知输入333402、输出107412 tokens，7次usage不明。没有可靠价格证据，费用为unknown/null；本表不是同样本同流程成本优劣比较。自动结果、代理修订、真实规范检查和用户人评分别留证，不混为自动成功率。

## 当前版本放行判断

P14尚未通过：公开实书衔接受到当前模型自相矛盾判定阻塞；技术译本的阅读器目视验证因锁屏待完成；实书及剩余小样本人评仍待回复。已观察到自然度漏检、校对分歧及真实漏译，不能只看结构与EPUBCheck宣布质量通过。跨文档脚注语义窗口也尚未覆盖，本次同文档样本不能替代其验收。

2026-09-29，用户明确要求在本分支以 v2.3 完全替代旧翻译入口。普通 `translate` 已切换，旧 CLI 模式移除；这是用户对入口的最新指令，**不改变上述P14质量验收结论**。

入口切换后重新验证：全库 **616 passed**、Pyright 0，CLI/校验器变更范围 Ruff/format 通过。领导力书在严格确认 EPUBCheck 5.4.0 已知 `nav aria-labelledby` 误报且无其他错误时，以官方便携 5.3.0 检查通过；另一册有真实非法role的 EPUB 仍由5.4.0拒绝。显式指定校验器始终优先。风险与输入安全回归经独立review **APPROVE**。
