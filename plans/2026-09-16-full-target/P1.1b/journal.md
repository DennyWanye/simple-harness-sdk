# P1.1b 实施记录（验收纯规则：成功表达式三值求值、硬门、必需检查完整性、独立性）

日期：2026-09-16｜基线：SDK main `873fd4a`（工作区含并行的 P1.1 契约改动）｜执行：单个 Opus 子代理｜实际耗时：约 55 分钟（含规范阅读约 20 分钟）

规范来源：
主计划 `complete-plan.zh-CN.md` §6.3、§8.1、§13（含 v1.4 补充：六用途/账户归属、受限 AST、硬约束独立 AND、四个事实不可互换）、§23 小片表 P1.1b 行、§25.1 裁决 1/2/3/4；
附件 `annex/aer-1.0/design.zh-CN.md` §4.1–4.3、§5.3–5.4、§6.1–6.2、§7；
附件 `annex/aer-1.0/reference/protocol_rules.py` 与 `tests/test_reference.py`（30 条参考纯规则测试，作为红测试起点改写，未复制其实现）；
附件 `acceptance-scenarios.json` 的 AER-V01/V02/V04/V05/V08。

## 1. 实现清单

### 生产代码（1 个新文件，805 行）

`src/agent_orchestrator/verification/acceptance_rules.py`

| 分区 | 交付 | 规范落点 |
|---|---|---|
| 投影 | `outcomes_by_id`、`project_verdict`、`CONCLUSIVE_EXECUTION` | AER §4.3 / I07：execution 非 SUCCEEDED（ERROR/NOT_RUN/CANCELLED/RUNNING）一律投影为 UNKNOWN，**不是 FAIL**；AER-V08 |
| 成功表达式 | `ExpressionResult{verdict, witness_path, unevaluated_ids}`、`evaluate_success_expression` | AER §4.3：all 遇 FAIL 为 FAIL、全 PASS 为 PASS、否则 UNKNOWN；any 遇 PASS 为 PASS、全 FAIL 为 FAIL、否则 UNKNOWN；缺失准则记 UNKNOWN 并进 `unevaluated_ids`，绝不默认 PASS |
| 硬门 | `GateResult{failed_ids, unknown_ids, missing_ids, passed}`、`hard_gate` | AER §4.1 / AER-V02：只读 `RequirementClass.HARD_CONSTRAINT`，**完全不看成功表达式**，FAIL / UNKNOWN / 缺席三类分开报告 |
| 必需检查 | `CompletenessResult{match, not_executed_ids, not_passed_ids, unsourced_ids, declined_ids, independence_required_ids, complete}`、`required_checks_complete`、`DISPATCHER_RECEIPT_KINDS` | AER §5.4 / AER-V04：复用契约 `match_review_criteria` 做一一匹配；`required_check_ids` 非空的准则必须真跑（SUCCEEDED）且 PASS；回执必须含 `tool_receipt`/`operation` 类 `TypedRef`，`knowledge`/`source`/`artifact`/`review` 这类模型撰写的引用不算；只在 `any` 分支下的准则允许未评估并记入 `declined_ids` |
| 独立性 | `IndependenceReason`(4)、`IndependenceFacts`、`IndependenceResult`、`independence_ok` | AER §5.3 最低要求：Reviewer 身份 ≠ 生产候选的 Agent（SELF_REVIEW）、无修改候选工作区权限（WRITE_ACCESS_TO_CANDIDATE）、不审自己改出的版本（REVIEWED_OWN_EDIT）、不同模型不自动算独立证据（MODEL_DIVERSITY_IS_NOT_INDEPENDENCE，与 SELF_REVIEW 一起报出） |
| 正式接受 | `AcceptReason`(20)、`ExecutionPosture`、`CompoundFacts`、`AcceptanceSubject`、`AcceptDecision`、`acceptable` | AER §6.2 九段合取：身份匹配 → 缺根准则（AER-V01）→ 硬门 → 成功表达式 PASS → 必需检查完成 → Review 结论 ACCEPT 且独立 → witness 当前有效/用途匹配/可读 → 在途关键操作/取消/方法采用 → compound（合法方法、全部必需 occurrence 有贡献、组合义务）。每个未满足项一个 reason code，`reasons` 为空才算可接受 |
| 重试 | `RetryAction`(6)、`RetryReason`(10)、`ReviewAttempt`、`ReviewHistory`、`RetryDecision`、`retry_policy` | AER §5.4：限额重试 / 升级独立 Reviewer / 升级人工 / 停止共用同一义务预算（`consumes_budget`）；同一候选版本上出现 ACCEPT 与非 ACCEPT → `ARBITRATE`（多数票不生效）；候选未变更不得重复抽样（`RESAMPLING_NOT_PERMITTED`），只有候选变更或基础设施错误才允许 `RETRY` |

