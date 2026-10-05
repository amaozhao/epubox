# v2.3 实施与验收记录

更新：2026-09-28。用户已明确批准执行全部任务；实施分支 `codex/epubox-v23-implementation`，起点 `13ece3a`。

**P00–P13 的实现和自动回归已完成；P14 质量验收仍未完成。** 后续已完成源实书预检、技术书原样装配和部分阅读器检查；真实翻译与剩余人评的进展见 [P14 后续验收记录](publication/acceptance.md)。2026-09-29 用户明确要求在本分支用 v2.3 完全替代旧翻译入口；普通 `translate` 现默认且仅运行 v2.3。此入口切换不代表 P14 质量验收通过。

需求与架构文件保持审批时的原文，其中旧“待确认”状态已被用户后续实施授权取代：

- `epubox_redesign.md` SHA256：`bbd1aed9a313f34d5af23514253b48218a8552253f727e4219e0fd3ee084f2b0`。
- `epubox_architecture_plan.md` SHA256：`1214ccf216fa06f50c0a759ea959f229c8e6ff7b3a2b9382c23f338f29fad2f7`。
- 计划的两路独立 review 保留在 `epubox_architecture_review.md`；实现审查见下文。

## 任务状态

| 任务 | 状态 | 交付与证据 |
|---|---|---|
| P00 | 完成 | 基线435测试；原创EPUB2/3工厂；记录旧行为、T01–T32与版本验收边界 |
| P01 | 完成 | 严格schema、三版本、确定性hash、原子JSON、文件锁、delta合并与request journal |
| P02 | 完成 | 安全XML/ZIP、资源清单、输入边界、源EPUBCheck及输出路径预检 |
| P03 | 完成 | 完整Unit、text/tail/属性槽位、translate继承、代码保护、固定语境/术语/主标题 |
| P04 | 完成 | g/x/b codec、父域与硬边界、有限CSS分析及保守锁序 |
| P05 | 完成 | 原始文档与计划JSON化；ready最后提交；跨进程JSON-only identity装配 |
| P06 | 完成 | 严格wire协议、真实完整消息预算、共享RPM/TPM/在途限额、每HTTP预留及SDK隐式重试关闭 |
| P07 | 完成 | 完整队列、非fail-fast、逐项批结果、缩批/单项协议修复、公平继续与有限退出 |
| P08 | 完成 | 五项review、完整目标修订与重审、版本绑定的章节衔接及有界修订 |
| P09 | 完成 | 派生导航、语言/元数据ChangeSet、保留资源字体、原子发布、CLI及真实小样本闭环 |
| P10 | 完成 | 字符簇安全CutPlan、虚拟边界、双阶段预算、最多一次整个Unit重规划 |
| P11 | 完成 | 片段复用/重审、接缝和章节向量、完整人工修订入口及本地门禁 |
| P12 | 完成 | 全清单恢复、缺损隔离、building续跑、显式文档精确修复/重试/追加额度、发布恢复 |
| P13 | 完成自动回归 | 最新616全库测试通过；本轮独立review完成，模型替身与真实provider证据明确区分 |
| P14 | 尚未通过 | 用户首题盲评A可接受/B有问题；34 Unit技术摘录正式完成，公开实书仍受真实模型衔接判定阻塞；阅读器和其余人评待补；默认入口已按用户新指令切换，不等于质量放行 |

## 模块与入口

- `engine/schemas/v23.py`、`engine/services/store.py`：持久契约、版本、源身份、计数与原子存储。
- `engine/item/{extractor,inline,planner}.py`、`engine/core/styles.py`：源归属、投影、完整消息预算和切片。
- `engine/agents/{runtime_v23,protocol_v23}.py`：实际provider调用、共享调度与严格响应。
- `engine/orchestrator_v23.py`：唯一运行状态机；翻译、review、衔接、恢复和人工修订。
- `engine/epub/{preparation,validation,publication}.py`：源预检、准备提交、独立装配核验和正式发布。
- `engine/cli_v23.py`、`main.py`：普通 `translate` 默认且仅运行 v2.3；保留 `resume` 及显式修复/重试入口。
- 复用 `core/markup.py`、`epub/replacer.py`；旧 `epub/builder.py` 修复打包异常伪成功及已有输出被截断的问题。

