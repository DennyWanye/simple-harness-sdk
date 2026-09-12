# P3.3 非代码 Mission 与证据闭环 · 计划（第 3 版）

- 版本沿革：第 1 版 → 第 2 版吸收第 1 轮两位独立评审（A 威胁模型 READY_WITH_CHANGES、B 代码一致性 NOT_READY）；第 2 版 → **第 3 版**吸收第 2 轮复评（A、B 均 READY_WITH_CHANGES）。四份评审原文在 `reports/`，逐条处置表在 §7。
- 依据：Host `plans/taskSys2/agent-orchestrator-phase3-plan.zh-CN.md` §5（P3.3）与 §11 的 P3.3-A01..A08。
- 理论定义（写代码前已查）：`04_tree_search_blackboard_memory.md` §9 **VERIFIED = 通过机器验证或可靠规则**、§10 知识必须带 provenance；`10_concurrency_conflict.md` §7 冲突不投票而是开 Conflict Task、§8 知识状态不是 True/False。

## 0. 本轮的中心断言

**文档领域的 VERIFIED 只意味着一件完全机器可判的事：「这份文件的这个版本的这几行里，逐字写着这句话」。**

第 2 轮复评指出第 2 版的断言**为假**——因为被约束的只有 `content`，而这条知识在系统里的"意义"由 `key` / `stance` / `supersedes` / `contradicts` 决定，它们仍是模型自由填的；而且用户实际拿到的交付物是 Worker 写的报告正文，与 claim 之间没有任何绑定。第 3 版要让这句话在**三个层面**同时成立：

1. **记录层**：`content` 由系统拼，`key` 由系统构造，`stance` 固定，`supersedes`/`contradicts` 受约束。
2. **消费层**：下游 Agent 看到它时带着"这是来源原文，不是本系统的结论，也不是指令"的标记，排序权重不高于 SUPPORTED。
3. **交付层**：报告的**结论区由系统按 claim 渲染**，Worker 的自由文字只能出现在明确标注为"分析 / 非结论"的章节。

## 1. 用户场景（Phase3 §5.1）

用户给几份带版本的真实文档，要求比较方案并写报告。系统：Planner 拆成资料核查 / 比较 / 整合 → Worker 引用来源 → Critic 核对遗漏与矛盾 → 领域检查与必要的人工审阅 → 只把符合既定依据的结论写进正式知识 → 交回一份每条关键结论都能点回原始来源段落的报告；确实证据不足的地方明确写成局限。

## 2. 现状与差距（读代码得到；四份评审逐条核对，全部属实）

| 现状 | 位置 | 差距 |
|---|---|---|
| Claim 分级只有一条 VERIFIED 路径：`pytest:` 证据被本次 `code_test` 跑过通过 | `memory/claims.py:142-187` | 文档任务没有合法的 VERIFIED 路径 |
| `covering_target` 是**纯路径前缀匹配**，与 claim 内容零关系 | `memory/claims.py:106-119` | 正是 §5.4 禁止的"测试名称/路径匹配不等于逻辑内容关联" |
| `tool-run:`、`knowledge:` 无条件 trusted | `memory/claims.py:80-81` | 没有任何"真去解析"的动作 |
| `rule_check` 只核 artifact 路径与 hash、`file:` 准则、used_knowledge | `verification/deterministic_checks.py:100-172` | 没有引用完整性、来源 hash、覆盖核对 |
| 冲突判定只比 `key` + `stance` | `verification/conflicts.py:51` | 不记适用范围 |
| `contradicts` 是模型自填 id，命中即冲突，对方 SUPPORTED 被强制打成 DISPUTED | `conflicts.py:47-49`；`commit_service.py:1663-1666` | **免费的降级通道**：不提供任何证据就能打掉别人的结论 |
| `supersedes` 只校验"存在 / 同 Mission / 是 VERIFIED"，**不校验 key 相关性** | `commit_service.py:1532-1547` | 一条廉价的 VERIFIED 可以退役任意一条无关知识 |
| `KnowledgeRecord` 的 `key`/`stance` 直接抄自 claim；`knowledge_view` **没有 trust 字段**，而 UNVERIFIED 候选是有 marker 的 | `commit_service.py:1585-1586`；`retrieval.py:310-330` vs `:277` | 不可信来源原文能以 VERIFIED 身份进下游 prompt |
| **系统模板硬编码 pytest（六处）**：`conflict_task` 准则、`CONFLICT_POLICY`、synthesis 默认政策、`check_arbitration` 的前缀与目录约束、`_open_conflict` 的 `code_test` 部署闸门、Arbiter/Worker 角色模板与 `context_builder` 文案 | `manager.py:30,68,96`；`deterministic_checks.py:88-99`；`commit_service.py:1735`；`role_templates.py:121,125,166`；`context_builder.py:207` | 文档领域的冲突与整合必然失败；纯文档部署里**每个冲突都开不出仲裁任务** |
| `_protected_seed` 只保护 `tests/` 与 `pytest:` 目标，数据源只有 `final_report["workspace_seed"]`；验证副本写入顺序 seed → inputs → **artifacts** → protected | `event_handler.py:2407-2422`；`workspace.py:318-343` | Worker 的 artifact 会覆盖验证副本里的来源 |
| `workspace_seed` 是 `Mapping[str, str]`，受 512KB 限制，且 Mission 创建后不可变 | `workspace.py:42,140-150`；`api/missions.py:60-66` | 没有来源字节的权威副本；一个 Mission 内来源根本不会变 |
| Mission 级判定树只挂 `workspace_seed` + 已接受 artifacts，**不含 inputs** | `event_handler.py:3521-3545` | 来源移出 seed 后判定树看不到来源 |
| accept 只收到 **PASS 的层**；accept 开头还有一次 TOCTOU 的 `KnowledgeIndex.check` | `event_handler.py:2182-2187`；`commit_service.py:3003` | 评估记录要另找通道；stale 若混进 `check()` 会让已通过六层的结果在 accept 被判失败 |
| 层状态词表没有 INCONCLUSIVE；`passed` 只认 PASS / NEEDS_HUMAN | `deterministic_checks.py:29-32`；`verifier_router.py:251-257` | "只能下调等级"必须写在层状态上，不能写在 adapter verdict 上 |
| `FORMAL_FIELDS` 里没有 claim；新事件漏登记会进 `unknown` 让回放测试变红；`formal_from_snapshot` 与 `Store.snapshot` 要同步 | `observability/replay.py:33-47,361,481-531`；`storage/store.py:1741-1775` | 新对象必须逐个定性并补快照侧 |

