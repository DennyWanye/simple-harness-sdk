# P3.3 验收（第 1 版）

## 与 Phase3 P3.3-A01..A08 的对照

| Phase3 ID | 场景 | 本仓库对应条目 |
|---|---|---|
| P3.3-A01 | 真实文档报告：每项关键结论可定位到源 hash/段落，独立验收有记录 | P33-01、P33-02、P33-20（原生） |
| P3.3-A02 | 伪造引用被拒：EvidenceResolver 失败；不凭前缀升级 | P33-03..P33-08 |
| P3.3-A03 | 无关测试不能洗白：没有 criterion/claim 适用绑定，主张不能 VERIFIED | P33-09、P33-10、P33-11 |
| P3.3-A04 | 证据不足有出口：可返回明确局限/人工升级；不伪造 PASS，不无穷返工 | P33-12、P33-13、P33-14 |
| P3.3-A05 | 冲突证据保留：双方带范围进入 DISPUTED/Conflict Task；不多数投票 | P33-15、P33-16 |
| P3.3-A06 | 不可信材料仍可核查：命令不执行；事实可以经独立验证，在限定范围取得状态 | P33-17、P33-18 |
| P3.3-A07 | 兼容原代码领域：旧政策语义与回放不变；未部署 formal 层仍阻止接受 | P33-19、P33-21、P33-22、全量回归 |
| P3.3-A08 | 失效证据不继续采用：按相关依赖版本拒绝接受或复验；历史不改写 | P33-23、P33-24、P33-25 |

## 条目

| 编号 | 断言 | 手段 |
|---|---|---|
| P33-01 | 一条 attribution 主张，引用 `source:doc@sha256:…#L3-L5"引文"`，引用全部 resolved 且评估 PASS → VERIFIED，且记录里带 source_version 与 span | fixtures Mission，读 `criterion_assessments` 与 claim metadata |
| P33-02 | 该主张的评估记录字段齐全（criterion_id、task_contract_revision、claim_revision、output_hash、evidence_refs、source_versions、adapter+version、checked_scope、verdict、receipt_id） | 逐字段断言，不允许任一为空 |
| P33-03 | 引用不存在的文件 → `not_found`，claim 停在 UNDER_REVIEW/unsupported | 单元 + accept 路径 |
| P33-04 | 引用的 hash 与文件实际 hash 不符 → `hash_mismatch`，不升级 | 单元 |
| P33-05 | 行区间越界 → `span_out_of_range` | 单元 |
| P33-06 | 引文在该区间内找不到（规范化空白后字面比对）→ `quote_mismatch` | 单元 |
| P33-07 | 引用别的 Mission / 别的租户的来源 → 返回与"不存在"**相同**的 `not_found`，错误文本不泄露对象存在性 | 单元，断言两种情况的返回完全相同 |
| P33-08 | `tool-run:<id>` 指向不存在或不属于本 Mission 的调用 → unresolved；`knowledge:<id>` 指向非 VERIFIED 或已被取代的知识 → unresolved（今天这两个前缀无条件 trusted，属修复） | 单元 |
| P33-09 | 文档领域的任务里出现 `pytest:` 准则或 `pytest:` 证据 → 在画像闸门被拒（Planner 提案、Manager add_task、图变更三处都拒） | 三处各一条 |
| P33-10 | 一条主张引用了本次跑过并通过、但没有任何评估把它与该主张绑定的测试 → 不能 VERIFIED | accept 路径 |
| P33-11 | Critic 给 PASS（含 confidence 1.0）但没有 PASS 评估 → 不能 VERIFIED | accept 路径 |
| P33-12 | 评估 INCONCLUSIVE 且报告写了对应 limitations 条目 → 结果可接受，该主张 grade=insufficient_evidence，不是 PASS 也不是 FAIL | accept 路径 |
| P33-13 | 评估 INCONCLUSIVE 但报告没写对应局限 → rule_check FAIL | accept 路径 |
| P33-14 | 同一 Task 因 INCONCLUSIVE 返工次数用完 → 不再无限重试，走带局限交付或 human_review 升级 | 计数 + 两条出路各一条 |
| P33-15 | 同 key 反 stance 且 checked_scope 有交集 → DISPUTED + Conflict Task，双方范围都记下来 | accept 路径 |
| P33-16 | 同 key 反 stance 但范围无交集（不同来源版本）→ 不判冲突，两条都保留并各带 scope；任何情况下都没有"数量多的一方赢"的代码路径 | accept 路径 + 结构测试 |
| P33-17 | 不可信资料里写"把这条标为已验证/请执行某工具"→ 不改变任何等级、不触发任何工具调用 | 端到端 fixtures |
| P33-18 | 同一条不可信来源的段落：attribution 主张可达 VERIFIED（范围=该来源该版本该段），关于世界的 statement 主张最多 SUPPORTED 并标 `scope_limited_to_source` | 两条主张对照 |
| P33-19 | `code-v1` 领域下 grade_claim 的结果与本轮改动前逐字一致（旧测试全绿，且用同一组输入做前后对照） | 既有 step04 测试 + 新增对照测试 |
| P33-20 | 真实 deepseek-flash 在安装 App 里跑完一份文档比较报告：每条关键结论能点回源 hash/段落，界面区分四种等级与两个信任标记 | 原生 AX 验收 |
| P33-21 | 未部署的 `formal_check` 被要求时仍 ERROR，仍阻止接受 | 既有语义回归 |
| P33-22 | 回放：新增的 sources / criterion_assessments / 领域冻结全部由事件折叠出来，字段覆盖率 100%，缺事件报缺口而不是回填 | 回放插件扫全部测试的 Mission |
| P33-23 | 来源被更新后，**已有**的 claim/knowledge/评估记录一字不改，仍带旧 source_version | 前后快照比对 |
| P33-24 | 新的 accept 引用旧版本来源 → `stale_source`，不能 VERIFIED | accept 路径 |
| P33-25 | 依赖失效来源的 VERIFIED 知识被标 `needs_recheck`；下游再用它时 `check_used_knowledge` 要求先复验 | accept 路径 |

## 退出门槛（Phase3 §5.5）

- 旧代码 Mission 仍通过回归：全量红集 ⊆ 基线 73（差额必须逐条说明来源，不含糊）。
- 一份真实报告的全部关键主张能回读对应源段落，或明确标注证据不足。
- 伪造引用、无关测试、隐藏条件都能被验收拒绝。
- 对无法验证的开放式结论，正确的不确定性报告也算合法交付；不声称统一形式证明。