使用、限额、修订格式和迁移说明见 [README](../../README.md)。旧HTML checkpoint与v2.3状态不混用。

## 自动验证与限制

以下为首轮实施时的历史验证；后续变更的最新验证见 [P14 后续验收记录](publication/acceptance.md)：

| 检查 | 结果 |
|---|---|
| `uv run pytest -q` | **565 passed，16.75s** |
| `uv run pyright` | **0 errors / 0 warnings** |
| 新增及修改的v2.3、CLI、共享markup/replacer与相关测试 Ruff | **通过** |
| 所有29个触及Python文件 Ruff format | **通过** |
| 实现文件 `git diff --cached --check` | **通过**；未改动的需求原文第3/4行有Markdown双空格换行，保留原始hash |
| 全仓 Ruff | **228项既有问题仍在**，基线239；不能写成全仓lint通过 |
| 全仓 format | 5个原有文件待格式化，按计划未进行无关全仓格式化 |

旧builder仍有两项既有 `BLE001`（语言处理，549/579行）；未把无关语言重构混入打包修复。未格式化的旧文件为 verifier、workflow、precode、orchestrator及旧workflow测试。

以下是需求到自动证据的索引；它表示确定性/协议部分的回归覆盖，不把任何替身断言解释为真实语义或实书验收通过：

| 用例 | 主要测试文件与断言 |
|---|---|
| T01、T05、T14、T26 | `test_extract_assemble.py`、`test_store.py`、`test_end_to_end.py`：槽位、继承、上下文/词表hash及全JSON跨进程恢复 |
| T02、T03、T06、T07 | `test_inline_planner.py`、`test_extract_assemble.py`：同父重排、引用/父域、硬边界有序文本域、CSS保守范围 |
| T04、T15、T25 | `test_runtime_protocol.py`、`test_batch_scheduler.py`：严格JSON、乱序/局部错误、整批坏响应、缩批、每HTTP计数与未知usage |
| T08、T09、T24、T31 | `test_inline_planner.py`、`test_orchestrator_v23.py`、`test_store.py`：字符簇/虚拟边界、review预算、补中间片、整代失效及兄弟结果合并 |
| T10、T11 | `test_runtime_protocol.py`、`test_orchestrator_v23.py`：五项校对协议与候选门禁；真实语义检出/格式绑定质量仍属于P14 |
| T12、T13 | `test_orchestrator_v23.py`：replace必须新版本重审、旧hash拒绝、衔接向量/次数恢复、空affected列表拒绝 |
| T16、T27、T30 | `test_store.py`、`test_package_publish.py`、`test_orchestrator_v23.py`：hash/锁、ready前后恢复差异、全清单补洞与缺损隔离 |
| T17、T18、T19、T20 | `test_package_publish.py`、`test_extract_assemble.py`、`test_end_to_end.py`：实际装配槽位/属性、跨文档链接、元数据/导航和输入支持边界 |
| T21 | `test_package_publish.py`、旧 `test_builder.py`：原子发布、别名拒绝、publish意图恢复及打包失败保留已有输出 |
| T23、T28、T29、T32 | `test_orchestrator_v23.py`、`test_batch_scheduler.py`、`test_cli_v23.py`：并发1/2局部继续、层级额度、无worker依赖等待及非成功退出 |
| T22 | **待P14实书与阅读器验收，不以端到端替身测试代替** |

## 独立实现审查

实现经过数据/提取/装配与运行/恢复/发布两路独立代码审查，先修复再复核。主要修复包括：

- 槽位区间漏重/双向归属、CutPlan/hash校验、translate父尾文、硬边界文字域顺序。
- 同代delta写回/累计计数、未发HTTP不耗逻辑额度、批attempt崩溃回放不得重复逻辑计数。
- 共享网络故障有界暂停、取消记unknown、关闭SDK自动重试、错误记录精确隐藏API key。
- 所有目标走同一本地门禁；丢失衔接文件恢复已有修订轮数；ready前恢复与ready后修复分离。
- 整批坏JSON隔离为一次单项协议修复，review协议修复不侵占正常replace及复核额度。
- 显式retry按当前周期计算协议修复额度，累计repair/HTTP计数继续增加，translate/review均有回归。
- 最终元数据处理后再独立核对；发布不伪成功；旧打包失败不破坏已有成品。