## 3. 威胁模型与诚实边界

要挡住的是**把没做的检查说成做过了**：① 伪造引用；② 洗白（无关测试、宽松 Critic、模型自报的 claim 类型、模型自填的 `key`）；③ 越权（跨租户/Mission；用"不存在"与"无权"的差别探测存在性）；④ 指令注入（**不只是工具调用层面，还包括写给判定器看的话，以及以 VERIFIED 身份进下游 prompt 的来源原文**）；⑤ 隐藏条件；⑥ **压制**（用无证据的 `contradicts` 把别人的结论打成 DISPUTED，用无关的 `supersedes` 退役别人的知识）。

明确**不做**（如实不声称）：

- 不做开放网页抓取；来源只能是用户提供并已登记的资料。
- 不装 Lean、不做形式化；`formal_check` 仍是未部署层，被要求时仍 ERROR。
- **本轮来源只能是文本**（md / txt / csv / json）。二进制来源没有登记通道 → 遗留 F-P33-2。
- **不做语义等价判断**。引文比对是"NFC + 空白折叠 + 首尾 strip"之后的**完整单元字面比对**。不做 NFKC，不做标点归一。
- **报告正文中的非结论陈述不做覆盖核对**（第 2 轮 B P2-3）。系统只保证**结论区**（由系统渲染）的每一条可回读；Worker 在"分析"章节写的散段文字不在核对范围内，界面上也按此标注。第 2 版里那个"粗粒度 `uncited_conclusion` 检查"**砍掉**——它挡不住真实误导（条目数对得上就过），又对正常报告误报。
- **拉丁文句子只能整行引**（见 D2）：`.` 不作句终符，因为 `e.g. ` / `U.S. ` 会产生假终符、允许从句子中段起引。
- **A03 在本轮只对文档领域关闭**。`code-v1` 的 `covering_target` 路径前缀判定原样保留（改它会搅乱 73 条基线红集，收益不在本轮验收里）→ 遗留 **F-P33-1**，留到 P3.4。
- **文档领域的冲突走人工裁决**（见 D6）：仲裁的本质是"哪一方对"，而文档领域只能证明"某来源某段这么写"，**没有可确定判定的外部检查**。不假装有。

## 4. 设计

### D1 领域画像 `governance/domains.py::DomainProfileV1`（A07）

落点在 `governance/`（这里已是"部署给定、Mission 冻结、模型不能改"的归属地且有内容寻址 + 冻结 + 快照全套机制）；不叫 `profiles`（该词已指运行时模型画像，`policies.py:313,359`）。

画像必须能**替换**而不只是设下限：

```text
DomainProfileV1
  id / version                      # code-v1 | doc-research-v1
  allowed_input_kinds / allowed_artifact_kinds / allowed_evidence_kinds
  planner_floor                     # 最低验证政策 + 准则文法白名单（下限）
  default_policy                    # 替换 SYSTEM_DEFAULT_POLICY / default_change_policy
  conflict_template                 # 替换 conflict_task 的准则、政策与裁决方式
  synthesis_template                # 替换 synthesis 默认政策
  external_check                    # 见下：是「评估记录里某 adapter 的非空 verdict」，不是证据前缀
  role_templates / context_wording   # 替换 Arbiter/Worker 模板与 visibility 文案里的 pytest 措辞
  adapters                          # 各层挂哪些 adapter（见 D4）
  completion_rules                  # 接受一个结果必须满足什么（含 inconclusive 限额，见 D5）
```