明确不做（已写进模块 docstring）：不调模型、不读 DB、不发通知、不做 §7 的事务/回执/披露。

### 红测试（1 个新文件，1278 行，**101 条**）

`tests/orchestrator/full_target/test_acceptance_rules.py`

| 组 | 条数 | 覆盖 |
|---|---:|---|
| 三值表 | 9 + 9 | `all` / `any` 两子节点 PASS×FAIL×UNKNOWN 全组合（参数化），与附件参考的 5 条表达式测试逐条对应并扩展为全表 |
| 表达式结构与投影 | 12 | 单准则透传；嵌套 all(any(...), 硬准则) 与 any(all(...), ...)；空 `all` / 空 `any` 被契约拒绝；缺失准则 → UNKNOWN 且进 `unevaluated_ids`；缺失不掩盖真实 FAIL；四种非 SUCCEEDED execution 投影为 UNKNOWN（参数化）；SUCCEEDED 保留原 verdict；RUNNING+PASS 仍是 UNKNOWN；契约拒绝 PASS+NOT_RUN（I07）；witness_path 三条（指向失败合取 / 通过分支 / 只列未定项） |
| 硬门（AER-V02） | 6 | 结果 OR 通过但隐私硬门 FAIL → 整体不通过且 `any` 分支确实为 PASS；FAIL 与 UNKNOWN 分开；缺席硬准则进 `missing_ids`；全 PASS 通过（即使非硬准则 FAIL）；ERROR 不算 PASS；契约拒绝把硬约束放进 `any` |
| 必需检查与一一匹配（AER-V04） | 11 | 未知 ID 拒绝；重复 ID 由契约在构造 `ReviewRecord` 时拒绝；遗漏必需 ID 拒绝；显式未采用的 OR 分支允许未评估（`declined_ids`）；NOT_RUN / ERROR 不等于 PASS；模型撰写引用不算回执；无任何证据不算回执；EXECUTION_RECEIPT 准则即使没有具名检查也要回执；`operation` 类回执可用；纯语义无具名检查的准则不要求回执；`independence_required` 被透出 |
| 独立性（AER-V05） | 6 | 自审；有写权限；审自己改出的版本；换模型不等于独立；合法独立 Reviewer 通过；多项同时违反一次全报 |
| 正式接受（AER §6.2） | 26 | happy path；缺根要求（AER-V01，同时给出 `ROOT_CRITERION_MISSING` 与 `unevaluated_ids=('windows',)`）；硬门优先于 OR（AER-V02 端到端）；表达式 UNKNOWN；回执不合法；自审；witness 过期 / 边界相等即过期 / 用途不符 / scope epoch 变更 / 不可用 / 被遮蔽；三种非 ACCEPT 结论（参数化）；record 绑到别的 package / 别的 input manifest；package 绑到旧要求版本；purpose 不符；在途关键操作无人负责；取消在途；方法采用失效；compound 四条（happy、缺必需 occurrence 并给出 id、非法方法、组合义务失败）；一次性给出所有 reason code 且不重复 |
| 重试与仲裁 | 14 | 首次评审；干净 ACCEPT 结束且不再耗预算；未变更候选不得重复抽样 → 升级；候选变更 → 允许重试；基础设施错误 → 允许重试（AER-V08）；ACCEPT 与 REJECTED 冲突 → 仲裁；3 比 1 多数仍仲裁；不同候选版本的不同结论不算冲突；预算耗尽 → STOP；升级梯子 独立→人工→停止；政策禁人工时直接停止；REJECTED → STOP；达到次数上限即使候选变更也升级；除 CONCLUDE 外都记在同一义务预算上 |
| 切片边界 | 1 | 读源码断言 import 段零 `store`/`commit`/`verifier_router`/`sqlite`/`requests`/`httpx` |

