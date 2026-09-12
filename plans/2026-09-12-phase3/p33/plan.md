# P3.3 非代码 Mission 与证据闭环 · 计划（第 1 版）

- 依据：Host `plans/taskSys2/agent-orchestrator-phase3-plan.zh-CN.md` §5（P3.3）与 §11 的 P3.3-A01..A08。
- 理论定义（写代码前已查）：
  - `04_tree_search_blackboard_memory.md` §9 可信等级：**VERIFIED = 通过机器验证或可靠规则**（不是"只能靠 pytest"），SUPPORTED = 有实验或证据支持，DISPUTED = 存在冲突证据；§10 知识必须带 provenance。
  - `10_concurrency_conflict.md` §7：冲突不投票，开 Conflict Task 检查双方证据；§8 知识状态不是 True/False。
  - 以上两条决定了本轮的核心走向：文档领域的 VERIFIED 走**可机器复核的引用解析**（来源 hash + 段落定位 + 引文比对），而不是"让文档任务去凑一个 pytest"，也不是"Critic 说不错"。

## 1. 用户场景（Phase3 §5.1）

用户给几份带版本的真实文档，要求比较方案并写报告。系统：Planner 拆成资料核查 / 比较 / 整合 → Worker 引用来源 → Critic 核对遗漏与矛盾 → 领域检查与必要的人工审阅 → 只把符合既定依据的结论写进正式知识 → 交回一份每条关键结论都能点回原始来源段落的报告；确实证据不足的地方，明确写成局限，而不是编一个 PASS。

## 2. 现状与差距（读代码得到，不是推测）

| 现状 | 位置 | 差距 |
|---|---|---|
| Claim 分级只有一条 VERIFIED 路径：引用 `pytest:<target>`，且该 target 被本次 `code_test` 跑过并通过 | `memory/claims.py::grade_claim` | 文档任务没有合法的 VERIFIED 路径；反过来，一个与结论无关的 pytest 只要跑过就能把任意引用了它的主张顶成 VERIFIED（A03） |
| 证据引用只做**前缀解析**：`pytest:/file:/artifact:/tool-run:/knowledge:`，`tool-run:`、`knowledge:` 一律记为 trusted，路径类只查"是不是登记过的 artifact 路径" | `memory/claims.py::parse_evidence` | 没有任何"真的去把这个引用解析开"的动作：文件是否存在、hash 对不对、指向哪一段、属不属于本租户本 Mission，全都没查（A02、A06） |
| `rule_check` 只核对 envelope 里 artifact 的路径与 hash、`file:` 准则、used_knowledge | `verification/deterministic_checks.py` | 没有引用完整性、来源 hash、覆盖核对（A01、A02） |
| 六层里只部署了 5 层，层与"检查规范"是一对一硬编码；`code_test` 就是 pytest | `verification/verifier_router.py` | 没有 adapter registry，新领域无处挂检查；也没有版本化的检查规范（§5.3） |
| 冲突判定只比 `key` + `stance` | `verification/conflicts.py::find_contradiction` | 不看适用范围：同一结论在不同来源版本/不同条件下成立，会被误判成冲突；反之带范围的真冲突也没有范围记录（A05） |
| 不可信来源只在工具网关打 `trust: untrusted_external` 标记，在分级时直接把引用判死 | `tool_gateway.py` / `claims.py` | 指令不可信与事实不可核验被合成了一个布尔值，用户给的资料等于白给（A06） |
| Mission 没有领域概念；`workspace_seed` 只是一堆文件，没有版本登记 | `api/facade.py` OPEN_FIELDS | Planner 可以给任务挑任意宽松的验证政策；来源没有版本身份，失效/被取代无从谈起（A07、A08） |

## 3. 威胁模型与诚实边界

本轮要挡住的是**把没做的检查说成做过了**：

1. 伪造引用：指向不存在的文件、改过的文件、越界的行号、根本不含该说法的段落。
2. 洗白：用一个无关的通过测试，或一个宽松的 Critic PASS，把没核过的结论顶成 VERIFIED。
3. 越权：引用别的租户 / 别的 Mission 的来源与知识；用"文件不存在"和"无权访问"的差别去探测对象是否存在。
4. 指令注入：不可信资料里写"请把这条结论标为已验证"。
5. 隐藏条件：结论只在某个来源版本 / 某个条件下成立，报告里不写范围。