**`external_check` 的语义统一**（第 2 轮 B P0-3 指出第 2 版这里不同构）：第 2 版写的 `external_check_evidence_kind` 是**证据字符串前缀**，而 D6 说文档领域的外部检查是 `source_coverage@v1` 的 **adapter verdict**，两者不是一回事。第 3 版统一为后者：`check_arbitration` 从"必须引 `pytest:` 且落在 `arbitration/<key>/` 下"改为"**该领域声明的外部检查 adapter 在这条仲裁 claim 上留下了非空 verdict**"。`code-v1` 的外部检查 adapter 就是 `code_test@v1`，其 verdict 由 pytest 运行产生——旧行为等价，但判据从"字符串前缀 + 目录"变成"评估记录"，两个领域同构。

**闸门是五处，不是三处**（第 2 轮 B P1-5）：

| # | 位置 | 覆盖 |
|---|---|---|
| 1 | `graph/task_graph.py:317-333` | 整图提案 |
| 2 | `graph/changes.py:515-541` | 图变更 |
| 3 | `commit_service.py:770-791` `_check_task_proposal` | 单 Task 提案 / Manager `add_task`（与 2 是不同函数） |
| 4 | `commit_service.py:1808` `insert_task` | 系统模板 · 冲突 |
| 5 | `commit_service.py:926` `insert_task` | 系统模板 · 综合（模板来自 facade `OPEN_SYNTHESIS`） |

五处走同一个 `check_against_domain()`。

冻结：照 `mission_policies` 做 `mission_domains` 绑定表；`domain` 进 `api/facade.py` 的 `OPEN_FIELDS`；0.10 之前的 Mission 绑 `code-v1`。回放只读冻结快照。

### D2 证据解析 `verification/evidence_resolver.py`（A02、A06）

**引用是结构化对象**：`ClaimProposal` 新增 `citations: tuple[SourceCitation, ...]`，`SourceCitation = {path, version, start_line, end_line, quote}`；`evidence` 字符串原样保留给 `code-v1`。`CONTRACT_SCHEMA_VERSION` **bump 到 2**（`models.py:30`，`from_json` 有严格未知键拒绝，旧 SDK 解析新信封会 `ContractError`——这是真实的兼容面变化，如实标出来）。

**来源字节的权威副本在 CAS，解析器与 adapter 都不从工作区读来源**：

- 登记来源时 `ArtifactStore.put_bytes()`，`sources` 表记 `version_hash`。
- 读取走 **`ArtifactStore.read(content_hash)`**（`artifacts/store.py:125-129`，本身重算 hash）。第 2 版写的 `read_verified(version_hash)` **这个 API 不存在**（`read_verified` 是模块级函数、参数是 `Artifact`），已更正。
- **adapter 与解析器用同一个取数口**（第 2 轮 A P0-2 缺口 2）：`source_coverage@v1` 要读来源原文，必须也走 CAS，否则它读到的是验证副本里 Worker 的那份。
- **protected 的落法**（第 2 版 D2 与 D7 互斥，第 2 轮两位都指出）：`_protected_seed` 的数据源只有 `workspace_seed` 且返回 `dict[str, str]`，而来源已移出 `workspace_seed`。第 3 版把 protected 扩成**按来源根路径前缀判定 + 内容取自 CAS**：`verification_copy(protected=...)` 的类型扩成 `Mapping[str, Path | bytes]` 与 `inputs` 同构（顺带补 `write_bytes` 缺失的 `writable` 检查，`workspace.py:152-156`）。这样 Worker 改写来源在 `protected_path_rewritten` 当场被拒，且不受 512KB 文本限制。

解析顺序与失败码：

| 检查 | 失败码 |
|---|---|
| 该来源在 `sources` 登记表里存在且属于本租户本 Mission（**路径是否落在来源根内并入这一条**，第 2 轮 A P2-B：两个可区分的码等于泄露来源根结构，与 P33-07 自相矛盾） | `not_found`（越权与不存在返回**完全相同**的结果） |
| CAS 里按登记版本读得到字节 | `unreadable` |
| 引用写的 version == **本 Attempt 派发时冻结的来源版本集合**里的版本（不是全局当前值，第 2 轮 A P1-H：否则同一份证据重跑会得到不同结论、并发 Attempt 的判定取决于提交次序） | `stale_source` |
| 行区间在文件范围内且 start ≤ end | `span_out_of_range` |
| 引文在全文中**唯一出现**（第 2 轮 A P1-B：否则"最小区间"不唯一） | `quote_ambiguous` |
| **引文是完整单元**：一整句（终符 `。！？`，**不含 `；`、不含拉丁 `.`**），或一整个结构单元（列表项 / 表格行 / 标题行 / 段落）。**锚点在空白折叠之前计算** | `quote_not_whole_unit` |
| 规范化后引文字面出现在该区间内 | `quote_mismatch` |

第 2 版的 `span_not_minimal` **砍掉**（第 2 轮 A P2-A）：让模型猜最小区间、猜错重试是纯返工来源，而系统自己算得出来——解析时由系统把区间收紧到最小并记进 `locator`，模型给的区间只作提示。安全性等价，少一个失败码少一轮返工。

