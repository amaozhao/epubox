# v2.5 T01—T60 验收矩阵（实施目标）

与 [实施计划](epubox_v25_implementation_plan.md) 一起使用；这些是**待实现/待执行的验收节点**，不是当前已通过的成绩。每行“节点”都表示计划中的测试路径与函数，执行命令为 `uv run pytest -q <节点>`；非模型测试采用仓库工厂生成的小 EPUB/JSON fixture，模型替身不得充当真实模型证据。原已有 v23 用例在协议迁移后改写到下列 v25 节点，不因删除旧测试而丢失断言。

| 用例 | 节点（命令参数） | fixture / 故障 | 必须观察到的结果 |
|---|---|---|---|
| T01 | `tests/v25/test_source.py::test_t01` | `mixed_text_tail` | 源槽位全归属，identity 无吞字/重复 |
| T02 | `tests/v25/test_inline.py::test_t02` | `same_label_reorder` | 相同链接/强调改序仍按身份装配 |
| T03 | `tests/v25/test_inline.py::test_t03` | `bad_marker_matrix` | 坏标记只拒对应候选，后续 Unit 保留 |
| T04 | `tests/v25/test_inline.py::test_t04` | `escaping_and_truncated_json` | 字面标记/非法字符安全处理，坏 JSON 不取半截 |
| T05 | `tests/v25/test_source.py::test_t05` | `protected_code_tail_alt` | 保护块不变、tail 一次、alt 作用域正确 |
| T06 | `tests/v25/test_inline.py::test_t06` | `break_page_noteref` | 硬边界保留，引用绑定单审 |
| T07 | `tests/v25/test_inline.py::test_t07` | `css_first_child_unknown_rule` | 样式域锁序安全，无关属性不变任务 |
| T08 | `tests/v25/test_inline.py::test_t08` | `long_nested_marker_segments` | 长链接虚拟边界重组一次、不复制 ID |
| T09 | `tests/v25/test_planner.py::test_t09` | `review_budget_replan_crash` | 校对预留、重规划术语哈希重绑定，旧结果不混 |
| T10 | `tests/v25/test_review.py::test_t10` | `semantic_negation_cases` | 结构/模型检出分开记录，不能以机器通过代人评 |
| T11 | `tests/v25/test_review.py::test_t11` | `wrong_link_binding` | 结构合法错绑被语义复核暴露 |
| T12 | `tests/v25/test_review.py::test_t12` | `replacement_regression` | 重大 issue 不晋级，新目标完整复核 |
| T13 | `tests/v25/test_review.py::test_t13` | `stale_revision_checks` | 迟到结果拒绝，衔接过期，修订有界 |
| T14 | `tests/v25/test_glossary.py::test_t14` | `empty_bad_polysemy` | 空词表合法，坏用户文件拒绝，多义不误锁 |
| T15 | `tests/v25/test_transport.py::test_t15` | `batch_order_rate_limit` | 缩批乱序归属正确，429/超时累计计数 |
| T16 | `tests/v25/test_store.py::test_t16` | `atomic_lock_config` | 半写/串书/配置变化/锁冲突安全 |
| T17 | `tests/v25/test_publication.py::test_t17` | `mutated_assembled_text` | 删中文字/复制 tail/改 href/漏 alt 均拒绝 |
| T18 | `tests/v25/test_publication.py::test_t18` | `same_id_cross_doc` | 相对链接与非 spine 尾注不误报不漏 |
| T19 | `tests/v25/test_publication.py::test_t19` | `derived_nav_revision` | 标题修订后导航按当前有效版本派生 |
| T20 | `tests/v25/test_publication.py::test_t20` | `epub2_epub3_boundaries` | 容器版本/字体混淆/不支持关系正确判定 |
| T21 | `tests/v25/test_publication.py::test_t21` | `old_output_crash_commit` | 旧文件不伪成功，提交中断可复核 |
| T22 | `tests/v25/test_live_acceptance.py::test_t22` | `real_reader_evidence` | 实书译本结构/阅读器/人工质量和费用字段真实记录 |
| T23 | `tests/v25/test_orchestrator.py::test_t23` | `middle_fail_later_progress` | 并发1/2局部失败后同章及后章继续并落盘 |
| T24 | `tests/v25/test_planner.py::test_t24` | `s1_success_s2_fail_s3_success` | 同CutPlan只补s2，术语子集不重算漂移 |
| T25 | `tests/v25/test_transport.py::test_t25` | `partial_batch_bad_json` | 局部合法项保存，整体截断批拒绝且其他批继续 |
| T26 | `tests/v25/test_source.py::test_t26` | `json_only_identity` | 全部 document JSON 重载可 identity 装配 |
| T27 | `tests/v25/test_orchestrator.py::test_t27` | `middle_hole_last_success` | 全清单补洞、已成功译文/校对复用 |
| T28 | `tests/v25/test_orchestrator.py::test_t28` | `unit_chapter_global_budget` | 局部限额继续，全局限额暂停，历史不归零 |
| T29 | `tests/v25/test_orchestrator.py::test_t29` | `derived_dependency_wait` | 等待不占worker/额度，补齐后局部解除 |
| T30 | `tests/v25/test_store.py::test_t30` | `three_stage_crash_corruption` | parsed_ready/提取/ready 恢复与损坏隔离 |
| T31 | `tests/v25/test_planner.py::test_t31` | `concurrent_segment_delta` | record_version 进度合并、revision 迟到拒绝 |
| T32 | `tests/v25/test_orchestrator.py::test_t32` | `drain_with_blocked_work` | 无ready任务退出needs_attention，完整进度保留 |
| T33 | `tests/v25/test_resume.py::test_t33` | `read_only_plan_twice` | 两次预览源/状态字节不变、零HTTP |
| T34 | `tests/v25/test_resume.py::test_t34` | `unknown_external_output` | 未知/手改译文不自动认领完成 |
| T35 | `tests/v25/test_inline.py::test_t35` | `zero_negative_context` | 零摘录为空、负数拒绝、跨文档/标记不串 |
| T36 | `tests/v25/test_glossary.py::test_t36` | `alias_scope_boundary` | C++/.NET、cat/category、casefold、多义边界正确 |
| T37 | `tests/v25/test_planner.py::test_t37` | `over_fifty_terms_budget` | 不静默截词，缩批/重规划或局部挂起 |
| T38 | `tests/v25/test_glossary.py::test_t38` | `structured_rule_hash` | 分隔符无歧义，note/mode/alias 敏感，有序数组不乱排 |
| T39 | `tests/v25/test_store.py::test_t39` | `freeze_and_repeat_response` | 外部词表变化无效，响应幂等、HTTP 不漏 |
| T40 | `tests/v25/test_publication.py::test_t40` | `resource_multiset` | 同src图片、文字伪img与多项错误准确分类 |
| T41 | `tests/v25/test_publication.py::test_t41` | `mtime_existing_output` | mtime/旧文件/构建False不伪成功 |
| T42 | `tests/v25/test_publication.py::test_t42` | `old_format_json_reassembly` | JSON重装配；旧-1/-2明确unsupported |
| T43 | `tests/v25/test_glossary.py::test_t43` | `default_auto_extract` | 无用户词表仍有全书窗口与候选记录 |
| T44 | `tests/v25/test_source.py::test_t44` | `late_table_note_views` | 后半书/表格/尾注/跨格式覆盖，硬保护不扫 |
| T45 | `tests/v25/test_glossary.py::test_t45` | `false_quote_wrong_view` | 虚构/hint-only/错view/未知ID候选被拒而其他有效 |
| T46 | `tests/v25/test_glossary.py::test_t46` | `user_precedence_overlap` | 用户preferred/required优先，部分重叠不绕过 |
| T47 | `tests/v25/test_glossary.py::test_t47` | `polysemy_resolution_defer` | 多义分scope、一次核对、不明延期 |
| T48 | `tests/v25/test_glossary.py::test_t48` | `overlap_repeat_frequency` | alias/证据/上下文重复不加倍 |
| T49 | `tests/v25/test_store.py::test_t49` | `paid_terms_without_bookplan` | 准备期已收费、无bookplan恢复不重付 |
| T50 | `tests/v25/test_orchestrator.py::test_t50` | `failed_window_later_success` | 中间术语失败后续继续，closed_with_gaps可冻结 |
| T51 | `tests/v25/test_orchestrator.py::test_t51` | `global_pause_terms_pending` | 账户/取消/总额度暂停不提前冻结 |
| T52 | `tests/v25/test_store.py::test_t52` | `freeze_glossary_book_crashes` | 各提交点重放同一快照、不重复提取 |
| T53 | `tests/v25/test_source.py::test_t53` | `document_immutable_after_freeze` | 冻结后DocumentPlan字节不变，词条仅在结果计划 |
| T54 | `tests/v25/test_review.py::test_t54` | `term_suggestions_idempotent` | review-2可选建议幂等落UnitRecord，不改词表 |
| T55 | `tests/v25/test_review.py::test_t55` | `known_wrong_term_block` | 重大错义issue阻断，不能仅写建议放行 |
| T56 | `tests/v25/test_transport.py::test_t56` | `term_window_group_limits` | 窗/冲突/总额度及传输重试累计不清零 |
| T57 | `tests/v25/test_glossary.py::test_t57` | `file_hash_vs_rules_hash` | 证据/频次只改文件hash，note/mode/target改规则hash |
| T58 | `tests/v25/test_glossary.py::test_t58` | `disabled_empty_gap_status` | disabled/not_required/closed空/closed_with_gaps分清 |
| T59 | `tests/v25/test_planner.py::test_t59` | `context_scope_many_terms` | 他章context不变成本段硬约束，>50词不截 |
| T60 | `tests/v25/test_live_acceptance.py::test_t60` | `technical_book_full_flow` | 真技术内容术语准备、恢复、翻译、重建、人评/费用分证据 |

