# 《The Leadership Algorithm》实书测试

测试对象：`/Users/amaozhao/Downloads/epub/leadership-algorithm-intelligent-systems.epub`。原书 SHA256：`c5173f54fba33e04187e526955d050e8337161f281ba1d0209b6555e387c9f2d`。测试不修改下载目录中的文件；详细机器证据位于 `work/v23-acceptance/leadership/`。

## 源文件规范检查

EPUBCheck 5.4.0 对原书 `OEBPS/navigation.xhtml` 的四处 `<nav aria-labelledby>` 报 RSC-005。它们均位于原始 ZIP 中，本项目没有生成这些标记。EPUBCheck 5.3.0 对**同一原书哈希**检查为 0 fatal、0 error、0 warning。

这四处属于 5.4.0 的[已知误报](https://github.com/w3c/epubcheck/issues/1726)：W3C [EPUB 3.4 导航示例](https://www.w3.org/TR/epub-34/#sec-nav-def-types-lot)也使用 `aria-labelledby`。本次固定使用官方 EPUBCheck 5.3.0 作为源文件和输出文件的检查器；安装包来自官方发行页，SHA256 与发行资产登记的 `6c07e68584b2e2ce2f89fe06e1246dfead3eb36b46b340e7d93524f29dcff6c5` 相符。保留原有无障碍标签。

当前 CLI 默认选本地的 5.4.0；若它对 EPUB 3.0 导航 `<nav aria-labelledby>` 报出且**只有**该已知误报，程序会用本地便携 5.3.0 重新验证同一本书，全部通过才用于后续准备和发布。显式 `--epubcheck-command`/`EPUBCHECK_COMMAND` 始终优先；其他诊断仍阻断。

## 全书无模型原样往返

原书约 6.8 万英文词。完整准备阶段生成 25 份 DocumentPlan、6736 个 Unit、6736 个 Segment，准备失败 0 项。逐文档原样装配核验 25/25 通过，输出 EPUBCheck 5.3.0 为 0 fatal、0 error、0 warning，模型/网络请求 0 次。

原 ZIP 与输出 ZIP 均有 44 个资源，名称集合相同；19 个非 XML 资源逐字节相同。25 份 XML 经序列化后字节不同，但 C14N 规范化内容逐份相同。这证明本次**原样路径**没有丢失或改写书中的文本、链接和 XML 结构；不证明中文译文语义正确。

原样输出：`work/v23-acceptance/leadership/leadership-identity.epub`，SHA256 `6cbac871ca243e736ae1738d460ed06518f90073449d0a3706c5b1f0f126402c`。证据：`identity-report.json`、`identity-document-probe.json`、`identity-zip-diff.json`。

## 真实内容形状与衔接

书中有 37 张 XHTML 表格、36 个嵌套列表容器，以及 10 对以 `role=doc-noteref/doc-footnote` 表示的作者与机构引用；没有 `<pre>`、`<code>` 或 MathML。技术缩写、百分比、范围、货币和公式以普通文本及 CSS class 呈现。故不能把它们简单归类为代码块或标准 `epub:type` 脚注。

原先把正文中的 `section epub:type=toc/index` 当成叙述段落，分别在目录和索引生成 216、740 个错误的相邻窗口。修复后两处均为 0；全书仍有 150 个表格同行窗口和 10 个角色型引用关系窗口。修复经独立 review 通过、全库 605 项测试及 Pyright/Ruff 检查通过，提交 `51b5ffe`。证据：`source-audit.json`、`movement-audit.json`、`coherence-window-probe.json`。

## 真实模型译文抽样

从原书结构预先选定段落、指标、表格数值和表格说明等 10 个 Unit，在原始完整 BookPlan 的上下文中做有界模型翻译/复核。这个测试不会输出混合语言的整本 EPUB；每次调用均写入独立请求日志，运行 HTTP 上限为 24。该探针直接调用同一翻译及复核核心阶段，但没有运行整本队列、自动重试和章节衔接，不能等同正式整书运行。

首轮共 20 次 HTTP，10 个 Unit 中 8 个经过机器复核接受。带受保护边界的“数据碎片化影响”段落首次返回结构不合法目标；Agent 争议处理流程句被模型判为语义歧义并标记 `needs_attention`。直接阶段探针没有执行调度器的自动重试，前者不等于正式调度一定会失败。原始自动输出和响应留在 `sample-results.json`，选择先于生成结果固定在 `sample-selection.json`。

随后通过正式完整目标修订入口提交这两项代理译文，并重新执行模型复核；两项均接受。**首轮自动 8/10，代理修订后样本 10/10 通过机器门槛**，两种结果分别保留。总计 23 次实际 HTTP；已知输入 16850、输出 3276 tokens，2 次超时 attempt 的 usage 不明，可靠费用信息为 null。修订文件、响应和请求日志位于 `sample-agent-repairs.json`、`sample-agent-repair-results.json` 与独立运行目录。代理修订不是用户人评，机器校对通过也不能外推到全书 6736 个 Unit。

## 结论与边界

这本书已证明**全书原始文本和 XML 结构可以无损通过提取、规划、装配和打包路径**。真实模型的首轮 10 Unit 样本没有全部自动通过；已验证的代理修订可经相同复核入口接受。对 6736 个 Unit 的整书中文译文及其章节衔接、阅读器显示和人工语义质量，本轮尚无通过证据，因此不能称“完全正确处理了整本译文”。2026-09-29 用户另行明确要求切换默认翻译入口；入口切换不构成本报告的质量结论。