**匹配用最小区间，展示用包含它的最小 markdown 块**（第 2 轮 A P1-B：`- 吞吐提升 40%` 的最小区间就是该行，上面那句 `### 供应商自述（未经我方复核）` 不在区间内，"显示区间全文"的补偿会完全失效）。两者分开定义：`locator`（匹配）与 `display_block`（展示，同一列表 / 段落 / 表格，直到上一级标题）。

解析结果 `EvidenceResolutionV1` 记：`ref / kind / target / status / source_version / locator / display_block / tenant_id / mission_id / source_trust`。`factual_status` 是 `status == resolved` 的派生属性，不另存。**`source_trust`（指令层信任）与解析结果（事实层）是两个维度**，分开存、分开显示。

旧前缀：`artifact:`/`file:` 核 hash。`tool-run:`/`knowledge:` **不做全域收紧**：`code-v1` 语义逐字不变；`doc-research-v1` 的 `allowed_evidence_kinds` 不含 `tool-run:`。`tool-run:` 现在修不了（`call_key = f"{run_id}:{call_id}"`，模型写信封时两个 id 都不知道，且只有 `view == "work"` 才记）→ 遗留 **F-P33-3**。

`grade_claim` 要多收一个领域参数；P33-19 的前后对照按 **legacy 分支的输入**对照，不把签名变更当语义变更。

### D3 准则评估记录、attribution 的三层收口（A01、A03、A06）

**评估记录** `CriterionAssessmentV1`（新表 `criterion_assessments`），字段按用户计划原文。它是**审阅载体，不是新的 Task 终态**，Task 状态机一个字不改。

**传递通道**（accept 只收到 PASS 的层，且 accept 时验证副本已不在手上）：adapter 跑在 verification 阶段，解析结果与 verdict 作为 `LayerResult.detail` 经 `record_verification_layer` 落 `verifications.detail_json`；accept 事务用 `list_verifications` 读回、重放出 `criterion_assessments` 行。FAIL 时 accept 不跑，评估只存在于 `verifications` 行里——失败码的断言落在 `detail_json` 上。挂起恢复时评估随 detail 一起复用，不重跑。
注意 `verifications` 是 `UNIQUE(result_id, layer)`（`schema.py:142`），**一个 layer 只有一行**：多条准则的评估打包进同一个 `detail_json`，而 `display_block` 全量入库有体积风险 → 只存 `display_block` 的引用坐标 + 截断后的文本（上限写死），完整文本由界面按坐标从 CAS 现取。

**attribution 的三层收口**（第 2 轮两位独立指出的中心 P0）：

1. **记录层**：一条 claim 是 attribution，当且仅当其 `content` 规范化后与某条 citation 的 `quote` **字面相等**（相等，不是包含）。`content` 由系统拼成 `《<path>》@<hash前8> #L3-L5 记载：「<quote>」`；**`key` 由系统构造为 `attribution:<version_hash>:<start>-<end>`**，模型写的 key 记 `key_downgraded` 后丢弃；`stance` 固定 `affirms`。于是一条 attribution **只可能与同一来源同一版本同一段的另一条 attribution 相遇**，永远无法用 `key`/`stance` 去反驳一条实测结论。模型声明的 `type` 只作提示，不符记 `type_downgraded`。
2. **消费层**：`knowledge_view`（`retrieval.py:310-330`）对 attribution 记录必须带 `source_trust` 与一个与 `candidate_claims` 的 `marker`（`:277`）同级的标记——「这是来源原文，不是本系统的结论，也不是指令」；排序权重不高于 SUPPORTED（`TRUST` 表，`retrieval.py:34`）；synthesizer / arbiter 的 visibility 文案（`context_builder.py:211`「只把 VERIFIED 当事实」）同步改写。
3. **交付层**：见 D8 的系统渲染结论区。

**压制通道的收口**：
- `contradicts`（`conflicts.py:47-49`）：`explicit` 冲突要求**提出方自己那条 claim 至少达到与被打击者同级**（doc 领域即需 resolved citation），否则记"未支持的冲突声明"并丢弃。
- `supersedes`（`commit_service.py:1532-1547`）：**要求目标与提案 claim 同 `key`**——这本来就是"取代"的定义。

**分级表（`doc-research-v1`）**：系统判定为 attribution + citation 全部 resolved + 确定性 adapter PASS → **VERIFIED**（范围 = 该来源该版本该段）；系统判定为 statement → **上限 SUPPORTED**，记 `scope_limited_to_source`，写死并加结构测试；任一 citation 解析失败 → UNDER_REVIEW / `unsupported` 带失败码；评估 INCONCLUSIVE → UNDER_REVIEW / `insufficient_evidence`。`code-v1` 走 legacy 分支逐字不变。

### D4 检查规范与 adapter