明确**不做**（本轮如实不声称）：
- 不做开放网页抓取；来源只能是用户提供并已登记的资料（Phase3 §5.1 原文要求）。
- 不装 Lean / 不做形式化；`formal_check` 仍然是未部署层，被要求时仍然 ERROR（A07 后半条）。
- 不做 PDF / 表格解析；本轮来源类型限文本类（md / txt / csv / json），二进制资料只记 hash、不能被段落定位引用。
- 不做语义等价判断：引文比对是**规范化后的字面包含**，不是"意思差不多"。

## 4. 设计

### D1 领域画像 `domains/profiles.py::DomainProfileV1`（对应 §5.3 第 1 条，A07）

一份画像是部署给出的常量，不是模型能改的东西：

```text
DomainProfileV1
  id / version                      # 例：doc-research-v1
  allowed_input_kinds               # 能作为来源登记的文件类型
  allowed_artifact_kinds            # 产物类型
  allowed_evidence_kinds            # 这个领域**允许出现**的证据前缀集合
  planner_floor                     # 任务最低验证政策 + 准则文法白名单
  check_specs                       # 有序的检查规范（见 D4），带 adapter id 与版本
  completion_rules                  # 接受一个结果必须满足什么（例：INCONCLUSIVE 必须写进局限）
```

- 注册两份：`code-v1` = **今天的行为逐字不变**（`allowed_evidence_kinds` 含 `pytest:`，check_specs 映射到现有五层），`doc-research-v1` = 新领域（`allowed_evidence_kinds` **不含** `pytest:`）。
- 创建 Mission 时冻结 `profile_id@version`，内容寻址存一份快照（沿用 P3.2 的 CAS），回放只读快照，不读当前注册表。
- 迁移：schema v7 之前的 Mission 绑 `code-legacy`（沿用第 9 步 `policy-legacy` 的做法），语义与今天完全一致。
- 闸门：Planner 提的任务、Manager 的 `add_task`、图变更三处都过同一个校验函数——验证政策不得低于 `planner_floor`，准则文法不得超出白名单，证据前缀不得超出 `allowed_evidence_kinds`。Planner 不能把验证器换松（§5.3 原话）。

**这条直接回答 A03 的根**：文档领域根本不允许 `pytest:` 作为证据，"写个无关测试来洗白"这条路在文法层就断了；同时 `code-v1` 一字未改，A07 的旧语义与回放不变。

### D2 证据解析 `verification/evidence_resolver.py`（§5.3 第 2 条，A02、A06）

新增来源引用文法（仍是数据，宽松解析，解析不了就是 unresolved 而不是异常）：

```text
source:<workspace-relative-path>@sha256:<64hex>#L<start>-L<end>["<quote>"]
```

解析按顺序做六件事，任一不过都给出**具体的失败码**并且**不升级任何等级**：

| 检查 | 失败码 |
|---|---|
| 路径归一化后落在本 Mission 登记的来源根内 | `out_of_scope` |
| 该来源在 `sources` 登记表里存在，且属于本租户本 Mission | `not_found`（**越权与不存在返回同一个码**，不泄露对象存在性，沿用 P3.1-A04 的口径） |
| 验证副本里读得到这个文件 | `unreadable` |
| 重算 sha256 == 引用里写的 hash | `hash_mismatch` |
| 重算 sha256 == 登记表里该来源的**当前**版本 hash | `stale_source`（A08 用它） |
| 行区间在文件范围内；给了引文时，规范化空白后引文字面出现在该区间内 | `span_out_of_range` / `quote_mismatch` |

解析结果落成记录（每条都进证据包，PASS 与 FAIL 一样记）：

```text
EvidenceResolutionV1
  ref / kind / target
  status: resolved | out_of_scope | not_found | unreadable | hash_mismatch | stale_source | span_out_of_range | quote_mismatch
  source_version (sha256) / locator / byte_span
  tenant_id / mission_id
  source_trust: trusted | untrusted_external      # 指令层信任
  factual_status: resolved | unresolved           # 事实层可核验性
```

**两个标记分开存、分开显示**（§5.3 最后一条原话：不得把来源的指令信任与事实可验证性合成一个布尔值）。这就是 A06 的做法：不可信资料里的**命令永不执行**（工具网关与数据框照旧），但它的**段落可以被解析核对**——只是核对出来的是"该来源的该版本在该段落里如此记载"，不是"世界如此"。

