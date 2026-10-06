# 阶段验收与独立审查

范围：feature/preflight 的 T00、T01、T02；需求见 [tasks.md](tasks.md)，固定来源和接口见 [baseline.md](baseline.md)。

## 结果

- 全量 pytest：428 passed，49.72 秒；实施前基线为 375 passed。
- Ruff：All checks passed；Pyright：0 errors、0 warnings；git diff --check 通过。
- CLI translate --help 正常；所有需求/历史文档相对链接有效。
- 本阶段文件单词命名和最多 1000 行检查通过；既有后续任务文件的整改责任完整登记于 [files.md](files.md)。
- 独立 code-reviewer：APPROVE；独立 architect：CLEAR。两位审查者分别重跑目标测试并检查修改，最终全量验证覆盖修正后的代码。

## 审查问题与修正

| 原问题 | 修正 | 回归证据 |
|---|---|---|
| items/messages 可以分别来自不同请求；预算可以伪造、错阶段或已超限 | measure_budget 只接受完整 payload；消息由既有 request_messages 派生；严格 BudgetResult；RequestBatch 检查 stage/fits、原子来源、共享 context 与 review 目标 | budget.py/bridge.py 测试拒绝伪预算、失败预算、错阶段、源内容变化、错误目标及 review 预估派发 |
| wire 身份未包含系统提示和输出额度 | 复用 runtime.wire_hash(stage, payload, output_tokens)，没有另造同名口径 | 载荷、系统提示和 completion cap 变化都改变预算身份 |
| PreparedInput 可以混搭准备文件、词表或翻译/提取配置 | 核对准备/词表 canonical hash、翻译配置相等、提取配置 hash、源/run/freeze/文档/单元/计划身份 | 四类篡改、身份变化、缺失计划与预检失败均拒绝 |
| CLI 首次哈希的锁与稍后稳定快照身份可能错位 | expected_source_hash 只用于运行期，在创建新身份目录前核对，清理临时快照；失败报告使用首次哈希 | 单次源变化后无新哈希任务、无 HTTP，原计量字节不变 |
| 子任务符号链接可能被跳过而新建任务 | 发现与迁移阶段明确拒绝子 run 链接；显式 work root 在解析前拒绝悬空链接 | 不构造/调用模型、不移动断点、不改变旧记录 |
| README 默认目录/成品命名及文件清单不一致 | 更新为源书旁同名目录和 -cn.epub；修正夹具扩展名和新增文件登记 | 文档链接及阶段文件约束检查通过 |

## 交付边界

源码职责拆分保持旧模型 JSON Schema 与公开导出兼容；原始 JSON 格式及已有计数不变。本阶段没有恢复新 workflow 执行入口、实现原子提取/回填/合批或提前接通 --limit，这些均有后续责任任务。

预算分词不匹配使用明确标记的估算及冻结余量；分词器不可用拒绝计算。完整 output 预留放不下时拒绝，不能借截断完成。

迁移仅做持锁的同文件系统 rename。跨文件系统、竞争断点和身份变化明确报错；测试只操作临时目录，未迁移真实用户资料。freeze 文件 SHA、源映射实际生成与磁盘 ready 门由对应后续任务负责，不能凭构造值对象跳过。

## 第二阶段 T03—T05 验收

基线 023529b。全量 pytest **481 passed（47.55 秒）**；Ruff、格式检查、Pyright（0 errors/0 warnings）与 diff 检查通过。独立 code-reviewer 为 APPROVE，独立 architect 为 CLEAR。

实现与交接详见 [extraction.md](extraction.md)。没有增加依赖、供应商或执行器，没有付费模型调用；新提取接口的生产接通归 T15。

| 审查反例 | 最终修正及回归 |
|---|---|
| 良构 HTML fragment 或必需 EPUB 资源 namespace 不符时静默 0 项成功 | text/html 仅已证明的 XHTML 走 XML；其他给诊断；原子入口在 mapping 前拒绝诊断，XHTML/NCX/OPF 错 namespace 均拒绝 |
| str XML 入口仍接受自定义内部实体 | str/bytes 同 DTD guard；既有安全输入和 UTF-16 已解码声明兼容 |
| 删除 protected spans、伪造 location 为属性、降低 p 原子类型 | 完整字节覆盖、权威槽位字段检查；硬原子从 QName/实际源字段推导并拥有整元素范围 |
| virtual 或属性 span 指向无关原字节 | actual mapped 字节必须属于声明 span；span 还必须在原节点范围内；普通内容范围禁止重叠 |
| 扩大属性 span 并同时从两对象删除 patch owner | 属性精确对应唯一 SourceSlot/实际范围；父补丁由真实节点/registry 与包含关系验证 |
| 空 alt、空 CDATA、长 UTF-16 CDATA+CRLF 的映射遗漏 | 保留空 lexical slot，不生成空项/零长编辑；CDATA 行结束按 XML 规则对应，原文重放逐字节相同 |

SourceMap.node_spans 可自然嵌套，是源结构记录，不能直接当作提交补丁；T07 使用原字节和已验证目标实现真正中文回填，不能以零编辑重放冒充回填验收。

## 第三阶段 T06/T07/T09/T10 验收

基线 4546ccd。全量 pytest **505 passed（49.81 秒）**；Ruff、格式检查、Pyright（0 errors/0 warnings）和 diff 检查通过。独立 code-reviewer 为 APPROVE（0 issues），独立 architect 为 CLEAR。

交接 API、兼容和后续接线边界见 [translation.md](translation.md)。回填只返回 bytes、不发布文件，术语规划不调用模型，没有增加新依赖或执行器。

| 审查问题/反例 | 修正与验证 |
|---|---|
| 长 table/p 同一视图拆为多个术语窗口后，后续窗口丢失最接近的前文 | 将 current.start 前同视图的字素完整≤400字符范围作最近前文；与其他同通道前文合计≤2，不与 primary 重叠 |
| 负责文件仍用 test_inline_planner/test_assembly/test_quality 等复合名称，旧链接和清单未同步 | 完成单词改名、imports 与发现同步；inputs/planning 移入 terms/；相对文档链接和阶段文件规则测试通过 |
| 字面控制标记或保护对象可被 model target 破坏/跨项引用 | source literal x、完整 registry/event 边界检查与 item_id 配对回归均通过 |
| 保存 registry 指向另一项 code/slot，可恢复错误来源 | 回填前验证 source parent、slot/subtree、原字面值、原顺序和当前 item 的实际字节范围；跨项重定向拒绝 |
| head description quote、嵌套 img alt、实体或 CDATA 在回填时损坏 | 属性按 region.type 处理，父子合并唯一补丁；未改实体原字节复制；root/g叶 CDATA 保壳、]]>合法编码，mixed不确定输入明确拒绝 |

本批次完成单独可测试的中文局部回填。生产准备、HTTP 批次共享上下文、逐阶段保存和最终 EPUB 出版分别继续由 T15/T11/T17/T18 接通；不能将独立 bytes 回填测试声称为整本书已翻译出版。