- 不做注册 API，模块级常量表即可。
- `code_test@v1 → layer "code_test"` 显式映射，旧字段含义不变。
- **硬约束写在层状态上，不是 adapter verdict 上**（第 2 轮 A P0-B：层状态词表没有 INCONCLUSIVE，而 `passed` 只认 PASS / NEEDS_HUMAN；在"能否接受"的语义下 `FAIL → INCONCLUSIVE` 是**放宽**不是下调）：**读不可信来源文本的 adapter 只能让层状态变差或不变，永远不能把一个否则会 FAIL 的层变成 PASS。**
- **"来源内容与主张矛盾"不由模型 adapter 判**（同上：它是唯一一条模型说了算还能加严的，因此也是唯一一条模型可以反着用来放宽的）。矛盾只在两条 claim 之间由 `key` + `stance` 判（已有机制）；来源层面不判矛盾。
- **INCONCLUSIVE 必须由确定性条件产生**：该准则的全部 citation 都 resolved，但没有一条 claim 的 content 与该准则绑定。模型 adapter 只能在确定性条件已允许 INCONCLUSIVE 的前提下附加说明。
- 文档领域：`citation_integrity@v1`（规则层，**纯确定性**，可给 PASS：所有 citation resolve 成功；每条准则至少有一条非空 verdict；"关键结论"集合由 **Task 的 success_criteria 推导**而非 Worker 标注）；`source_coverage@v1`（外部检查层，**读不可信文本，只能 FAIL / INCONCLUSIVE / NEEDS_HUMAN**）；Critic 仍不能单独升 VERIFIED（结构测试钉住）。
- `VERIFIER_VERSION` **不 bump**。理由更正（第 2 轮 B）：本轮 schema 升 v8，`VERSION_SOURCES` 的 `orchestrator_schema` 已经会让 snapshot hash 变，那半条理由多余；真正的理由是 `human_review.py:66-68` 的 `reusable_layers` 按 `verifier_version` 过滤，而层**语义**未变（变的是层内挂了哪些 adapter，adapter 身份已逐条记在评估里）。

### D5 证据不足的出口（A04）

1. **只能由 adapter 产出**，Worker 与 Critic 不能请求。
2. **与 ERROR 严格区分**：崩溃 / 未部署 = ERROR，仍短路、仍阻止接受。
3. **三条判 FAIL 而非 INCONCLUSIVE**：该准则一条 citation 都没有；citation 解析失败；两条 claim 之间的矛盾。
4. **局限是结构化字段**（第 2 轮 A P1-F：自由文本没有机器判据，写一句"部分结论证据不足"就形式满足）：信封新增 `limitations: tuple[{criterion_id, claim_id, missing}, ...]`，`rule_check` 比对"INCONCLUSIVE 的 criterion 集合 ⊆ limitations 覆盖的 criterion 集合"，不满足则 FAIL。
5. **Mission 级门槛**：关键结论中 INCONCLUSIVE 占比超阈值 → Mission 结果 **INSUFFICIENT**、停止原因 `insufficient_evidence`。**分母取 Mission 的 success_criteria（用户写的），不取 Task 的**（第 2 轮 A P1-G：Task 准则是 Planner 写的，多写几条琐碎准则就能把占比压到阈值下）。阈值放 `completion_rules`（部署常量、随 Mission 冻结、不可晋级）。
6. **防无穷返工**：不新增计数器，复用 Attempt 重试预算 + `reason="inconclusive"`；限额在 `completion_rules`。
7. **人工升级**：`escalated` 的置位条件从"只有 Critic"扩成"Critic 或 adapter 的 NEEDS_HUMAN"，共用每 Task 一次的 `escalation_left`。**同时必须改挂起恢复分支**（第 2 轮 B P1-4）：`verifier_router.py:169-174` 今天只在 `layer == "critic_review"` 这一支恢复 `escalated`，而 `reusable_layers` 会把任何 PASS/NEEDS_HUMAN 行拿回来——adapter 在规则层给的 NEEDS_HUMAN 恢复后会静默绕过第六层。把条件拆开：`escalated` 的恢复对所有层生效，`critic` 的重建仍只对 critic 层。

### D6 冲突：范围只加注，文档领域走人工裁决（A05）

- 同 key 反 stance **一律** DISPUTED + Conflict Task，与今天一致；"冲突优先于分级、命中不投影"（`commit_service.py:1554-1563`）不变。
- 范围只**加注**（双方的 `source_version` / 条件写进冲突记录的 sides），不豁免。**`checked_scope` 缺失 = 全域**。`checked_scope` 在 accept 期不参与任何判定，因此**交集代数一行都不写**（第 2 轮 A P2-D）。
- "二者适用范围不同、各自成立"是 **Conflict Task 的一种裁决结果**，不是 accept 时的预判。
- **文档领域的冲突走人工裁决**（第 2 轮 A P1-E）：`CONFLICT_POLICY` 含必需的 `code_test` 而 `source_coverage@v1` 永不能 PASS，两条相乘 = 文档冲突任务无法完成；且仲裁的本质是"哪一方对"，文档领域只能证明"某来源某段这么写"。`doc-research-v1.conflict_template` 的裁决层是 `human_review`（NEEDS_HUMAN），不假装有一个外部检查层。
- **`_open_conflict` 的部署闸门要抽象**（`commit_service.py:1735` 写死 `"code_test" not in self._deployed_layers` 就 defer）：改为按画像声明的外部检查/裁决方式判断，否则纯文档部署里每个冲突都 DEFERRED。
- 绝不多数表决（结构测试钉住）。

