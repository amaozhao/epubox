# 提供书籍的真实测试记录

日期：2026-10-06\
分支：feature/preflight\
状态：真实测试发现问题并已修复；旧预算任务暂停，新预算全书重跑等待用户明确选择。本记录不表示 T20 全部完成。

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

用户的新版全书重跑选择尚待答复。重跑使用单独 work root，保留旧任务；本次没有实现或声称通用断点预算迁移。


最终代码验证：全量 pytest **671 passed（128.92 秒）**；Ruff、163 个 Python 文件的格式、Pyright（0 errors/0 warnings）、阶段文件约束与 diff 检查通过。独立代码审查 **APPROVE（0 issues）**，架构审查 **CLEAR**。
