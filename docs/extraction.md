# 第二阶段：资源解析、源字节映射与原子提取

范围为 [任务计划](tasks.md) 的 T03 → T04 → T05。代码基线为 feature/preflight@023529b；沿用 [分块要求](chunking.md) 与 [既有交接合同](baseline.md)，没有新增供应商、执行器、模型调用或依赖。

## 可单独调用的接口

| 任务 | 接口 | 结果 |
|---|---|---|
| T03 | engine.epub.parsing.parse_resource(data, media_type) | ParsedResource：raw、text、encoding、kind、tree、diagnostics |
| T04 | engine.epub.ranges.index_resource(parsed) | ResourceIndex：原始字节、Element-only 路径索引、NodeSpan、SlotSpan |
| T04 | index.bind_document(document) | SourceMap：实际节点整元素范围、源字符/字节位置、保护区间 |
| T05 | engine.item.atoms.extract_resource(data, path, source_hash, media_type, config) | AtomicDocument：公开 DocumentPlan、SourceMap、按阅读顺序的 AtomicItem |

输入为源资源的原始 bytes，source_hash 为整本固定快照身份。AtomicDocument 使用 epubox-atoms-1，可由已有 canonical_json_bytes/parse_contract 保存和回读；原始资源不复制进每个原子 JSON，仍位于源快照。DocumentPlan.resource.source_sha256 与 SourceMap.document_hash 都是该原资源的字节 SHA；BookPlan.document_hashes 仍是各已保存计划 JSON 的 SHA，两者不能混用。

旧 extract_document(str, ...) 保留既有版本和源计划语义，用于已有 JSON/已付费记录的兼容。新 raw-byte 提取器为 epubox-atomic-1，adapter 为 epubox-bytes-1；生产准备管线改用新接口归 T15，不通过隐含 CLI 选项切换行为。

## T03 的分类与拒绝条件

- XHTML、NCX、OPF 按真实根 QName 的 namespace URI 和 local name 识别，XML 标签大小写和前缀原样保留。
- text/html 中的真实 XHTML 根仍使用严格 XML；注释、脚本中的伪 xmlns/html 字符串不能改变资源分类。
- 真正 HTML 保留原 bytes 并给出 tree=None 与明确诊断；T04/T05 无可靠映射便拒绝。未引入 html5lib、recover 或 HTML→XHTML 自动转换。
- 严格解析保留注释、PI、CDATA 和结构空白，支持 UTF-8、UTF-16LE/BE、BOM、CRLF 与明确的编码声明。声明与实际编码不匹配、未知编码、损坏 XML 清晰失败。
- 标准 XHTML DTD 只使用既有受控离线实体定义；自定义内部 DTD、参数实体、任意文件/网络外部实体拒绝。解析不联网、不读取任意实体文件。
- 未识别用途的 XML 仅按严格 XML 保存，不能仅凭出现英文或名为 title/p 的本地标签就自动翻译机器字段。

## T04 的定位保证

Expat 给出实际 UTF-8/UTF-16 字节事件位置，lxml 提供语义树；两个解析结果的 QName 与 element-only 路径必须一致。NodeSpan 登记整元素、起始标签、内容、结束标签范围；自闭合元素允许空 content/endtag，但完整元素范围不为空。

SlotSpan 把 lxml 的已解码 text/tail/attribute 字符对应到同一份原字节。实体、CRLF、命名空间、引号中的 >、注释/PI 的 tail 和空属性/空 CDATA 都有专门验证。CDATA 内的 CR/CRLF 也按 XML 规则对应到 LF，原字节仍不变。

SourceMap.node_spans 可嵌套，表示完整源元素，不是可直接提交的重叠编辑集合。SourceMap.locations 只登记属于翻译项的实际字符范围；protected_spans 是这些可编辑范围之外原字节的完整补集。head 中 title/指定 description 的白名单位置单独登记，其余 CSS/JS、属性、标签与资源路径仍受保护。

以下情况明确拒绝：路径/QName/源字符不一致、范围越界或错误重叠、切入一个多字符实体、一个待编辑字符范围跨越无法保持的 CDATA 语法。不能退回整文档序列化。index.replay() 是零编辑原字节重放，不代表 T07 的中文回填已经完成。

## T05 的所有权与覆盖

最外层 p/table/em/i/ul/ol 一次拥有完整翻译范围；后代只有 g/x 等已有结构引用，不重复成为正文任务。整表不会以 tr/td/th 分项，列表不会以 li 分项；p 内的 em/i 与整个 p 同项，独立 em/i 则是自身完整项。纯保护内容和空白不会生成空翻译项。

普通包装元素沿完整子元素下钻，parent.text、子元素和 child.tail 都登记。虚拟文本块的源位置按原字节排序，不能把父节点的末尾文字错误放在子节点前。SourceSlot 所有权、primary source views 和 SourceRef 都复用既有校验，保存后可重放源证据。

translate=no 的继承与后代 translate=yes 的岛保留，岛仍属于已有外层原子；完全保护的代码、媒体或未知外部命名空间子树不产生后代任务。img 的可译属性独立登记；父正文项同时覆盖该节点时，region.patch_owner_id 记录 T07 合并为唯一补丁所需的归属，不提前做目标回填。

XML 白名单按 [基线合同](baseline.md) 固定：XHTML head/title 与 meta name=description 的 content、正文可译属性；NCX docTitle/text 与 navLabel/text；OPF 的明确主书名和纯文本简介。作者、identifier、manifest/spine、路径、ID、foreign namespace 同名字段保留。

虚拟块的 region.safe_boundaries 只保存允许候选的 slot_id、char_offset、byte_offset，使用既有保守句界规则并检查段落空白、字符簇和源实体边界；不在原子项或配对 inline 范围中登记切点。实际在配置预算内选择分组归 T08/T14。这些始终是源位置，不能拿来按原文偏移截断译文。

AtomicDocument 验证完整清单和阅读顺序、source/map 身份、所有 source-owned 范围的字节映射、通道一致及硬原子的整元素范围；缺项、重复后代正文任务或把完整 p 伪改成局部范围都会失败。

保存合同还核对完整原字节覆盖、每个映射位置的 node/field/attribute 与源槽位一致、实际可译字节均位于所属 item/node 范围中。属性项精确等于其实际属性范围，patch_owner 必须拥有该节点或 registry 引用；普通内容范围不可互相重叠。扩大属性范围并同时删掉父补丁归属不能绕过检查。

## 文件职责与后续交接

原 1592 行 structural_extractor.py 拆为 structure.py、metadata.py、projection.py、policy.py；source_views.py 改为 views.py。结构算法、标记语言和证据视图继续复用，所有 Python 引用同步。每个本阶段交付文件均为单词命名并低于 1000 行，完整整改状态见 [files.md](files.md)。

后续 T06 完成目标占位符/完整保护验证；T07 用原字节与已验证目标做局部回填；T08 在付费调用前根据这些完整原子及安全虚拟切点检查预算；T15 再将新源计划与准备/恢复管线接通。不能用本阶段的原文重放代替中文回填和最终 EPUB 的验收。

最终测试与独立审查记录见 [任务进度](tasks.md) 和 [审查记录](review.md)。测试只使用隔离夹具，不移动或修改真实书籍断点。

验收结果：**481 tests passed，47.55 秒**；Ruff、格式检查、Pyright 与 diff 检查通过；两条独立审查分别为 APPROVE 和 CLEAR。