红→绿过程：先写测试文件，`pytest` 报 `ModuleNotFoundError: agent_orchestrator.verification.acceptance_rules`（确认为红）；实现后 100 绿 1 红，红的那条是并行 P1.1 审阅把 `ValidityWitness` 的 `USABLE` 收紧为必须同时 `CURRENT` + `READABLE`，遮蔽用例改用 `decision=BLOCKED` 后全绿。

## 2. 门槛结果

- `uv run --frozen --group dev --extra local-capacity pytest tests/orchestrator/full_target/test_acceptance_rules.py -q` → **101 passed**。
- 同目录整体回归 `tests/orchestrator/full_target` → **311 passed, 1 skipped**（skip 是既有的 pandaPIparser 未配置）。
- `ruff check` 两个文件 → All checks passed；`ruff format --check` → 2 files already formatted。
- `uv run --frozen --group dev mypy src/agent_orchestrator` → 17 errors，全部落在 `evaluation/agentdojo_bridge.py`、`evaluation/agentdojo_runner.py`、`evaluation/are_benchmark.py`、`runtime/deepseek_tokens.py`（缺可选依赖 stub 的既有债），本片零新增；`mypy src/agent_orchestrator/verification/acceptance_rules.py` 单独跑 → Success。
- 禁止 import grep：模块 import 段只有 `collections.abc` / `dataclasses` / `enum` / `typing` 与 `..contracts` 四个子模块；`store|commit|verifier_router|sqlite|requests|httpx` 在 import 段零命中（正文散文里出现两次，是"不做什么"的说明）。
- 未提交、未跑全量回归（按任务书要求）。

## 3. 与任务书/规范的偏差及理由