### D7 失效证据与来源命令（A08）

- 新表 `sources`：`(mission_id, tenant_id, path, version_hash, kind, trust, registered_at, superseded_by, revoked)`。
- **facade 三个新命令的完整设计**（第 2 轮两位都指出第 2 版只写了"走 Commit Service 与事件"）：
  - `register_source` / `supersede_source` / `revoke_source`；
  - **幂等键**：来源自己的幂等键（CAS 的 `put_bytes` 幂等，但 `sources` 的行不是）；
  - **授权**：写死为人 / Host 表面专属，**不可从 TaskProposal、图变更、工具动作或连接器回调到达**，加结构测试；
  - **审批**：`supersede_source` / `revoke_source` 能让**在途的验证结果**变 stale，即能改变已跑完的验收结论 → 需要 `approvals`，按 L2 处理；
  - **来源根与任何产物发布目标必须不相交**（第 2 轮 A P1-H：否则 P3.2 的受控发布产物能被登记成来源，引用自己写的句子拿 VERIFIED，形成自引闭环），创建 Mission 时校验；
  - **revoke 只影响新的使用**：已开的 Conflict Task 不因 revoke 关闭（否则可以用撤回来源来消解已记录的冲突）。
- **历史一个字不改**：已有 claim / knowledge / 评估原样保留，带当时的 `source_version`。
- **新的使用重新检查，用派生谓词而不是推送标记**：`KnowledgeRecord` 增加 `source_versions`；综合产物继承上游的**并集**（一跳洗白自动失效）。
- **stale 走独立方法，不碰 `check()`**（第 2 轮 B P1-3：`check_used_knowledge` 的实现就是 `return index.check(...)`，而 accept 开头还有一次 TOCTOU 的 `KnowledgeIndex.load(...).check(...)`（`commit_service.py:3003`），非空直接 `fail_result`——把 stale 混进去，一个已通过全部六层的结果会在 accept 被判失败，用户拿到 FAIL 而不是 A08 想要的"重新检查 / 明确局限"）：新增 `KnowledgeIndex.stale(...)`，`check()` 一个字不改；失效知识由 `context/retrieval.py` **排除出可用知识**并标明原因，模型根本拿不到它。
- 同一 accept 事务内新建的知识不作为证据。

### D8 Host 接线与界面（A01）

- 部署清单 features 加 `domains`；创建 Mission 时选领域（默认 `code-v1`）；`domain` 进 facade `OPEN_FIELDS`。
- 来源登记走 `register_source`（算 hash、进 CAS、标 `untrusted_external`）。
- **报告的结论区由系统渲染**（第 2 轮 A P0-C）：Worker 只提交 claims，系统按 claim 记录生成"结论"章节（等级徽标、两个信任标记、`display_block` 全文、审阅记录、范围）；Worker 的自由文字只能出现在明确标注为"分析 / 非结论"的章节。这同时让 P33-20 的原生验收变成可机器核对的。
- **Mission 级判定树要挂来源**（第 2 轮 B P1-7）：`_evaluate_criteria`（`event_handler.py:3521-3545`）只挂 `workspace_seed` + 已接受 artifacts，**不含 inputs**；来源移出 seed 后判定树看不到来源，而文档 Mission 的 Mission 级准则基本是自由文本 → `needs_critic` 必为真 → 整个 Mission 的成败由一个**看不到来源的 judge Critic** 决定。改为按登记版本从 CAS 挂载；Mission 级 INSUFFICIENT 在 `judge_mission` 之前由确定性代码算出。
- 界面：四等级徽标 + 两个分开的标记 + 可点开的引用 + 审阅记录 + 范围；证据不足显示"证据不足（缺什么）"；Mission 级 INSUFFICIENT 单独显示。

### D9 事件与回放

| 对象 | 定性 | 落点 |
|---|---|---|
| 领域冻结 | **正式状态**，照 `policy_version_id` 先例 | `MissionCreated` payload 带 `domain_id`；`FORMAL_FIELDS["mission"]` 加 `domain_id`；`OPTIONAL_FIELDS` 加 `("mission","domain_id")`；**`formal_from_snapshot` 与 `Store.snapshot` 同步补 `mission_domains`，照 `actions`/`approvals` 加 `has_table()` 守卫**（`store.py:1767-1772`），否则旧库读新代码会抛 |
| `sources` | **正式状态** | 新事件 `SourceRegistered` / `SourceSuperseded` / `SourceRevoked`；`FORMAL_FIELDS["source"] = ("version_hash", "superseded_by", "revoked")`——**`revoked` 用布尔不用时间戳**（`compare()` 是精确比较，两侧时钟不同就是永久 mismatch）；**`formal_from_snapshot` 必须补 `"source"` 项**，否则 `compare()` 的反向检查会把每一条来源都算成 mismatch、`consistent` 直接 False。旧库两侧都空、分母不变，**不需要 `OPTIONAL_FIELDS`** |
| `criterion_assessments` | **不进正式状态**（审阅载体），照 `verifications` 先例 | 不发新事件，随 `VerificationLayerRecorded` 的 detail 走（该事件已在 `NO_FORMAL_EFFECT`） |
| 失效知识 | 派生谓词 | 不进投影 |