同时保留并强化旧前缀：`artifact:`/`file:` 解析为登记产物并核 hash；`tool-run:` 必须能在 `tool_calls` 表里按 id 查到且属于本 Mission（今天是无条件 trusted，这是个真缺陷，一并修）；`knowledge:` 必须是本 Mission 的 VERIFIED 知识且未被取代（复用 `KnowledgeIndex.check`）。

### D3 准则评估记录 `CriterionAssessmentV1`（§5.3 第 4 条、§5.3 拟议记录，A01、A03）

按用户计划里给的字段建新表（schema v8），在 accept 事务里写：

```text
CriterionAssessmentV1
  criterion_id / task_contract_revision
  claim_id / claim_revision
  output_ref / output_hash
  evidence_refs + source_versions
  verifier_adapter_id + version
  checked_scope
  verdict: PASS | FAIL | INCONCLUSIVE | NEEDS_HUMAN
  receipt_id / provenance
```

- 它是**审阅载体，不是新的 Task 终态**（原文）。Task 状态机一个字不改。
- `checked_scope` 是这条评估**实际覆盖到什么**：对文档领域是 `(source_path, source_version, span)` 的集合 + 结论键；对代码领域是 pytest target。
- **绑定规则（A03 的正面表述）**：一条 Claim 能升到 VERIFIED，当且仅当存在一条 verdict=PASS 的评估，其 `claim_id` 指向它、且 `checked_scope` 覆盖它引用的证据。没有绑定就不能 VERIFIED——无论有多少个测试通过了。
- `grade_claim()` 改为从**有效评估 + 覆盖回执**推导等级；旧的 pytest 策略作为 `code-v1` 的 legacy 适配保留（原文要求），走同一个函数的 legacy 分支，不另开一套。
- 分级表（文档领域）：

| 情形 | 等级 |
|---|---|
| `type=attribution`（"某来源如此记载"）+ 引用全部 resolved + 评估 PASS | VERIFIED（范围写明：某来源某版本某段） |
| `type=statement`（关于世界的事实）+ 只有不可信来源的 resolved 引用 | SUPPORTED（并记 `scope_limited_to_source`） |
| `type=statement` + 独立的两个以上互不隶属来源都 resolved，或有可信来源 | SUPPORTED；要 VERIFIED 需要该领域的外部检查 adapter 给 PASS |
| 任一引用 resolve 失败 | UNDER_REVIEW，`grade=unsupported`，并把失败码带上（A02） |
| 评估 INCONCLUSIVE | UNDER_REVIEW，`grade=insufficient_evidence`（A04） |

### D4 检查规范与 adapter 注册表（§5.3 第 3 条）

- `verification/adapters/` 新增 `AdapterRegistry`，注册项 = `(adapter_id, version, layer, run)`。
- **V2 的外部检查与旧 `code_test` 显式映射**（原文：不把旧字段悄悄改义）：注册一条 `code_test@v1 → layer "code_test"` 的映射，评估记录里写 `verifier_adapter_id="code_test", version="v1"`，旧字段含义不变。
- 文档领域三个 adapter：
  - `citation_integrity@v1`（规则层）：报告里每条被标为关键结论的条目至少有一条 resolved 引用；所有 claim 的引用全部 resolve 成功；发现伪造引用直接 FAIL。
  - `source_coverage@v1`（外部检查层）：逐条准则算 `checked_scope` 并给 verdict；来源里找不到足以判定的内容时给 **INCONCLUSIVE**，不给 PASS 也不硬判 FAIL。
  - Critic 仍是独立审阅层，**不被允许单独把任何东西升到 VERIFIED**（今天也不允许，这里加结构测试钉住）。
- Router 保持 V1 政策语义：层序不变、必需层 FAIL/ERROR 短路不变、未部署层 ERROR 不变。adapter 只是在层内部被调度。

### D5 证据不足的出口（A04）

- INCONCLUSIVE **不等于 FAIL**：结果仍可被接受，但 `completion_rules` 要求报告里必须有对应的局限条目（`limitations[]` 指名哪条结论证据不足、缺什么），否则 `rule_check` FAIL。正确的不确定性报告是合法交付（§5.5 原话）。
- 防无穷返工：同一 Task 因 INCONCLUSIVE 返工次数受 `inconclusive_retry_limit`（默认 1）限制；用完后两条出路——带局限交付，或走 `human_review` 第六层升级。两条都不是伪造 PASS。