1. **`independence_ok(package, record, *, facts)` 多了一个必填关键字参数**。任务书写的是 `independence_ok(package, record)`，但 `ReviewPackage` 里没有"谁生产了这份候选"的字段，也没有 Reviewer 工作区写权限的字段——`candidate_refs` 只指向产物本身。在契约补字段之前，这些事实只能由调用方（P6.1 的 `review_coordinator`）传入，故新增 `IndependenceFacts`。见下节契约变更请求 1。
2. **`acceptable` 采用 `AcceptanceSubject` 打包 + 关键字参数**：`acceptable(subject, *, now_ms, purpose, witness, current_scope_epoch)`。公式有九段合取、十几个事实，散成位置参数既不可读也容易错位；打包后每个事实有名字，且 `AcceptanceSubject` 本身是 frozen dataclass，调用方无法"顺手补一个 True"。`current_scope_epoch` 是 I19 必需的外部输入（witness 自带的是它被签发时的 epoch），不能从 subject 推。
3. **重试域的类型是本片新建的**（`ReviewAttempt`/`ReviewHistory`/`RetryAction`/`RetryReason`/`RetryDecision`）。契约里已有的 `ReconciliationResult`/`may_rehandoff` 是**操作重交接**的合同（§14.3），与**审阅重试**是两件事，不能复用，否则正好犯"四个事实互相替代"的错。见契约变更请求 2。
4. **"逐准则一一匹配的三种拒绝"里，重复 ID 只能测契约层**。`ReviewRecord.__post_init__` 已经拒绝重复 `criterion_id`（"record.criteria must not repeat a criterion_id (AER §5.4)"），所以纯规则层根本收不到这种输入；对应测试断言 `ContractError`，而未知 ID 与遗漏必需 ID 由 `CompletenessResult.match` 报出。
5. **"回执必须是 dispatcher/工具来源"用 `TypedRefKind` 判定**。契约里没有"这条证据是模型写的"这一显式字段，唯一可用的来源信号是 `TypedRef.kind`。本片把 `tool_receipt` 与 `operation` 定为 dispatcher 来源（常量 `DISPATCHER_RECEIPT_KINDS`），其余（`knowledge`/`source`/`artifact`/`review`/`observation`）一律不算——`observation` 也排除，因为观察记录自身的来源真实性在 `ObservationRecord` 上，而 `CriterionOutcome` 只看得到一个引用。见契约变更请求 3。
6. **要求回执的范围略宽于"具名检查"**：`evaluation_kind ∈ {EXECUTION_RECEIPT, DETERMINISTIC, FORMAL}` 的准则即使 `required_check_ids` 为空也要求回执。理由是 AER §5.4 的"Receipt 来自真实 dispatcher/工具而非模型生成的字符串"针对的是这三类可机器核对的判断；纯 `SEMANTIC` 准则本来就靠独立语义 Review 而不是回执。
7. **`witness` 的时效判定改为委托给契约的 `ValidityWitness.is_fresh_for`**。写实现时参考测试（`now=10, not_after=10` 应当拦截）与当时契约的 `now_ms > not_after_ms` 不一致，本打算在本模块内自行实现；随后发现并行的 P1.1 审阅已把契约改成 `now_ms >= not_after_ms`（注释明确写了"exclusive, matching the annex reference"），于是删掉重复实现，直接调用，保持单一事实来源。测试 `test_witness_expiry_boundary_blocks` 把这条边界钉住，防止以后被改回去。
8. **未实现 §6.2 里的 "DATA 精确绑定"**。它需要 `DataRequirement`/`BoundInput` 的实际解析与输入版本比对，属于 P2.x 图引擎的范围；本片在 `CompoundFacts` 上留了 `composition_obligation_passed` 作为接入点，没有伪造一个恒真检查。

## 4. 契约变更请求（不自行修改 `contracts/`）

1. **`ReviewPackage` 缺少候选生产者身份与 Reviewer 权限事实**。建议加 `producer_agent_ids: tuple[str, ...]`（或 `candidate_author_refs: tuple[TypedRef, ...]`，kind=`task`）与 `reviewer_workspace_policy`（至少一个"Reviewer 对候选工作区只读"的布尔或 policy ref）。理由：AER §5.3 的独立性最低要求是**接受公式的一个合取项**，它依赖的事实现在完全在契约外，等于把 AER-V05 的判据交给调用方自证。本片以 `IndependenceFacts` 临时承载。
2. **审阅重试/仲裁缺少契约类型**。建议在 `contracts/resolution.py` 加 `ReviewAttempt` 与 `ReviewBudgetState`（或等价物），把"同一义务预算"和"同一候选 content hash"做成线上字段。理由：AER §5.4 的"禁止重复抽样直到 PASS"与"冲突不投票"需要跨多次 Review 的持久事实；现在只能由调用方自行拼装，无法被 ledger 审计。
3. **证据来源没有显式的"非模型产出"标记**。建议给 `CriterionOutcome` 的证据项加来源枚举（如 `EvidenceOrigin ∈ {DISPATCHER, TOOL, EXTERNAL_AUTHORITY, MODEL_ASSERTION}`），或在 `TypedRef` 上加 `produced_by`。理由：目前只能靠 `TypedRefKind` 间接推断，模型只要把自己的结论写成 `kind="tool_receipt"` 的引用就绕过了本片的检查——真正的拦截必须在写入证据时发生，纯规则层只能做二次防线。本片的 `DISPATCHER_RECEIPT_KINDS` 是该二次防线。
4. **`RequirementsRevision` 与 `ReviewPackage` 之间没有强制的准则覆盖关系**。`ReviewPackage` 自带 `criteria`，与 `binding.requirements_revision` 只靠一个整数关联，因此"包里少了一条根准则"（AER-V01）在契约层是合法的。本片在 `acceptable` 里用 `revision.required_criterion_ids() ⊆ package.criterion_catalogue()` 拦下并给出 `ROOT_CRITERION_MISSING`；若希望更早拦截，建议 `ReviewPackage` 增加 `requirements_content_hash` 并在构造时校验覆盖。

