# 第三阶段：标记验证、局部回填与术语源窗口

范围：[任务计划](tasks.md) 批次 3 的 T06/T09 → T07/T10。代码基线为 feature/preflight@4546ccd；复用 [源提取合同](extraction.md)。不增加模型供应商、依赖或执行器，不操作真实书籍断点。

## 独立接口

| 任务 | API | 交付 |
|---|---|---|
| T06 | engine.item.inline.validate_item_target(item, target) | 根据当前 AtomicItem 验证 target，返回结构化 Event |
| T07 | engine.epub.fill.fill_resource(raw, inventory, targets, identity=False) | 原始资源 bytes + 完整按 item_id 对应的 target → 安全回填后的 bytes |
| T09 | engine.services.terms.inputs.load_atomic_terms(path, documents) | 标准化 UserTerm tuple 与确定性 hash |
| T10 | engine.services.terms.planning.plan_atomic_terms(inventories, terms, ...) | 具有固定输入身份、全 primary 覆盖及前文范围的 TermExtractionPlan |

这些接口可以用隔离夹具单独测试；生产准备、派发、逐阶段保存和 EPUB 出版的接线继续由 T15/T11/T17/T18 负责。

## T06：标记与完整保留对象

继续使用既有 g/x/b 语言。g 表示完整内联范围，x 表示完整保留对象，b 是已有固定边界。未知、缺失、重复、错配、跨父节点/边界及 XML 非法字符被拒绝；不同 item 可以各有本地 x1，批次必须按 item_id 对应，不能用标记名称替代项身份。

原书中类似 ⟦+g1⟧、⟦=x1⟧ 的字面文本转为 x，对象具有 boundary_type=literal_marker、slot_id/start/end 和原字面值。目标不能删除、改写或跨越这些字面边界。

正文的 literal 范围按 protected 所有权登记；属性仍以整属性为单位保持原有 owned span，同时通过 x 固定其中的字面片段。primary 证据视图跳过属性 literal，保留可译部分的真实 SourceRef。T07 在属性中按事件复制 x 的原字节，不能把整属性再次编码而改写字面实体。

代码、CSS/JS、SVG/Math、媒体及纯保护项不成为大段模型正文；原子项不会因标记生成被拆开。真正的精简 HTTP 投影仍由 T13 构造，不能把本地 registry 全源码作为模型输入。

## T07：原始字节局部回填

回填前必须重读并验证 AtomicDocument，核对完整 target ID 清单和非空字符串、原 bytes 的 SHA，重建 SourceMap，重算每项源范围。registry 也需重放真实 DOM parent、节点/槽位、原始顺序、原字面值及所属项原字节范围；篡改成另一个段落的 code 或 literal 槽位会失败。

目标只经过已有事件验证和原字节模板渲染：g 的原起止标签、x 的原子树/源片段、注释/PI 的原范围从快照复制；text 根据原编码作正确 XML 转义。不能用全局字符串替换或整文档 lxml 序列化。

独立属性按 region.type 判定，包含 metadata 通道的 head description content。保留原属性引号，文本正确转义引号、实体、换行；未修改属性的实体写法逐字节保持。父 p 与 img alt 使用同一父补丁合成，其他独立补丁按最初源偏移一次拼接，重叠或越界直接拒绝。

UTF-8/UTF-16、BOM、未修改 CRLF 和未登记 head/CSS/JS 保留。单纯 root 或 g 叶节点的 CDATA 保留原壳，目标包含 ]]> 时合法分段；无法可靠组合的 mixed CDATA 明确 FillError，不能静默消除 CDATA。输出重新严格解析并核对元素、注释与 PI 清单。

identity=True 也先完成全部输入/来源/目标验证；只有每个目标等于源 projection 才能逐字节返回原资源。缺项、外来项、坏标记、错来源不会因恒等模式绕过检查。此 API 不发布文件，不代表 T18 最终 EPUB 已完成。

## T09：用户术语输入

对象映射和列表输入保持 preferred/required/keep_source、aliases、match_policy、scope 和完整 note。keep_source 未指定 target 时使用 source；未知字段、错误类型、越界作用域或重叠作用域中的冲突译法拒绝。

新原子 API 按实际 DocumentPlan/Unit ID 校验作用域，精确保留 note 首尾空白和 Markdown 缩进并计入 hash；返回冻结模型和确定性顺序。原有 load_user_terms 继续旧规范化/hash 语义，不悄悄改写已有任务的用户副本。引用专有名词可通过 keep_source 保留。

## T10：术语视图与窗口

新 strategy 为 epubox-term-planner-3，旧 v2 API/读取继续保留。默认 max_primary_chars=12000 是字符数，不是 token；primary 范围在字符簇边界分组并全覆盖、不重复。术语纯文本窗口可以切开长 p/table 的源视图，正式 AtomicItem 始终不变。

不同文档与 body/table/note/attribute/metadata/navigation 通道不串。每个提取窗口只附带当前起点前最近最多两个同通道片段，各≤400字符，无未来片段或祖先背景；长视图后续窗口可用当前 view 起点前的范围作最近前文，与当前 primary 严格不重叠。

primary 用户规则与 context 用户规则分别登记；context 不成为当前目标的硬约束或 primary 证据。item_id 只绑定 strategy 与 primary 源范围，提取输入 hash 另绑定前文、规则和模型输入身份。

T11 将多个提取项合入一次 HTTP 时，encoder 还必须按整个候选批次去重并共享最多两个前文，不能直接拼接各项 context_ranges；本阶段不修改付费执行器。

## 验收与文件约束

标记、回填、术语作用域/窗口、UTF-16/CDATA、保护字节和篡改反例均有可运行测试。相关实现与测试为单词文件且各≤1000行；词表模块移入 terms/，测试目录以固定 __init__.py 隔离发现。已同步原测试改名与 import，完整清单见 [files.md](files.md)。

最终全量结果和独立审查见 [tasks.md](tasks.md) 与 [review.md](review.md)。下一批次的 T08/T11/T12 继续处理付费前预检、术语执行与冻结。

验收结果：全量 **505 passed（49.81 秒）**；Ruff、格式检查、Pyright、diff 检查通过；代码审查 APPROVE、架构审查 CLEAR。
