# 提供书籍的真实测试记录

日期：2026-10-06\
分支：feature/preflight\
状态：2026-10-07 已通过普通命令自动续传完成中文 EPUB，真实 EPUBCheck 通过。下文保留历史诊断；本记录不表示 T20 全部完成。

## 输入及校验

用户授权直接使用 `/Users/amaozhao/Downloads/epub/ai-side-hustle-playbook.epub` 测试。原书 SHA-256 为 `bc1211d1f67ad22c4df136cd3b6e1479d504515c93a9aac49ae92c4587f81149`；原文件未修改。

真实 EPUBCheck 5.3.0 在模型请求前拒绝原书：943 errors、0 fatals、0 warnings。OPF 声明 EPUB 2，而正文包含 EPUB 3 标签；一个 cover landmark 引用缺失的资源。

测试副本位于原书同名目录的 `input/source.epub`。仅修复 `content.opf` 的 EPUB 3 声明、元数据及 manifest properties，和 `EPUB/nav.xhtml` 的无效封面链接；作者和标识符值保留，其他 25 个资源字节不变。测试副本通过真实 EPUBCheck：0 errors、0 fatals、0 warnings。修复明细与哈希保存在该目录 `validation.json`。

测试沿用配置的 `agnes-3.0-flash`、10 RPM、并发 2 和 2000-token 原子限制；没有更换服务或扩大请求额度。输出指定为原书旁 `ai-side-hustle-playbook-cn.epub`，工作资料保存在原书同名目录。

## 发现及修复

真实准备暴露元素 tail 空白的 DOM 父级校验错误。最小复现为 `<p>Before <em>inside</em>\n </p>`：tail 存储在 em 节点，但实际属于 p。`fill.py` 原先误把存储节点当作物理父级。

修复只更改普通元素 tail 的父级判断，仍根据原节点路径验证父级；comment/PI 特殊 tail 规则保持不变。p/em 和 ol/li/a 回归在修复前失败、修复后通过；现有跨项重定向拒绝测试继续通过。真实副本全部 19 个 XML/XHTML 资源、793 个原子项通过原字节 identity 回填。

DOM 修复相关测试 22 passed；当时全量测试 **666 passed（128.05 秒）**。Ruff、格式、Pyright 和 diff 检查通过，独立代码审查 APPROVE（0 issues）、架构审查 CLEAR。

## 输出预算与进度修复

真实 provider 的 59/73-token 标题响应以及 198-token 校对响应均被截断。完整响应的 cl100k 估算被直接用作 provider cap，既没有输出分词差异余量，也没有充分使用已配置的 4096-token 上限。截断结果被拒绝，没有回填。

新 CLI 冻结 `output_budget_version=3`，输入预算仍为 v2。输出 v3 计算完整响应估算加 50% 和 256 tokens 的余量，再预留至少配置的输出 cap；可派发请求使用完整配置 cap。如果所需预留超过输出额度，或输入加完整输出预留超过上下文额度，则在 HTTP 前阻断。

旧 v2 limit 序列化和预算计算不变，全部 793-member/59-batch ready 校验仍通过。输出 v3 的 limits 和 wire cap 都进入请求身份；旧任务不原地更改。未完成的 v2 真实接口路径在调度前暂停，完成的 v2 任务仍可恢复出版。测试中显式注入的 transport 是假接口验证接点。

另修复响应先落盘导致累计 usage 增量被漏算，以及通用进度缺少总单元数而显示 `18/0` 的问题。响应落盘、finish、重启和回放的 usage 恰好累计一次。

## 真实运行结果

当前旧任务：`9bf8af3e052aedd2f5f27619da57d6a084d1714dcd196d8d10e14e76baf088a6/12ffdd84ed1948e395d9427fe8052a4c`。

- 术语提取：46/46 窗口完成，20 次 HTTP；resolution 2 次 HTTP，3 组局部诊断继续按冻结规则记录。
- 预检：793 个原子项，59 个旧预算初译批次；输出 v3 的只读预检也通过全部 793 项，无阻断。
- 正文：39 项初译已保存、18 项校对通过、19 项待处理；累计 39 次 HTTP（术语 20、resolution 2、translate 13、review 4）。
- SIGINT 中断后，正常 CLI resume 本地恢复，没有新增 HTTP；仍为 18/793、HTTP 39。累计实际已知输入 153845、输出 32444 tokens。
- 当前状态 paused，原因是旧输出预算需要新 v3 任务。全部旧 JSON、响应、用量与译文保留。
- 原书 SHA-256 不变，最终 `ai-side-hustle-playbook-cn.epub` 尚不存在。未完整翻译或结构无效时不能发布成功成品。

用户已明确要求直接开始翻译，因此已授权新版全书运行。重跑使用单独 work root，保留旧任务；本次没有实现或声称通用断点预算迁移。