两路独立实现审查均为 **APPROVE**，已报告的阻断问题全部修复并复核。数据审查验证了批attempt回放；运行路径最终审查验证了原始multi-item坏JSON/singleton成功探针及显式retry新周期，9项批处理测试、Pyright、Ruff通过。审查通过仅指已检查的实现范围，不替代P14外部验收。

## 真实工具与模型证据

本地便携 EPUBCheck 5.4.0 + Temurin 21.0.12.1+1，下载自官方发布并校验官方SHA；仅放在忽略的 `.tools/epubcheck/`，未全局安装。原创EPUB2/3均完成源规范检查→JSON-only identity→发布→真实EPUBCheck，零ERROR/FATAL/WARNING。

真实模型为已配置的 `agnes-3.0-flash`，本次固定context 32768 / output 1024。官方[模型说明](https://wiki.agnes-ai.com/en/docs/agnes-30-flash)公布512K/65536容量；本次使用更低子限额，`.cn` gateway的只读models响应确认模型存在但没有容量字段。

1. 第一轮24次HTTP因动态output cap只有64–86导致截断，运行正确暂停且没有正式输出。随后明确追加2次探针，定位到review提示使模型返回非法 `target:null`。
2. 修复输出预留为配置上限、review使用三种分离示例，并更新提示版本为 `epubox-v23-2`。新运行2次探针通过，继续从JSON续跑。
3. 新运行在24次总HTTP内完成10/10 Unit。曾有9/10通过，剩余标题 `Chapter → 第章` 被真实review拒绝；代理通过正式修订入口提交 `章节` 并重新机器校对后完成。**这是代理修订，不是人工盲评。**
4. 成品经独立结构/内容检查、打包重开及真实EPUBCheck通过，`reader_check=not_run`。
5. 同一模型/输出上限对旧链路运行两段原创文字，实际3次HTTP；匿名对照已写入 [盲评表](review.md)，用户现已给出首句“A可接受、B有问题”，其余评分待填写。

成功产物：`work/v23-acceptance/real-model-zh-v2.epub`。
SHA256：`dc31c72ee4171c6835aeb2495b6ab5f365099841a0ae43ed80015a8b59fde151`。
成功运行ID：`a60059a025b24d3789d7e989595249fd`；完整结果在 `work/v23-acceptance/live-repair-v2-result.json`，运行目录保留每次请求及报告。

| 范围 | HTTP | 已知输入tokens | 已知输出tokens | usage不明attempt |
|---|---:|---:|---:|---:|
| 第一轮及2次故障探针 | 26 | 9926 | 1924 | 2 |
| 成功的新提示运行 | 24 | 14786 | 3134 | 0 |
| 旧链路两段对照 | 3 | 2685 | 54 | 0 |
| 合计 | 53 | 27397 | 5112 | 2 |

没有可靠价格证据，费用保留unknown/null，不能报零。成功小样本在人工修订前9/10、修订后10/10，不能外推整书自动完成率。实际书籍质量、性能和成本比较仍未验收。

## P14 剩余及继续条件

- EPUB2：已取得源规范通过的公开实书《The Cask of Amontillado》，157个普通Unit接受、3个派生导航有效，共160/160，但衔接判定出现自相矛盾major后代理停止本轮，尚无成品；保留首次自动结果和修订记录。
- EPUB3：技术书独立规范修正副本已完成29文档、2615 Unit 的零模型原样装配；真实原文摘录34 Unit已在47次HTTP后完成模型流程与正式发布，包含代码、表格、同文档脚注及复杂内联，EPUBCheck零错误/警告。
- 人工盲评：首句用户判断已记录；其余小样本及实书固定段落仍待用户评阅。机器审查与代理修订均不算人评。
- 目标阅读器：锁屏阻塞已解除，Calibre已实际查看小样本中文及技术identity内容。后续正式产物使用单独阅读副本，避免阅读器书签修改发布文件。
- 人评及实书质量门槛仍须补齐。默认入口虽已按用户2026-09-29的新指令切换，不能把局部机器通过或外部等待冒充P14完成。

`.tools/`、`work/`、下载书籍、密钥和 `.DS_Store` 不进入实现提交；证据保留在当前本地工作区。
