# 第一阶段验收与独立审查

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