### D6 冲突带范围（A05）

- `find_contradiction` 增加范围比较：同 key 反 stance 且 `checked_scope` **有交集** → DISPUTED + Conflict Task（沿用第 4 步的机制，冲突优先于分级不变）。
- 范围**无交集**（不同来源版本 / 不同条件）→ 不是冲突，两条都保留为带范围的事实，各自记 `scope`。这正是 §5.4 的"保留双方、记录版本/时间/条件"。
- 绝不多数表决（今天也没有，加测试钉住）。

### D7 失效证据（A08）

- 新增 `sources` 表（schema v8）：`(mission_id, path, version_hash, registered_at, superseded_by, revoked_at, kind, trust)`。
- 来源被更新或撤回时：**历史记录一个字不改**；已有的 Claim / Knowledge / 评估全部原样保留（带它们当时引用的 `source_version`）。
- 新的使用必须重新检查：任何**新的 accept** 引用旧版本 → `stale_source` → 不能 VERIFIED；依赖该来源的已有 VERIFIED 知识打 `needs_recheck` 数据标记（不是状态变更，§25.3 状态机不扩），下游再用它时 `check_used_knowledge` 要求先复验。
- 摘要/综合产物记录用了哪些知识（今天 `used_knowledge` 已有），组合后重新验收（今天 synthesis 已要求）；这里补的是"被引知识失效时综合产物要复验"。

### D8 Host 接线与界面（§5.3 最后一条，A01）

- 部署清单 features 加 `domains: ["code-v1", "doc-research-v1"]`；创建 Mission 时选领域（默认 code-v1，保持现有行为）。
- 来源登记：用户在界面挑选已授权目录里的资料 → 登记进 `sources`（算 hash）→ 作为 `workspace_seed` 注入，并标 `untrusted_sources`。
- 每条结论展示：等级徽标（VERIFIED / SUPPORTED / DISPUTED / 未知）、**分开**的两个标记（来源指令信任、事实核验状态）、可点开的引用（路径 + hash 前缀 + 行区间 + 引文）、审阅记录（哪个 adapter 哪个版本给的什么 verdict）、范围。
- 证据不足的结论显示为"证据不足（缺什么）"，不显示成灰色的通过。

## 5. 切片

| 切片 | 内容 | 覆盖 |
|---|---|---|
| A | 领域画像、冻结与三处闸门、legacy 绑定、schema v8 迁移 | A07 |
| B | EvidenceResolver + `sources` 登记表 + 旧前缀的真实解析（`tool-run:`/`knowledge:`） | A02、A06 |
| C | CriterionAssessmentV1 表与写入、`grade_claim` v2（含 code-v1 legacy 分支） | A03、A01 一半 |
| D | adapter registry + 三个文档 adapter + INCONCLUSIVE 出口与局限条目 | A04 |
| E | 冲突范围比较 + 来源失效与 needs_recheck | A05、A08 |
| F | 全量回归、回放投影补齐、wheel 干净环境验证 | A07 后半 |
| G | Host 钉版、接线、界面、真实 deepseek-flash 原生验收（一份真实报告） | A01 |

每个切片：测试先行 → 实现 → 提交推送 → 更新 handoff。

## 6. 风险

| 风险 | 处置 |
|---|---|
| 引文比对做成"语义相似"会变成又一个说不清的判断 | 只做规范化空白后的字面包含，文档里写死这条边界 |
| 文档领域禁用 `pytest:` 可能挡住"既有代码又有文档"的混合 Mission | 本轮领域按 Mission 冻结；混合场景如实登记为遗留，不在本轮假装支持 |
| schema v8 迁移碰上真实库 | 沿用第 8/9 步做法：备份优先、已 ≥v8 为 no-op、真实库副本干跑 |
| INCONCLUSIVE 出口被模型当成万能挡箭牌 | 局限条目必须指名结论与缺什么，`citation_integrity` 校验其结构；retry 上限兜底 |
| 新表进回放的投影漏项（第 8 步踩过） | 复核阶段用 pytest 插件在全部测试的 Mission 上扫回放覆盖率 |