## 5. 后续小片的接入点

- **P6.0（`verifier_router` 绑定）**：直接用 `required_checks_complete` 与 `project_verdict`，不要在 router 里再写一份 verdict/execution 的合并逻辑。
- **P6.1（`review_coordinator`）**：`retry_policy` 与 `independence_ok` 是它的决策内核；`IndependenceFacts` 由它填，填完应尽快推动契约变更请求 1。
- **P2.3c（`resolution_commits` 根 GoalResolution）**：按任务书用本模块判"缺根要求"，即 `acceptable(...).reasons` 含 `ROOT_CRITERION_MISSING` / `SUCCESS_EXPRESSION_NOT_PASS` 即不得写根 Resolution。
- **P3.5（validity 求值与 Witness）**：`_witness_reasons` 现在只消费 `ValidityWitness`；witness 怎么算出来仍在 P3.5，本片不越界。

## 6. 审阅修复（2026-09-16，独立审阅"需修后合并"）

审阅结论：必改 1 项、应改 3 项、可选 3 项。修复后 `tests/orchestrator/full_target/test_acceptance_rules.py` 由 101 条增至 **108 条**，全绿；同目录整体 520 passed / 1 skipped；`ruff check`、`ruff format --check` 通过；`mypy src/agent_orchestrator` 仍是 17 errors（4 个既有缺 stub 文件，检查文件数由 157 增至 159，本片零新增），本模块单跑 Success。未提交、未跑全量回归、未改 `contracts/`。

### 必改（高）

**F1 `AcceptanceSubject.independence` / `posture` 去掉默认值。**
审阅指出的是真缺陷：`IndependenceFacts()` 的语义是"没有人生产过这份候选、Reviewer 无任何权限"，`ExecutionPosture()` 的语义是"无在途、未取消、采用有效"——两者都是**最宽松的可能世界**，加了 `default_factory` 就等于把"忘了去查"翻译成"可以接受"。现已改为必填字段（`dataclass` 无默认），并在类 docstring 写明原因。
新增测试 `test_acceptance_subject_demands_the_independence_and_posture_facts`：两者任缺其一都在构造 `AcceptanceSubject` 时 `TypeError`，连 `acceptable` 都进不去；配套 `test_stated_all_clear_facts_are_still_accepted` 保证"明确声明全清"仍是合法路径，避免矫枉过正。

### 应改（中）

**F2 `acceptable` 增加 package 与 revision 的成功表达式一致性校验。**
新增纯函数 `success_expression_digest(expression)`（`content_hash_of(expression.to_json())`）与 reason code `AcceptReason.SUCCESS_EXPRESSION_MISMATCH`，校验位置在 `acceptable` 的第 1b 步——即身份校验之后、`required_checks_complete`（第 5 步）之前，符合审阅要求的顺序。
审阅指出的攻击面确实存在：`ReviewPackage.__post_init__` 只校验表达式的**结构**与准则 ID 是否在目录内，**不**校验"硬约束必为独立 AND 合取项"（那条只在 `RequirementsRevision` 上），所以把 package 的表达式从 `all(all(win,linux), privacy)` 改成 `any(all(win,linux), privacy)`，缺席的 privacy 就会被 `_criteria_only_under_any` 判成"合法未采用的 OR 分支"，`required_checks_complete` 返回 `complete=True`。
新增 `test_package_expression_must_match_the_requirements_revision` 复现这条路径：断言 `SUCCESS_EXPRESSION_MISMATCH` 出现，**同时**断言 `completeness.declined_ids == ("privacy",)` 且 `completeness.complete` 为真——把"没有这条校验会漏掉什么"一并钉在测试里。
另有 `test_a_reshaped_but_equivalent_package_expression_is_still_a_mismatch`：把 `all(all(a,b), c)` 拍平成 `all(a,b,c)` 也算不一致（`reasons` 恰好只有这一条），因为身份按 canonical JSON 判，不做语义等价推理；`test_success_expression_digest_separates_the_operators` 钉住 `all` 与 `any` 的摘要必须不同。
注：`ReviewPackage` 目前没有 `requirements_content_hash` 字段，故只比表达式摘要；代码注释已写明该字段出现后应一并比对，对应契约变更请求 4。