## 真实书与外部验收

- **EPUB2**：`work/v23-acceptance/p14/the-cask-of-amontillado.epub`，Project Gutenberg #1063 美国公版，SHA256 `4e4e64ae9b36a40c5476737b3a585fb7eaf9a596ed7867b10fd3ff40fecd3327`；已有同模型历史自动与代理修订结果只作旧基线，不重启被删除的旧引擎。
- **EPUB3 技术内容**：`work/v23-acceptance/p14/technical-excerpt/source.epub`，源自用户本地技术书的13个原DOM子树、代码/表格/脚注/复杂内联，SHA256 `b447b9de1699e7b732b7c63c28fc177da65670b7c1b7133e472e943792a6accf`。只作本地评测，不发布译本。
- **用户指定的 EPUB3 大书**：`/Users/amaozhao/Downloads/epub/leadership-algorithm-intelligent-systems.epub`，SHA256 `c5173f54fba33e04187e526955d050e8337161f281ba1d0209b6555e387c9f2d`，约6736 Unit、37表；先做全书源视图/术语覆盖与无模型装配，再按显式运行 HTTP 硬限额真实翻译。书籍只在本地测试，不进入Git。

运行配置以 `work/v25-acceptance/config.json` 固定：Agnes `agnes-3.0-flash`、context 32768、output 4096、并发2；公开书/技术摘录各先设 120 HTTP 硬限额，大书先设 120 HTTP 的术语准备审计上限。任何追加额度均使用公开CLI参数留档；不自动扩大预算。请求日志逐次累计 HTTP、已知输入/输出 tokens、usage 未知次数与可靠费用（无价格则 null）。原书和产物用便携 EPUBCheck 5.4.0；已知 `nav aria-labelledby` 误报的 EPUB3.0 书只在同源 5.3.0 完整通过时启用已实现的精确回退，记录实际Java/JAR命令与版本。

T22/T60 的 `test_live_acceptance.py` 节点只校验上述本地证据索引/哈希/字段，不替代真人判断。固定源句在模型前写 `work/v25-acceptance/fixed-passages.json`；原始自动结果、代理修订、人评四维（忠实度/自然度/术语/格式）分别写 `automatic.json`、`agent-repairs.json`、`blind-review.md`。完整技术摘录走一次术语准备→翻译→JSON恢复→正式EPUB→真实EPUBCheck；Calibre ebook-viewer 9.15.0 通过成品**副本**检查目录、表格、代码与脚注，原成品 SHA 保持不变。用户提供的大书在受限额度内仅报告已覆盖比例和缺口，不以其局部结果声称整书翻译通过。

真实质量结果写入 `work/v25-acceptance/report.json`，仓库摘要写 `docs/epubox_v25_acceptance.md`。若模型因账户/预算暂停，必须以 paused 留档且不把剩余术语窗口转成 closed_with_gaps；若人评或阅读器检查未取得，只报告代码/机器证据已完成，不把 v2.5 质量门槛标为通过。