`check_structure`（`replay.py:396-468`）的不变量都围绕 mission/task/attempt/result/action/conflict，**加 `source` 不触发任何一条、不会变红**（已核）。切片 A 第一件事是跑 `test_replay.py` 确认 `unknown_event_types == {}`；sources 的三个事件在切片 B 才出现，由 P33-36 的"每切片跑一次"覆盖。

## 5. 切片

| 切片 | 内容 | 覆盖 |
|---|---|---|
| A | 领域画像（含替换默认政策、系统模板、角色模板与 context 文案）、**五处**闸门、`mission_domains` 与 facade、**schema v8**、D9 的事件与回放（含 `formal_from_snapshot` / `Store.snapshot`）、**`check_arbitration` 抽象与 `_open_conflict` 部署闸门抽象一并前移** | A07；A05 的前置 |
| B | **schema v9** `sources` 表与三个 facade 命令（幂等键/授权/审批/发布目录不相交）、来源进 CAS、protected 扩成 `Path | bytes` 并按来源根判定、`SourceCitation` 契约与 `CONTRACT_SCHEMA_VERSION` bump、EvidenceResolver 七个失败码 | A02、A06 一半 |
| C | **schema v10** `criterion_assessments`、评估记录的产出与传递、`grade_claim` v2、**attribution 三层收口**（系统构造 key、消费层标记、压制通道收口）、`code-v1` legacy 分支 | A03、A01、A06 另一半 |
| D | adapter 常量表、三个文档 adapter、**层状态上的硬约束**、INCONCLUSIVE 七条边界、结构化 `limitations`、Mission 级 INSUFFICIENT、人工升级与挂起恢复 | A04 |
| E | 冲突范围加注、文档领域人工裁决、知识 `source_versions` 与 `KnowledgeIndex.stale`、检索排除 | A05、A08 |
| F | 全量回归、wheel 干净环境验证 | A07 后半 |
| G | Host 钉版、接线、**系统渲染结论区**、Mission 判定树挂来源、真实 deepseek-flash 原生验收 | A01 |

**每个切片完成即跑 `tests/orchestrator` 全量**，不等切片 F；然后提交推送、更新 handoff。

## 6. 风险

| 风险 | 处置 |
|---|---|
| **fixtures 悬挂**：脚本耗尽直接抛 AssertionError（`fixtures.py:104,514,524`），切片 B/C/E 都可能触发 | 每切片完成即跑全量；发现悬挂先查是不是脚本配额，不放宽断言 |
| **真实模型写不出合规 citation**（第 2 轮 A P1-B 的可行性反面）：表格单元格与冒号句（`：` 不是终符）只能整行整引，而 P3.3 的场景恰以表格与冒号句为主；模型的省力反应是**改结论去迁就可引的句子**，比引用失败更糟 | 结构单元（列表项/表格行/标题行/段落）算作完整单元，覆盖表格与冒号句；citation schema 随画像注入 Worker 输入包；切片 G 先用真实文档做一次可行性 spike，不合格就回头调文法而不是调结论 |
| 引文比对被误解成语义相似 | §3 与 D2 写死；结构测试钉住不做 NFKC |
| 文档领域禁 `pytest:` 挡住混合 Mission | 领域按 Mission 冻结；混合场景 → 遗留 F-P33-4 |
| schema 迁移碰真实库 | 迁移是**追加式**的，一个切片一版（v8 领域绑定 / v9 来源 / v10 评估记录），不攒成一次大改；备份优先、已 ≥ 目标版本 no-op、真实库副本干跑 |
| `verifications.detail_json` 体积（`UNIQUE(result_id, layer)`，一个 layer 一行） | 只存坐标 + 截断文本，完整文本按坐标从 CAS 现取 |
| 新表/新事件进回放的投影漏项 | D9 逐个定性；切片 A 先跑回放测试；复核阶段用 pytest 插件扫全部 Mission |
| `evaluation.py:216-228` 在 `critic` ablation 下禁自由文本准则，文档领域永远跑不了该策略 | 切片 G 不安排 critic ablation 的对照评测，如实说明 |

## 7. 评审处置

### 第 1 轮（两位，共 10 P0 / 18 P1 / 9 P2）

全部采纳或给出裁决理由，处置表见第 2 版；唯一不采纳的是 A P1-5（按 claim 类型分叉判冲突），取 B P0-1 的更严方案。

### 第 2 轮（两位均 READY_WITH_CHANGES）