**F3 表达式参数标注改为 `SuccessExpression`，非法节点显式抛 `ContractError`。**
`evaluate_success_expression` 与 `_criteria_only_under_any` 的 `Any` 全部换成 `contracts.resolution.SuccessExpression`；两处 `walk` 在遇到既非 `CriterionExpr` 也非 `AllExpr`/`AnyExpr` 的节点时抛 `ContractError`（消息统一由 `_illegal_node` 生成，带 §25.1 裁决 2 出处），不再靠 `node.children` 抛 `AttributeError`。`success_expression_digest` 同样先校验节点类型。模块因此新增一处 `from ..contracts.models import ContractError`（仍在允许的 `contracts` 范围内，禁止 import grep 依旧零命中）。
新增 `test_an_unparsed_expression_is_refused_not_walked`（把**未解析的原始 JSON** 直接喂进来——这是最可能发生的真实误用）与 `test_an_illegal_nested_node_is_refused`（合法 `AllExpr` 里混进一个裸字符串子节点）。

**F4 `_criteria_only_under_any` 的重复副本加注释。**
函数 docstring 已写明它与 `contracts/resolution.py` 的私有同名助手逐行重复、副本是**故意且临时**的（P1.1b 不得改 `contracts/`），并留下 "To do: 契约助手公开化后删掉本副本并改为 import"，指向本记录 §4。公开化请求由协调方汇总转交 P1.1 持有者。

### 可选（低）——本次不改，记为已知取舍

1. **`INCONCLUSIVE` 且候选已变更仍走升级而非 `RETRY`。** 只有 `REWORK` 认 `candidate_changed_since_last_attempt`。理由：`REWORK` 是"缺陷明确、可以改"，改完再评审是正常闭环；`INCONCLUSIVE` 是"当前证据不足或冲突无法解决"，改候选并不消除证据不足这个原因，直接重评审在语义上接近"换个样本再试"。若后续发现 INCONCLUSIVE 的成因常是候选本身，再放开这条即可，届时须补一条"不得借改候选绕过升级"的测试。
2. **`_conflicting` 只把"含 ACCEPT 的分歧"判为冲突**，`REJECTED` vs `REWORK` 不触发仲裁。理由：AER §5.4 的仲裁要挡的是"有人说可以接受、有人说不行"；两个都不接受的结论对**是否放行**没有分歧，差别在返工范围，走 `REWORK` 的正常路径即可。代价是"这份候选是否还值得救"的分歧目前不会自动进仲裁。
3. **预算耗尽时若同时存在冲突，返回 `ARBITRATE` 且 `consumes_budget=True`。** 冲突检查排在预算守卫之前，是刻意的：预算用光不该把一个未解决的冲突默默留在原地（那等于"没人负责"）。代价是这一步可能透支预算，需要由调用方的预算账本兜底。若 §21.5 的守恒要求严格不透支，应改为返回 `STOP(BUDGET_EXHAUSTED)` 并单独记录"冲突未仲裁"标记。

## 7. 契约第三轮落地后的适配（2026-09-16）