最终代码验证：全量 pytest **671 passed（128.92 秒）**；Ruff、163 个 Python 文件的格式、Pyright（0 errors/0 warnings）、阶段文件约束与 diff 检查通过。独立代码审查 **APPROVE（0 issues）**，架构审查 **CLEAR**。

新版以同一源副本和现有词表进行的只读试算为 64 个初译请求批次（正文45、metadata18、navigation1），不是793次请求；真正新任务的词表会重新冻结，最终批数以落盘计划为准。工作根目录为原书同名目录下的 `current`。


## 新版运行中的校对恢复修复

新版任务为 `current/9bf8af3e052aedd2f5f27619da57d6a084d1714dcd196d8d10e14e76baf088a6/c5ebfe476aca406684d7d221b766ce56`。实际初译计划为64批（正文45、metadata18、navigation1）。

第一次接口连接中断时，94项校对已通过，103项初译已保存，累计68次HTTP。针对42个失败项及3个共享未知请求项显式重试后，暴露旧review请求使用当前retry epoch重建的错误。

修复要求草稿的hash与review epoch都匹配历史请求才可直接复用；否则从持久初译响应重建历史草稿。历史review还使用自身record_versions和plan_epochs。26个真实历史review请求的只读重建均通过；续跑保留全部94个已通过项并继续完成。

新增共享review失败→重试→重启的回归通过；全量pytest **672 passed（126.91秒）**，独立代码审查APPROVE、架构审查CLEAR。新版全书仍在翻译，尚未出版。

## 2026-10-07 普通命令续传及出版

执行用户要求的命令，无附加参数：

```sh
.venv/bin/python main.py translate /Users/amaozhao/Downloads/epub/ai-side-hustle-playbook.epub
```

原书通过同名目录的 `active.json` 关联既有修复快照任务。`source.json` 保留快照主来源并追加用户原书别名，路径、哈希和 inode 都受保护；原书 SHA-256 仍为上文记录的 `bc1211…81149`。

最后阻塞项是 `EPUB/text/ch005.xhtml` 的 head title，源文本与当前目标均为 `ch005.xhtml`。八次真实校对均认为无需修改，但协议错误地要求文件名也必须通过语言流畅度检查。修复仅在 metadata/head_title、无占位符、源与目标完全相等、且等于冻结资源文件名时允许 fluency/script 为 not_applicable；accuracy 仍必须通过。执行、响应回放和只读验证使用同一判断，普通正文不能套用此例外。

普通命令保留既有译文，仅新增一次真实校对请求：793/793 项通过，累计 HTTP 从 424 增至 425，进程退出码 0。

成品：`/Users/amaozhao/Downloads/epub/ai-side-hustle-playbook-cn.epub`，391307 字节，SHA-256 `b5f6fd325cd6c7e8cb2be578068b07a14e53fae0bf887eca785ef8efe5a6fb94`。发布记录中的真实 EPUBCheck 5.3.0：0 errors、0 fatals、0 warnings。此结果证明程序完成和 EPUB 结构有效，不代表人工逐句校审。

最终全量 pytest **707 passed（136.00 秒）**；Ruff、165 个 Python 文件格式、Pyright（0 errors/0 warnings）、变更文件单词命名/≤1000 行和 diff 检查通过。独立代码审查 APPROVE（0 issues）、架构审查 CLEAR。

随后再次执行完全相同的无参数命令，退出码 0，返回同一成品：793/793、累计 HTTP 仍为 425。过程仅本地核验已保存响应和出版证据，没有新增模型请求；成品哈希和原书哈希均保持不变。

## 2026-10-07 新书输入兼容验证

用户反馈 `ai-agents-harnesses-foundations-langchain-langgraph.epub` 在启动后被源 EPUBCheck 拒绝。原书 ZIP 完整、XML 可解析，OPF 声明 EPUB 2.0，而正文包含 EPUB 3 标签和属性；真实检查器报出 2216 条 RSC-005 错误。

修复后，在临时目录执行生产 P1：成功得到 parsed_ready，18 个正文资源、2104 个单元。使用原样回填的临时候选走生产成品验证和真实 EPUBCheck：2216 条原有问题全部匹配，实际 passed=false，没有新增问题。原书 SHA-256 `a6e6367e1bb10109641d640b11f4489c8e949da1a6fd0ebe58940aa93ceb0224` 保持不变；临时目录自动清理。

此次仅验证解析和结构校验路径，没有发模型请求，也没有生成或声称已完成这本书的中文翻译。正式翻译仍使用用户原来的普通命令。

最终全量 pytest **735 passed（140.61 秒）**；Ruff、167 个 Python 文件格式、Pyright（0 errors/0 warnings）、变更文件约束和 diff 检查通过。独立代码审查 APPROVE（0 issues）、架构审查 CLEAR。用户进一步提出目录仅保留解压内容和单 JSON，目标记录在 [publication.md](publication.md)，当前存储布局尚未重构。
