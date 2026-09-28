# EPUBox

本地 EPUB 英译简中工具。v2.3 使用固定语义单元、受限格式引用和持久 JSON；模型不生成原始 XHTML。局部失败会保存原因并继续其他独立任务，只有整书检查完成后才发布 EPUB。

`translate` 现在直接使用 v2.3 JSON 引擎，旧翻译入口已从 CLI 移除。旧 HTML checkpoint 不转换为新引擎结果；已有 v2.3 运行仍通过 `resume` 续跑。实书、人评和阅读器质量验收仍需分别完成。

## 安装与配置

需要 Python 3.13+。新引擎的文件锁支持当前 macOS/Linux 环境。

```bash
pip install -e '.[dev]'
```

沿用 `.env` 中的模型配置，例如 `AGNES_API_KEY`、`AGNES_BASE_URL`、`AGNES_MODEL`、`AGNES_TEXT_RPM`。新引擎保留 Agnes 和原有 CR proxy 适配；不会自动切换未配置服务商。密钥不写入运行 JSON。

v2.3 在调用模型前要求源书通过 EPUBCheck。先按 [官方说明](https://www.w3.org/publishing/epubcheck/)准备 Java 与 EPUBCheck，再指定命令：

```bash
export EPUBCHECK_COMMAND='java -jar /absolute/path/epubcheck.jar'
```

也可使用 `--epubcheck-command 'java -jar ...'`。本开发工作区已准备便携工具于 `.tools/epubcheck/`，程序会自动发现。对 EPUB 3.0 导航 `<nav aria-labelledby>` 的 EPUBCheck 5.4.0 已知误报，若便携 5.3.0 可用且重新检查全书通过，程序会精确回退到 5.3.0；其他错误仍阻断。工具目录不提交到仓库。没有检查工具、源书有真实 ERROR/FATAL 或输出路径不安全时，不开始付费调用；WARNING 会记录。

## 翻译一本书

```bash
python main.py translate ./book.epub
```

默认使用已配置的 Agnes 模型、32768 上下文、4096 输出上限和并发 2，成品路径为源书旁的 `book-cn.epub`。需要时可显式调整 `--context-tokens`、`--max-output-tokens`、`--concurrency`、`--output` 和 `--provider`。`--http-limit` 统计实际 HTTP 调用，包括重试；默认 0 表示按初始 Unit/衔接计划计算有界上限。达到运行上限会暂停，保留进展；达到单个 Unit 的上限只挂起该 Unit。

旧 `--limit`、`--preserve-fonts` 和 `--engine` 选项已从翻译入口移除。新版自行按上下文预算切片并默认保留字体；`--language` 只接受简中别名并统一为 `zh-Hans`。

默认保留字体、CSS 和二进制资源，不剪枝、不重压图片。默认不覆盖已有输出；需要时显式传 `--overwrite`，仍先验证临时产物。

## 从 JSON 续跑

CLI 会打印工作目录。恢复扫描完整任务清单，补中间失败项，不从“最后成功序号”重新翻译后文。

```bash
python main.py resume ./work/<source_hash>/<run_id> --output ./book.zh.epub
```

- 已接受的目标继续复用；校对失败从校对继续。
- 长 Unit 的成功片段保留，同一 CutPlan 下只补缺片。
- 准备阶段尚未 ready 时，也可从已保存快照继续，不要求原书仍在原路径。
- 未 ready 时若提取器版本已升级，需要新建运行；ready 的运行继续使用原先冻结的文档 JSON。
- 修改源书、词表、模型生成参数或提示版本时新建运行，不混用旧结果。
- `Ctrl+C` 第一次停止新派发并保存返回结果，再次中断可强制停止。

显式重试已经耗尽自动次数的 Unit，并按需要增加额度：

```bash
python main.py resume ./work/<source_hash>/<run_id> --output ./book.zh.epub \
  --retry-unit <unit_id> --add-unit-http 6 --add-run-http 6
```

衔接检查的显式重试入口：

```bash
python main.py resume ./work/<source_hash>/<run_id> --output ./book.zh.epub \
  --retry-check <document_id> --add-check-http 3 --add-run-http 3
```

增加额度会留档，不清空历史计数，也不重开无限修订循环。没有可执行源计划的 Unit 不能靠重试额度解决；报告会保留其准备阶段问题。

## 修订与损坏文档恢复

普通 Unit 的修订文件：

```json
{"unit_id":"u-...","base_revision":0,"plan_epoch":0,"target":"完整的修订后投影"}
```

长 Unit 的 `target` 使用完整的 `item_id → 目标投影` 对象，不能只提交半个 Unit。可以用 `{"items":[...]}` 提交多个修订。`base_revision`、`plan_epoch` 从当前 `units/<unit_id>.json` 获取。

```bash
python main.py resume ./work/<source_hash>/<run_id> --output ./book.zh.epub \
  --repair-file ./repairs.json
```

修订仍须经过本地检查、机器校对和相关衔接复核，不能编辑 `accepted` 字段绕过检查。

如单份 DocumentPlan JSON 缺失或损坏，可明确要求从源快照恢复：

```bash
python main.py resume ./work/<source_hash>/<run_id> --output ./book.zh.epub \
  --repair-document <document_id>
```

只有同版本重建结果与 BookPlan 登记哈希完全相同才恢复；否则需要新运行。正常 resume 不会静默重提取并替换已提交计划，结果记录损坏也不会自动变成零计数或已完成。

## 术语表

使用 `--glossary ./terms.json`。支持原来的 `{"cache":"缓存"}` 字典，也支持明确范围与模式的词条：

```json
[{"source":"cache","target":"缓存","scope":"book","mode":"required","note":"本书计算机语境"}]
```

模式为 `preferred`、`required`、`keep_source`。不要把多义词无条件强锁到一个译法。空词表合法；明确指定但无法加载的文件会报错。运行期间词表固定。

旧的独立 `generate-glossary` 命令已移除。v2.5 的默认自动术语提取和冻结流程按[审定实施计划](docs/epubox_v25_implementation_plan.md)开发；在该任务完成前，此处仅描述当前已存在的用户词表输入。

## 工作目录与结果

```text
work/<source_hash>/<run_id>/
  source.epub
  bookplan.json
  documents/<document_id>.json
  units/<unit_id>.json
  checks/<document_id>.json
  requests/<request_id>.json
  staging/
  publish.json
  report.json
```

文档 JSON 保存原始 HTML/XHTML、源槽位、Unit 和 g/x/b 引用。初始结果先写入，最后提交 ready 计划；之后逐项原子保存。运行结果为：

| 状态 | 含义 |
|---|---|
| `completed` | 所有必需目标/派生值、机器校对、衔接、结构及 EPUBCheck 通过，并提交本次产物 |
| `paused` | 运行总额度、取消或共享服务问题导致暂停 |
| `needs_attention` | 独立可执行工作已处理，仍有局部失败或依赖待处理 |
| `failed` | 不能继续信任输入/存储/程序状态，或发布失败 |

只有 `completed` 返回成功退出码与正式输出路径。其他状态不会导出混入英文的草稿；有效译文仍保存在 JSON。真实阅读器未执行时报告 `not_run`，不会自动伪记通过。

## 历史运行与开发验证

旧 HTML checkpoint 不自动转换成新版 accepted 结果。旧模块当前无法从 CLI 调用，按 v2.5 计划在共享正确性能力迁移后物理删除。

```bash
.venv/bin/python -m pytest -q
.venv/bin/pyright
.venv/bin/ruff check main.py engine tests
```

方案、任务和实际证据分别见 [架构计划](docs/epubox_architecture_plan.md)、[实施进度](docs/epubox_implementation_status.md) 和 [P14 实书验收记录](docs/epubox_p14_acceptance.md)。规范要求 T01–T32 和真实质量/阅读验证；不能把模型替身通过写成实书验收通过。

阅读验收使用成品副本：Calibre 可能把阅读进度写进 EPUB 的 `META-INF/calibre_bookmarks.txt`，从而改变已发布文件的哈希。

## 许可证

MIT