协调方通知契约第三轮已合入，本节记录 4 项适配。只改了 `verification/acceptance_rules.py` 与本片测试；`contracts/` 只读。测试由 108 条增至 **122 条**，全绿。

### A1 删除 `_criteria_only_under_any` 私有副本

契约已把助手公开为 `contracts.resolution.criteria_only_under_any()`，并在 `ReviewPackage` 上提供方法 `criteria_only_under_any()`。副本连同 §6/F4 的 "To do" 注释一并删除；`required_checks_complete` 改调 `package.criteria_only_under_any()`——用**包自己的**方法而不是自由函数，是为了让"哪些分支可以不采用"只有一个说法，调用方不可能拿另一棵表达式来算。
新增 `test_declined_branches_come_from_the_contract_helper`：先断言模块上已经没有 `_criteria_only_under_any`，再断言 `declined_ids` 恰等于"包声明的可弃集合 − 记录里实际报了的准则"。写第一版时把 `declined_ids` 直接当成可弃集合断言而挂掉——两者不是一回事（可弃集合是表达式的性质，`declined_ids` 是本次记录真的没报的那些），修正后的断言把这个区别也钉住了。

### A2 `independence_ok` 交叉核对 package 与 facts

`ReviewPackage` 现有 `producer_agent_ids`、`reviewer_workspace_access`（`WorkspaceAccess`，`WRITE` 在构造期即拒）与 `produced_by(agent_id)`。`IndependenceFacts` 按要求**保留为必填输入**，两边改为交叉核对：

- 自审判定改为"任一来源知道即算"：`reviewer in facts.producer_agent_ids or package.produced_by(reviewer)`。谁都可以指认，谁都不能单独洗清。
- 新增 reason code `IndependenceReason.FACTS_CONTRADICT_PACKAGE`（值 `"INDEPENDENCE_FACTS_CONTRADICT_PACKAGE"`），两类矛盾触发：package 声明了生产者且与 facts 的集合不等；facts 声称 Reviewer 可写而 package 记的是 `NONE`/`READ_ONLY`（`WRITE` 构造期已拒，所以 package 永远不可能同意这个说法）。
- **不做**"以 package 为准"或"以 facts 为准"的消解：package 是打包时冻结的锚，facts 是协调方此刻观察到的，两者不一致本身就是结论——不知道哪边错的时候，这次审阅就不算独立。

新增 4 条测试：package 指认自审而 facts 漏报（同时给出 `SELF_REVIEW` 与矛盾码）、双方生产者集合不等（只有矛盾码）、facts 声称可写与 `READ_ONLY` 包矛盾、双方一致则独立；另加 1 条 `test_package_refuses_a_reviewer_with_write_access` 钉住契约构造期拒绝 `WRITE`。
既有的 `test_independence_reports_every_broken_requirement_at_once` 相应更新：它传 `reviewer_can_write_candidate=True` 而包是默认 `READ_ONLY`，现在会**多**报一条矛盾码——这是新行为的正确表现，测试里加了注释说明来由，没有为了让断言通过而放宽成 `>=`。

### A3 `acceptable` 用 `requirements_content_hash`，并并入结构违规

- 新增 reason code `REQUIREMENTS_CONTENT_MISMATCH`。第 1b 步改为：`package.requirements_content_hash` 存在时必须等于 `revision.content_hash()`（强校验，同时覆盖准则目录而不只是表达式）；不存在时沿用原来的表达式摘要比对 `SUCCESS_EXPRESSION_MISMATCH`。两者是 if/elif，不重复报。
- 新增第 1c 步与 reason code `HARD_CONSTRAINT_STRUCTURE`：并入契约的 `package.hard_constraint_violations()`，违规准则 id 挂在 `AcceptDecision.hard_constraint_structure_ids` 上。这一步对"未声明 `requirements_content_hash` 的包"才有实际作用——契约只在包声明了 hash 时才在构造期强制该结构，而**未声明**正是被篡改的锚会长的样子。
- 之所以为 content hash 单开一个 code 而不是复用 `REQUIREMENTS_REVISION_MISMATCH`：后者已经表示"版本号/mission 对不上"，两种失败的处置不同（一个是接错了版本，一个是内容被换过），合并会让调用方无法分辨。