| 来源 | 条目 | 处置 |
|---|---|---|
| A P0-A / B P0-1 | `key`/`stance` 把 attribution 洗成世界断言；不可信原文以 VERIFIED 身份进下游 prompt | **采纳**（两位独立指出）：attribution 的 key 由系统构造、stance 固定；`knowledge_view` 带 `source_trust` 与 marker、排序不高于 SUPPORTED；文案同步 → D3 |
| A P0-B | INCONCLUSIVE 比 FAIL 宽松，约束写错了层次 | **采纳**：约束改写在层状态上；矛盾不由模型判；INCONCLUSIVE 只由确定性条件产生 → D4 |
| A P0-C | 报告正文与 claim 无绑定，A01 在交付层是空的 | **采纳**：结论区由系统渲染 → D8；同时按 B P2-3 在 §3 写明正文非结论陈述不做覆盖核对，并砍掉 `uncited_conclusion` |
| B P0-3 余项 | 系统模板还有 4 处 pytest 硬编码；`external_check` 语义不同构 | **采纳**：`_open_conflict` 闸门、`check_arbitration` 目录约束、Arbiter 模板、context 文案全部纳入画像；`external_check` 统一为"adapter 的非空 verdict" → D1、D6 |
| B P0-5 余项 | `formal_from_snapshot` / `Store.snapshot` 缺项；`revoked_at` 时间戳风险 | **采纳**：补两处并加 `has_table()` 守卫；改布尔 `revoked`；`OPTIONAL_FIELDS` 只给 `domain_id` → D9 |
| A P1-A | `；` 与拉丁 `.` 重新打开剥离前提 | **采纳**：终符只留 `。！？`；拉丁句子整行引 → D2、§3 |
| A P1-B | 最小区间与显示全文方向相反；列表/表格；引文不唯一；规范化与锚点次序 | **采纳**：匹配用最小区间、展示用 `display_block`；结构单元算完整单元；`quote_ambiguous`；锚点先于空白折叠 → D2 |
| A P1-C / P1-D | `contradicts` 免费降级；`supersedes` 不校验 key | **采纳**：提出方需同级证据；`supersedes` 要求同 key → D3 |
| A P1-E | 文档领域 Conflict Task 永远不可能通过 | **采纳**：文档领域冲突走人工裁决，不假装有外部检查 → D6、§3 |
| A P1-F | 局限没有机器判据 | **采纳**：结构化 `limitations` 字段 → D5 |
| A P1-G | INSUFFICIENT 分母可被 Planner 稀释 | **采纳**：分母取 Mission 的 success_criteria → D5 |
| A P1-H | 来源命令的授权/时序/自引闭环/revoke 消解冲突 | **采纳**四条 → D7 |
| B P1-2 | protected 与 inputs 互斥 | **采纳**：protected 扩成 `Path | bytes` 并按来源根判定 → D2 |
| B P1-3 | stale 混进 `check()` 会撞 accept TOCTOU | **采纳**：独立 `stale()`，`check()` 不动 → D7 |
| B P1-4 | adapter 的 NEEDS_HUMAN 在挂起恢复后失效 | **采纳**：拆开复用分支 → D5 |
| B P1-5 | 闸门实际是五处 | **采纳** → D1 |
| B P1-6 | 三个 facade 命令没有设计 | **采纳**：幂等键/授权/审批/定位 → D7 |
| B P1-7 | Mission 判定树看不到来源 | **采纳** → D8 |
| A P2-A | `span_not_minimal` 改由系统收紧 | **采纳**：砍掉该失败码 → D2 |
| A P2-B | `out_of_scope` 与 `not_found` 可区分 = 泄露结构 | **采纳**：并入 `not_found` → D2 |
| A P2-C / B P2-3 | `uncited_conclusion` | **采纳**：砍掉，改为 §3 的诚实边界 + D8 的系统渲染 |
| A P2-D | `checked_scope` 交集代数无消费者 | **采纳**：一行都不写 → D6 |
| A P2-E | 廉价 VERIFIED 污染检索排序 | **采纳**：attribution 排序不高于 SUPPORTED → D3 |
| A P2-F | `write_bytes` 缺 `writable` | **采纳**：切片 B 顺手补 → D2 |
| B P2-1 | `read_verified(version_hash)` 这个 API 不存在 | **采纳更正**：用 `ArtifactStore.read(content_hash)` → D2 |
| B P2-2 | `CONTRACT_SCHEMA_VERSION` 要不要 bump | **裁决：bump 到 2**。`citations` 让 `from_json` 的严格未知键拒绝对旧 SDK 生效，这是真实的兼容面变化，如实标出 → D2 |
| B P2-4 | 切片顺序错位（A 换模板、E 才抽象 `check_arbitration`） | **采纳**：两项前移到切片 A → §5 |
| B P2-5 | 文档领域跑不了 critic ablation | **采纳**：切片 G 不安排该对照，如实说明 → §6 |
| B P0-4 提醒 | `grade_claim` 多收领域参数，别把签名变更当语义变更 | **采纳** → D2 |
| B P1-1 细节 | `verifications` 一个 layer 一行；`display_block` 体积 | **采纳**：打包进同一 `detail_json`，只存坐标 + 截断文本 → D3 |
| B P1-8 更正 | 不 bump 的理由里有半条多余 | **采纳更正** → D4 |