新增 4 条测试：声明 hash 且相等 → 可接受；声明 hash 但不等 → `REQUIREMENTS_CONTENT_MISMATCH` 且**不**再报 `SUCCESS_EXPRESSION_MISMATCH`（验证 elif 分支）；声明 hash 的包把硬约束塞进 `any` → 契约在构造期就 `ContractError`；未声明 hash 的包同样篡改 → `HARD_CONSTRAINT_STRUCTURE` 且 `hard_constraint_structure_ids == ("privacy",)`。既有的 `test_package_expression_must_match_the_requirements_revision` 一并补上结构码断言。

### A4 证据来源改为以 `produced_by` 为主、`kind` 为回退

`TypedRef`/`EvidenceRef` 现有 `produced_by: Provenance | None`，且契约的 `reject_model_claimed_provenance` 保证模型提交的载荷只能自认 `model`/`human`——所以 attribution 是可信信号，`kind` 不是（模型可以自己写 `kind: "tool_receipt"`）。

新增公开枚举 `ReceiptSource` 与纯函数 `receipt_source(outcome)`，三态：

| 值 | 条件 | 判定 |
|---|---|---|
| `DISPATCHER_ATTRIBUTED` | 任一证据 `produced_by ∈ {SYSTEM, TOOL}`（常量 `DISPATCHER_PROVENANCE`） | 合格回执 |
| `PROVENANCE_UNKNOWN` | 无任何 dispatcher attribution，但有**未标注**且 `kind ∈ DISPATCHER_RECEIPT_KINDS` 的证据 | 走回退路径，**接受但标记** |
| `NOT_A_RECEIPT` | 其余，含"`kind` 是 `tool_receipt` 但 `produced_by=MODEL`" | 不合格 |

关键点是第三行：**已标注为 MODEL 的 `tool_receipt` 直接判不合格**，不再因为 kind 好看就放行——这正是本条规则存在的理由。`CompletenessResult` 新增 `provenance_unknown_ids`；按协调方要求，回退路径**不**影响 `complete`，只作为 caveat 透出，供 P6.0/P6.1 决定是否要求补标注。

新增 4 条测试：`receipt_source` 四种输入的分类（含 `kind=knowledge` 但 `produced_by=TOOL` 应判合格——attribution 优先于 kind）；模型自认的 `tool_receipt` 仍进 `unsourced_ids` 且 `complete=False`；已标注回执无 caveat；未标注回执走回退、`complete=True` 且进 `provenance_unknown_ids`。测试夹具相应拆成 `tool_receipt`（未标注，旧形态）/`attributed_receipt`（`TOOL`）/`forged_receipt`（`MODEL`）三种。

### 门槛

- `pytest tests/orchestrator/full_target/test_acceptance_rules.py -q` → **122 passed**。
- `ruff check` / `ruff format --check`（两个文件）→ All checks passed / already formatted。
- `mypy src/agent_orchestrator/verification/acceptance_rules.py` → Success。
- `mypy src/agent_orchestrator` → 20 errors；其中 17 条是原有 4 个缺 stub 文件，另 3 条在 `graph/projection_validation.py`（`task_network` 子代理在途，非本片）。本片零新增。
- 同目录整体回归：`test_projection_integrity.py` 当前**收集失败**（同样是 `graph/task_network.py` 子代理在途，与本片无关）；`--ignore` 该文件后 **605 passed, 1 skipped**。
- 禁止 import grep 仍零命中：新增的 `from ..contracts.models import ContractError` 与 `Provenance/TypedRef/content_hash_of` 都在允许的 `contracts` 范围内。
- 未提交，未跑全量回归。
