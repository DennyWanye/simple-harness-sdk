# P2.1c 实施记录（纯 `evaluate_readiness` 与 `EligiblePrimitiveTask` 构造）

日期：2026-09-16｜基线：SDK main `623d4c8`（工作区含 P1.2 / P2.1 / P2.2b 等并行切片的未提交产物，本切片零触碰）｜执行：单个 Opus 子代理｜测试先行

规范来源：
主计划 `simple_harness/plans/taskSys2/升级planV1/v1.4/simpleharness-full-target-1.4/complete-plan.zh-CN.md`
§11.5（ValidityWitness 与 epoch 屏障）、§18.5（兼容接线与必须拒绝的旁路，含 v1.3 第 4 条硬约束）、§23（P2.1c 行：`graph/eligibility.py` 纯 `evaluate_readiness` 读 purpose=START 的 ValidityWitness，不接 allocator、不调账本）、§24.1 裁决 6（三层 frontier / READY 降为可重建索引 / `EligiblePrimitiveTask` 不是安全令牌）；
附件 `taskGraph/simpleharness-taskgraph-design.zh-CN.md` §8.1–8.3（三层集合、八种理由、真实 dispatch 绑定哪些版本）、§9（前提检查阶段 SELECT/START、MAINTAIN、ACCEPT）、§11.2（完整 read-set，"不存在某对象/边/写者"也是读取事实）、§11.5（旧在途工作：撤销旧 generation 派发资格；外部操作结果未知则阻塞冲突后继）；
附件 `taskGraph/simpleharness-taskgraph-implementation-design.zh-CN.md` §6（PlanningFrontier vs ExecutionFrontier、允许派发的十条必要条件、readiness 缓存附 read-set）。

## 1. 交付清单

### 生产代码（1 个新文件，未改动任何既有文件）

| 文件 | 行数 | 主要类型与函数 |
|---|---:|---|
| `src/agent_orchestrator/graph/eligibility.py` | 1442 | `ReadinessReason`(12)、`READINESS_PRECEDENCE`、`PLANNING_REASONS` / `EXECUTION_REASONS` / `ADMITTED_DISPATCH_REASONS`；`EffectOutcome`(6) + `UNSETTLED_EFFECTS`、`OccurrenceOutcome`(6)、`ApprovalState`(4)、`CandidatePolicy`(4)、`order_released()`；输入视图 `TaskView` / `ActivePlanView` / `EvidenceView` / `PendingOperation`；输出 `ReadinessDetail` / `ReadinessReport`；九个门 `_integrity_gate` / `_refinement_gate` / `_selection_gate` / `_order_gate` / `_data_gate` / `_evidence_gate` / `_approval_gate` / `_stale_gate` / `_operation_gate` + `_witness_verdict`、`start_preconditions()`；`build_read_set()`；`evaluate_readiness()`；`stale_after()`；`NotEligible` / `EligibilityGateBypassed` / `EligiblePrimitiveTask` / `admit_for_dispatch()`；`LegacyReadyVerdict` / `legacy_ready_is_not_eligibility()`；`PlanningFrontier` / `ExecutionFrontier` / `AdmittedDispatch` |

只读依赖：`contracts/{htn,evidence_state,resolution,obligations,models,semantic_base}`、`graph/{task_network,projection_validation}`、`artifacts/input_bindings`。
零 `storage` / `scheduling` / `sqlite` / orchestration store import（由测试逐行扫描 import 语句钉住）；不调账本、不调模型、不改任何状态。
`graph/__init__.py`、`graph/task_network.py`、`graph/projection_validation.py`、`contracts/`、`scheduling/allocator.py` 逐字节未动（`git status` 只多出本切片两个新文件）。

### 红测试（1 个新文件，121 个测试函数 / 164 条参数化用例）

`tests/orchestrator/full_target/test_readiness_reasons.py`（1605 行），分十五节：

| 节 | 条数 | 覆盖 |
|---|---:|---|
| 1 GRAPH_INTEGRITY | 5 | 缺语义绑定（**不是 legacy fallback**）；快照不含该 occurrence；投影不可拓扑（`GraphIntegrityError` 一挂，compound 与 primitive 一起停）；绑定指向另一个 task；健康网络不报此因 |
| 2 NEEDS_REFINEMENT | 11 | compound → NEEDS_REFINEMENT；**遍历 `TaskStatus` 七个旧状态字符串（含 READY）仍 NEEDS_REFINEMENT**；其余门全过时仍 NEEDS_REFINEMENT；primitive 不报此因；compound 的报告永远进不了 `admit_for_dispatch` |
| 3 NOT_SELECTED | 9 | 采用集合外的 occurrence；Mission 不接新工作；义务 SATISFIED / CANCELLED / SUPERSEDED（无获准 demand）；候选策略 RUNNING_WORK_EXISTS / ATTEMPTS_EXHAUSTED / WITHDRAWN；正常情形不报此因 |
| 4 WAITING_ORDER | 20 | `order_released` 六种 outcome × 两种 release_condition 的**完整真值表**；RUNNING 前置；**FAILED / CANCELLED 对 accepted 不算满足**；settled_terminal 合同下 FAILED 放行；**UNKNOWN 在两种合同下都不结算**；前置无观测结果记为 `order_outcome_unobserved` 而非放行；ACCEPTED 放行；细节指名具体前置 |
| 5 WAITING_DATA | 8 | 输入解析缺失（`input_resolution_missing`，独立于"失败"）；PENDING_PRODUCER；NOT_DISCLOSABLE；两者 problem kinds 互不相同；manifest 未冻结；必需端口只有 provisional 绑定；有 manifest 但带 problems 仍等待；干净冻结 manifest 放行 |
| 6 证据三因 | 23 | witness 缺失；**purpose 必须是 START**（PLAN/MAINTAIN/ACCEPT/CONTEXT/DISCLOSE/RECOVERY 六种全部拒绝）；TRUE+USABLE 放行；truth UNKNOWN/CONFLICT/FALSE；decision BLOCKED；decision/availability UNAVAILABLE → **OBSERVER_UNAVAILABLE 而不是 WAITING_EVIDENCE**；观察服务整体不可达；epoch 提升 → VALIDITY_RECHECK_PENDING；not_after 到期；scope 无法确认 epoch；epoch 一致且未到期不触发复验；无 deadline 的 witness 可用；**MAINTAIN 阶段前提不在 START 检查**；未声明 phase 默认 SELECT 会被检查；多前提时 OBSERVER_UNAVAILABLE 优先于仅不可用的 witness（与声明顺序无关） |
| 7 WAITING_APPROVAL | 4 | PENDING；REFUSED（**不是任务失败**）；GRANTED 放行；NOT_REQUIRED 放行 |
| 8 STALE_BINDING | 5 | dispatch_generation 变动；input_binding_revision 变动；两种来源同时出现时分别报告；一致时不报；**计划没有记录该值时不凭空造出过期** |
| 9 WAITING_OPERATION_UNKNOWN | 9 | UNKNOWN 且冲突；PENDING 且冲突；PARTIAL 且冲突；UNKNOWN 但不冲突不阻塞；APPLIED / NOT_APPLIED / NOT_HANDED_OFF 不阻塞；**UNKNOWN 不被折算成 WAITING_DATA 或 OBSERVER_UNAVAILABLE**；细节指名 operation_id |
| 10 READY_CANDIDATE 与理由独立性 | 6 | 正常路径 READY_CANDIDATE 且 details 为空；**12 个理由值互不相同、无合并**；`READINESS_PRECEDENCE` 恰好覆盖每个理由一次；报告带 occurrence/task/时间戳；read-set 记录八个通道；被拒的报告同样带 read-set |
| 11 EligiblePrimitiveTask | 14 | **直接构造被拒**；猜 token 构造被拒；从 READY 报告可构造；五种非 READY 报告全部拒绝；绑定 contract_revision/hash、dispatch_generation、input_binding_revision、manifest hash、method instance、requirement_refs、acceptance ids、read_set、plan_revision、mission；冻结；未冻结 manifest 抛 `ManifestNotFrozen`；别的 consumer 的 manifest 被拒；判别别的 occurrence 的报告被拒；**docstring 写明 "not a security token" 与 handoff 复查** |
| 12 stale_after | 13 | 未变不失效；requirements_revision / manager_epoch / budget_grant_revision / task 合同版本 / method instance hash / fact 版本 / acceptance hash / support set 摘要 / scope epoch / absence range **十个通道各一条**；读集项消失也算变化；当前观测更宽不算变化 |
| 13 三层 frontier | 10 | compound 进 planning、不进 execution；READY primitive 进 execution、不进 planning；等 ORDER 的 primitive 两边都不进；未被采用的 occurrence 两边都不进；图损坏两边都空；planning 按理由分组；**AdmittedDispatch 恒为空**；`ADMITTED_DISPATCH_REASONS == frozenset()`；READY_CANDIDATE 不在 PLANNING_REASONS |
| 14 纯度、隔离与 legacy 辅助 | 18 | import 行逐条扫描四个禁止词（storage / scheduling / sqlite / orchestration_store）；两次调用结果相等；不改输入；**七个 TaskStatus 值都不放行派发**；compound 由 legacy 辅助返回 NEEDS_REFINEMENT；primitive 由它退回 `evaluate_readiness`；`now_ms` 是 keyword-only；报告冻结；`TaskView` 拒绝空 occurrence id |
| 15 变异自证 | 9 | 逐个把九个门 monkeypatch 成"永不拒绝"，断言判定随之改变 |

红→绿过程：先只写测试文件，`pytest` 报 `ImportError: cannot import name 'eligibility'`（红）；实现后 162 绿 2 红——两条都是测试自身的构造缺陷（`report_for` 把 `input_result=None` 当成"用默认值"，以及"未被采用的 occurrence"用例误取了本来就被采用的根 occurrence），修正测试后 164 全绿。

**变异测试（在套件内，不是手工改源码）**：第 15 节九条用例分别用 `monkeypatch` 把 `_integrity_gate` / `_refinement_gate` / `_selection_gate` / `_order_gate` / `_data_gate` / `_evidence_gate` / `_approval_gate` / `_stale_gate` / `_operation_gate` 换成恒返回 `None` 的空门，先断言原判定成立、再断言换门后判定改变（八条变成 `READY_CANDIDATE`，整性门那条变成"不再是 GRAPH_INTEGRITY"）。九个门全部是承重的，且这组自证每次跑测试都会重新验证，不依赖人工记录。为此 `evaluate_readiness` 里的门是按名字逐个调用的，没有在 import 期把它们冻进一个元组。

## 2. 与规范的关键对应

- **§18.5 第 4 条硬约束 / 实现稿 §6**：`_refinement_gate` 的判据只有 `binding.form is TaskForm.COMPOUND`，既不看 `orchestration_semantics_version`，也不看 `TaskView.legacy_status`——后者只进诊断文案。`legacy_ready_is_not_eligibility()` 把同一条规则写成给 P2.3c allocator 用的显式辅助：`admits_dispatch` 恒为 `False`，compound 的 `gate_reason` 是 `NEEDS_REFINEMENT`，primitive 的 `gate_reason` 是 `None` 并在 `explanation` 里点名"去跑 evaluate_readiness"。
- **§24.1 裁决 6（三层 frontier）**：`PlanningFrontier` / `ExecutionFrontier` 是从快照 + readiness 结果算出的纯查询，只读不改；`AdmittedDispatch` 只提供 `not_decided_here()`，`ADMITTED_DISPATCH_REASONS` 是空集且被测试钉住——没有任何 readiness 理由本身构成准入。预算、租约、物理容量在本模块一个字节都没读。
- **§24.1 裁决 6（READY 降为可重建索引）**：`ReadinessReport` 不持有任何状态，`EligiblePrimitiveTask` 的 docstring 第一句就是"不是安全令牌"，并写明 dispatch 事务与工具/Provider handoff 前都要重新复查。
- **TG §8.2（理由不合并）**：12 个理由值互不相同。"输入缺失"是 `WAITING_DATA` 的 `input_resolution_missing`，"观察服务不可用"是独立的 `OBSERVER_UNAVAILABLE`，"外部操作未决"是独立的 `WAITING_OPERATION_UNKNOWN`；三者各有正反例，且有一条专门断言 UNKNOWN 的操作不会被折算成前两者。
- **TG 决策 1（ORDER 释放）**：`order_released()` 是公开的六×二真值表函数。ACCEPTED 对两种合同都释放；FAILED / CANCELLED / SETTLED_OTHER 只在 `settled_terminal` 下释放；**UNKNOWN 在两种合同下都不释放**；前置完全没有观测结果时报 `order_outcome_unobserved`，不当成已释放。
- **TG §4.3（DATA）**：`_data_gate` 原样携带 `input_bindings` 的 `ResolutionProblemKind` 作为 detail code（`ReadinessReport.data_problem_kinds` 把它们解回枚举），并另加三条自有判据：manifest 缺失、`is_frozen` 为假、`required_ports_satisfied()` 为假（必需端口只有 provisional 绑定时可以跑但不算 DATA 已满足）。
- **§11.5 + TG §9（证据与 epoch 屏障）**：`start_preconditions()` 用 `phase_check_points()` 取出所有会在 SELECT/START 检查的前提（未声明 phase 的默认包含 SELECT），逐条取 `purpose=START` 的 `ValidityWitness`。检查顺序是：purpose → scope epoch 是否可确认 → epoch 是否一致 → `not_after_ms` 是否到期 → 可读性 → truth → decision。多个前提同时出问题时按 `OBSERVER_UNAVAILABLE > VALIDITY_RECHECK_PENDING > WAITING_EVIDENCE` 定档，因此判定与前提声明顺序无关。
- **TG §11.2 / 实现稿 §6（read-set）**：`build_read_set()` 无论判定结果如何都产出 `SemanticReadSet`，覆盖 requirements_revision、task 合同（id + revision + contract_hash）、method instance（plan_revision + 内容 hash）、fact（witness id + support_revision + 内容 hash）、acceptance（manifest 每条绑定的 acceptance_id + support_revision + content_hash）、manager_epoch、budget_grant_revision、support set 摘要、scope epochs，以及一条 `AbsenceRead("conflicting_unsettled_operation", scope, operation_range_revision)`——"这个范围里没有冲突的未决写者"本身是一次读取，没有范围版本的话，扫描之后才登记的冲突操作会让缓存判定看起来仍然新鲜。`stale_after()` 对这十个通道逐项比对，任一变化即失效；读集项消失也算变化，当前观测更宽则不算。
- **TG §8.3（真实 dispatch 绑定什么）**：`admit_for_dispatch()` 产出的 `EligiblePrimitiveTask` 绑定 mission/plan_revision、occurrence/task/obligation、contract_revision + contract_hash、局部 `dispatch_generation`、`input_binding_revision`、`InputManifest.manifest_hash()`、adopted method instance id、`requirement_refs`、acceptance ids 与整份 read-set，并记录 `admitted_at_ms`。manifest 未冻结时让 `ManifestNotFrozen` 直接抛出而不近似。
- **§18.5（缺绑定是损坏）**：`_integrity_gate` 把"缺 `TaskSemanticBindingV1`"、"快照不含该 occurrence"、"绑定与 occurrence 互指不同 task"、"form 不一致"、"投影不可拓扑"全部报成 `GRAPH_INTEGRITY`，没有任何一条退化成"照常派发"。

## 3. 偏差与裁定（本切片自行裁定，均已用测试钉住并在代码里写明理由）

1. **compound 拦截排在采用性检查之前**。`NEEDS_REFINEMENT` 门先于 `NOT_SELECTED` 门运行，因此一个不在采用集合里的 compound 也会返回 `NEEDS_REFINEMENT` 而不是 `NOT_SELECTED`。理由：拒绝派发 compound 是安全属性，不应取决于它当下是否被采用；采用性由两个 frontier 查询在成员资格阶段施加（`PlanningFrontier` / `ExecutionFrontier` 都先过 `plan.is_adopted()`）。第 13 节有一条用例专门覆盖"未被采用的 compound 理由是 NEEDS_REFINEMENT，但两个 frontier 都不收"。
2. **epoch 屏障排在 witness 可用性之前**。一份 `decision=BLOCKED` 且 epoch 已过旧的 witness 返回 `VALIDITY_RECHECK_PENDING` 而不是 `WAITING_EVIDENCE`。依据 §11.5"消费者使用缓存前必须核对 witness.epoch"——在重算之前，对这份缓存结论下任何结论都是不成立的。
3. **`WAITING_OPERATION_UNKNOWN` 阻塞 PENDING / PARTIAL / UNKNOWN 三种效果，不只 UNKNOWN**。实现稿 §6 的必要条件写的是"没有冲突的未决 Operation"，PENDING 与 PARTIAL 都属于未决；`NOT_HANDED_OFF` 不阻塞（什么都没发出去，不存在现实冲突），`APPLIED` / `NOT_APPLIED` 已决亦不阻塞。detail code 携带具体的 `operation_<outcome>`，所以"是哪一种未决"并没有被理由名吃掉。
4. **观察服务整体不可达时无条件返回 `OBSERVER_UNAVAILABLE`**，即使该任务一条 START 前提都没有。理由：这是关于"这次判定本身是否可信"的事实，不是关于该任务的事实。
5. **`STALE_BINDING` 只在计划**记录了**当前值时才成立**。`ActivePlanView.dispatch_generations` / `input_binding_revisions` 里没有该键时不判过期——缺少观测不等于已变化（与 ORDER 门的 `order_outcome_unobserved` 是同一条原则的两个方向：前者不凭空造过期，后者不凭空放行）。
6. **签名形状**：规范写的是 `evaluate_readiness(TaskView, ActivePlan, Resolutions, Facts, InputBindings)`。实现把 facts/witnesses 连同"外部未决操作"与"support set 读集"合并成一个 `EvidenceView`（未决操作是被观测到的世界事实，不是计划的一部分），因此实际签名是 `evaluate_readiness(view, plan, resolutions, evidence, input_result, *, now_ms)`。语义项一个不少。
7. **门的运行顺序**按任务书列举的次序（整性 → 细化 → 采用 → ORDER → DATA → 证据 → 审批 → 过期 → 操作 → 就绪）落为 `READINESS_PRECEDENCE`，并作为公开常量导出；有一条用例断言它恰好覆盖 12 个理由各一次。

## 4. 契约变更请求

| # | 缺什么 | 现在怎么绕过 | 请求 |
|---|---|---|---|
| CR-1 | `contracts/resolution.py` 里 `OperationEnvelope` 只冻结不可变语义字段，**操作三轴中的 `effect_outcome`（NOT_HANDED_OFF / PENDING / APPLIED / NOT_APPLIED / PARTIAL / UNKNOWN）没有契约落点**（主计划 §14 与 AER 附件 §12–14 都定义了它，但当前包里只有 `ReconciliationOutcome`，语义相近而取值不同） | 在 `graph/eligibility.py` 里声明只读视图枚举 `EffectOutcome` + `PendingOperation(envelope, effect_outcome, conflicts_with)`，并在 docstring 写明它是视图、不是第二个写入权威 | 由 AER 切片把 control / effect_outcome / accounting 三轴与 current 控制字段记录一起放进 `contracts/resolution.py`，`eligibility` 改为 import；本模块不再自带枚举 |
| CR-2 | `ObligationAccountView` 有 `lifecycle`，但没有字段直接表达"当前有获准 demand"（§18.5 / 实现稿 §6 的必要条件之一） | 用 `lifecycle is UNSATISFIED` 作为代理判据，detail code 写成 `obligation_<lifecycle>` | 若"获准 demand"将来要与 lifecycle 解耦（例如"义务未满足但暂停出资"），请在 `ObligationAccountView` 上增列显式字段；否则请在契约 docstring 里确认代理判据成立 |
| CR-3 | 契约里没有"某个 Task 的审批状态"与"当前候选/Attempt 策略"的类型 | 在 `TaskView` 上以 `ApprovalState`(4) 与 `CandidatePolicy`(4) 两个本地枚举承载 | 授权/审批切片落地时给它们一个契约落点，`TaskView` 改为引用 |

另：`OccurrenceOutcome` 也定义在本模块，但**不**申请搬进契约——它和 `graph/task_network.RelationRow` 同类，是一次"读数"而不是可存储事实，存储侧的权威记录是 `Acceptance` / `GoalResolution` / Task 状态机。

## 5. 门槛

| 项 | 命令 | 结果 |
|---|---|---|
| 本切片测试 | `uv run --frozen --group dev --extra local-capacity pytest tests/orchestrator/full_target/test_readiness_reasons.py -q` | **164 passed** |
| full_target 目录（顺带确认未打断并行切片） | `uv run --frozen --group dev --extra local-capacity pytest tests/orchestrator/full_target -q` | **998 passed, 1 skipped**（skip 是 PANDA 未配置真实 parser，与本切片无关） |
| lint | `uv run --frozen --group dev ruff check <两个文件>` | All checks passed |
| format | `uv run --frozen --group dev ruff format --check <两个文件>` | already formatted |
| 类型 | `uv run --frozen --group dev mypy src/agent_orchestrator` | `eligibility.py` **0 error**；仓库仍有 20 条既存错误，全部在 `evaluation/`(17)、`runtime/deepseek_tokens.py`(1)、`planning/htn/validation.py`(2，属并行切片 P2.1 在途文件)，本切片未新增也未修改这些文件 |

未提交、未跑全量回归，符合本切片约束。

---

# 审阅修复（2026-09-16，独立审阅"需修后合并"）

审阅结论：门主体正确，5 个额外变异中 4 个被捕获。以下逐条对应修复；只改了 `graph/eligibility.py` 与本片测试，未提交。
契约第四轮已落地，本轮同时把三个本地视图枚举换成契约版。

## A. 契约对齐（第四轮落地后）

| 原本地定义 | 现在 |
|---|---|
| `eligibility.EffectOutcome`（6 值） | **删除**，改 import `contracts.resolution.EffectOutcome`；`PendingOperation` 由 `(envelope, effect_outcome)` 改为 `(envelope, state: OperationCurrentState)`，控制面与冻结语义按 AER §12.2 分表，构造时校验 `state.operation_id == envelope.operation_id`（有一条 `ContractError` 用例）。`effect_outcome` / `effect_settled` 成为委托属性。**CR-1 关闭** |
| `eligibility.ApprovalState`（4 值枚举） | **删除**，改 import `contracts.resolution.ApprovalState` + `ApprovalDecision`；审批门改用契约自带的 `is_effective(now_ms=...)`，因此"曾经批准过"与"现在被批准"分开（不变量 I09）：已过期的 GRANTED 报 `approval_expired_grant`。`TaskView.approval` 现在拒绝非契约类型。**CR-3 关闭（审批半边）** |
| `eligibility.CandidatePolicy`（4 值枚举） | **改名为 `DispatchCandidacy`**，见下方"偏差 8"——契约版 `CandidatePolicy` 是另一个概念，没有合并 |
| `lifecycle is UNSATISFIED` 代理判据 | 改读 `ObligationAccountView.has_admitted_demand`；lifecycle 与 demand 现在是两条独立判据、两个 detail code。**CR-2 关闭** |

## B. 必改（严重）

**1　`EligiblePrimitiveTask` 的 token 可被 `replace` / `copy` / `pickle` 携带出去** — 已修。
- `_token` 改为 `field(init=False, ...)`：不再是构造参数（有用例断言它不在 `inspect.signature` 里，且传入抛 `TypeError`）。
- 构造合法性改由 `ContextVar` `_ADMITTING` 把关：只有 `admit_for_dispatch()` 打开的那一瞬间允许构造，`finally` 立即复位；token 在构造**之后**由 `object.__setattr__` 写入，属性 `gate_passed` 读它。用 `ContextVar` 而不是模块全局，避免并发上下文互相看到对方的准入窗口。
- `__replace__`（3.13+ 的 `dataclasses.replace` 委托点）、`__copy__`、`__deepcopy__`、`__reduce__`、`__reduce_ex__` 一律抛 `EligibilityGateBypassed`。3.11/3.12 上 `dataclasses.replace` 走构造器，被 `_ADMITTING` 守卫拦住，**两条路径抛同一个异常**，拒绝行为不随解释器版本变化。
- 新增三条拒绝用例：`dataclasses.replace(admitted, task_id=…, occurrence_id=…)`、`copy.copy` / `copy.deepcopy`、`pickle.dumps`。
- 另：`manifest.manifest_hash()` 移到打开准入窗口**之前**调用，未冻结的 manifest 抛 `ManifestNotFrozen` 时窗口从未被打开。

**2　`_witness_verdict` 缺 `consumer_ref` 归属检查** — 已修。
按 `artifacts/input_bindings._check_witness` 的同一条规则（`WITNESS_CONSUMER_MISMATCH`）：`consumer_ref.kind` 必须是 TASK 且 `consumer_ref.id == str(binding.task_id)`，否则 `WAITING_EVIDENCE` + detail code `witness_consumer_mismatch`。三条用例：发给另一个 task 的 witness、`kind` 不是 TASK 的 witness（正例两条）、发给本 task 的 witness 放行（反例）；另加一条**定向变异**（第 15 节）把 `consumer_task` 换成"witness 自称的那个消费者"，断言外来 witness 随即被放行。

## C. 必改（中）

**3　freshness=STALE/REVOKED 归 VALIDITY_RECHECK_PENDING，且不再自写 epoch/deadline 逻辑** — 已修。
门内改为直接调用契约的 `ValidityWitness.is_fresh_for(now_ms=…, current_scope_epoch=…)`，它一次性覆盖 epoch、not_after 与 freshness 三件事；失败后只用 `_freshness_code()` 判断"是哪一半说的不"来选 detail code（`witness_epoch_stale` / `witness_deadline_passed` / `witness_freshness_STALE|REVOKED`），**判定权在契约、报告权在本模块**。顺序：purpose → consumer → epoch 未知 → `is_fresh_for` → 可读性 → truth → decision，即新鲜度屏障仍排在 truth/decision 之前（§11.5"使用缓存前必须核对"）。新增 STALE / REVOKED 两条参数化用例。

**4　read-set 增补四条控制通道** — 已修。
`build_read_set()` 现在在 TASK 通道里额外记录 `<task>#dispatch_generation`、`<task>#input_binding_revision`、`<task>#approval`、`<task>#obligation_lifecycle`（公开常量 `CONTROL_READ_CHANNELS`），取值一律是**门实际用到的有效值**（计划里有就用计划的，没有就用绑定的）。approval 通道的 `semantic_revision` 是新增的 `TaskView.approval_revision`，content hash 覆盖 `ApprovalState.to_json()`；obligation 通道的 hash 覆盖 `(obligation_id, lifecycle, has_admitted_demand)`。
新增四条 stale 用例，且都是**两次真实判定的 read-set 对比**（不是手工改字段）：生成号推进、输入绑定版本推进、审批变更、义务 lifecycle 变更 / demand 撤回。

**5　support set 只动 revision（member_digest 不变）** — 已补用例。
生产代码本来就比较 `(revision, member_digest)` 二元组，逃逸的是测试覆盖：新增 `test_a_changed_support_set_revision_alone_invalidates_a_cached_readiness`。

## D. 建议（三条一并做）

**6　缺 obligation 账户 → 不放行**。`_selection_gate` 在 lifecycle / demand 之前先查账户是否存在，缺失时 `NOT_SELECTED` + `obligation_account_missing`，docstring 写明"无账户 = 无获准 demand，找不到账本条目不是许可"。新增一条用例。

**7　门序由声明驱动**。新增 `_GATE_SEQUENCE`（门函数名 → 它能产出的理由，按门内优先级），`gate_precedence()` 把它摊平再接上 `READY_CANDIDATE`，`READINESS_PRECEDENCE` **由它算出来**而不是另写一份。`evaluate_readiness` 按 `_GATE_SEQUENCE` 顺序从模块 globals 取门函数调用（所以变异自证仍然生效）。新增 `test_the_documented_precedence_is_the_order_the_gates_actually_run_in`。

**8　`admit_for_dispatch` 校验同源**。新增 `_same_origin(report, plan)`：mission、plan_revision、requirements_revision、manager_epoch、budget_grant_revision，以及 read-set 里每条 `ScopeEpochRead` 对应的当前 epoch，任一不符抛 `NotEligible` 并列出全部不符项。为此 `ReadinessReport` 增列 `mission_id` 与 `plan_revision` 两个字段（比把它们塞进 read-set 的 TASK 通道诚实）。新增三条用例（plan_revision / requirements_revision / scope epoch 各一条）+ 一条定向变异（把 `_same_origin` 换成 no-op，断言跨 plan revision 的报告随即被放行）。

## E. 新增偏差与新的契约变更请求

**偏差 8（新）：契约版 `CandidatePolicy` 未被采用，本地枚举改名为 `DispatchCandidacy`。**
协调者要求"把本地 CandidatePolicy 改为 import 契约版并删本地副本"，但两者**同名不同义**：
- `contracts.resolution.CandidatePolicy` 是 §10.2 的带版本记录（`policy_version` / `max_candidates` / `synthesis_allowed` / `reserve_tokens`），回答"一个目标可以带几个候选、综合要留多少预算"；
- 本地枚举回答实现稿 §6 必要条件的另一半"当前 Attempt 允许吗"（已有在跑的工作 / 重试用尽 / 已撤回）。
把后者改成前者会让 readiness 门读一个跟它的问题无关的记录。因此**改名避让**而不是合并：本地类型现在叫 `DispatchCandidacy`，detail code 从 `candidate_policy_*` 改为 `dispatch_candidacy_*`，docstring 明确写出两者"共享一个短语，不共享含义"。若协调者确认要在本片里同时接入候选数上限判据（需要额外输入：当前候选计数），请给一条明确指示，本片不擅自发明。

**CR-4（新）：`SemanticReadSet` 缺 obligation / authority 通道。**
它的四条带类型通道是 TASK / METHOD / FACT / ACCEPTANCE，而 `ReadItemKind` 本身是有 `OBLIGATION` 与 `AUTHORITY` 的。本片把四条控制通道挂在 TASK 通道下、用 `<task>#<channel>` 命名空间区分（每一条确实都是"关于这个 task 能否派发"的事实，所以不算张冠李戴，但不够直白）。请求：给 `SemanticReadSet` 增加 `obligation_revisions` 与 `authority_revisions` 两条通道，本模块随即改挂过去，`CONTROL_READ_CHANNELS` 这个常量也就可以撤掉。

CR-1 / CR-2 / CR-3 已由契约第四轮关闭（见 §A）。

## F. 修复后的门槛

| 项 | 命令 | 结果 |
|---|---|---|
| 本切片测试 | `pytest tests/orchestrator/full_target/test_readiness_reasons.py -q` | **192 passed**（修复前 164；新增 28 条） |
| lint | `ruff check <两个文件>` | All checks passed |
| format | `ruff format --check <两个文件>` | already formatted |
| 类型 | `mypy src/agent_orchestrator` | `eligibility.py` **0 error**（修复过程中出现过一条 `__reduce_ex__` 的 LSP 违反，已把形参改为 `SupportsIndex`）；仓库剩 17 条既存错误，全在 `evaluation/`、`runtime/deepseek_tokens.py`、`planning/htn/validation.py` |
| full_target 全目录 | `pytest tests/orchestrator/full_target -q` | **见下方说明：本片零失败，目录内有并行切片的在途失败** |

### full_target 全目录说明（重要）

跑全目录时有失败，但**全部来自正在被其他并行切片写入的文件**，与本片无关，证据三条：

1. 把本片两个文件（`graph/eligibility.py` 与 `test_readiness_reasons.py`）临时移出后重跑全目录，仍然失败：`test_htn_store.py::test_the_same_manifest_may_not_be_claimed_by_another_task`、`test_obligation_inheritance.py::test_a_legacy_replacement_leaves_all_thirty_new_tables_empty`（1057 passed / 2 failed）。
2. 失败集合在连续三次运行之间不断变化（3 → 10 → 11 条，且换了文件：先是 `test_htn_store` / `test_obligation_inheritance`，后是 `test_compound_gate_no_pseudo_cycle`），`ls -lt` 显示 `storage/htn_store.py`、`planning/htn/compiler.py`、`test_obligation_inheritance.py` 等文件的 mtime 就在这几分钟内持续推进——工作树正在被并行代理改动。
3. `grep -l eligibility tests/orchestrator/full_target/*.py` 只命中本片自己的测试文件：没有任何其他测试 import 本片模块；把 `test_htn_novel_method_admission.py` 与本片测试单独放在一起跑是 256 全绿。

本片自己的 192 条在单跑与合跑下都稳定全绿。全目录的净结论要等并行切片落定后由协调者重跑。

### 修复后的测试分节

| 节 | 条数 | 修复轮新增 |
|---|---:|---|
| 1 GRAPH_INTEGRITY | 5 | — |
| 2 NEEDS_REFINEMENT | 11 | — |
| 3 NOT_SELECTED | 11 | +2（无账户、demand 未获准） |
| 4 WAITING_ORDER | 20 | — |
| 5 WAITING_DATA | 8 | — |
| 6 证据（含 consumer 归属与 epoch 屏障） | 28 | +5（consumer 正反三条、freshness STALE/REVOKED 两条） |
| 7 WAITING_APPROVAL（契约 ApprovalState） | 7 | +3（DENIED/EXPIRED、过期授权、无过期授权） |
| 8 STALE_BINDING | 5 | — |
| 9 WAITING_OPERATION_UNKNOWN | 10 | +1（控制记录与信封不同源被契约拒绝） |
| 10 READY_CANDIDATE 与优先级 | 8 | +2（`gate_precedence()` 一致、报告带 mission/plan_revision） |
| 11 EligiblePrimitiveTask | 20 | +6（token 非构造参数、replace、copy/deepcopy、pickle、同源三条） |
| 12 stale_after | 19 | +6（四条控制通道 + support set 只动 revision + demand 撤回） |
| 13 三层 frontier | 10 | — |
| 14 纯度与 legacy 辅助 | 19 | +1（TaskView 拒绝非契约 approval） |
| 15 变异自证 | 11 | +2（witness consumer 定向变异、`_same_origin` 定向变异） |
| **合计** | **192** | **+28** |

---

# 读集通道改挂（契约第五轮：`obligation_revisions` / `authority_revisions`）

契约第五轮给 `SemanticReadSet` 加了两条带类型通道（`ReadItemKind.OBLIGATION` / `AUTHORITY`），**CR-4 关闭**。本节把上一轮临时挂在 TASK 通道下的四条控制读取改到它们各自该去的地方。只改了 `graph/eligibility.py` 与本片测试，未提交。

## 1. 改挂结果

| 控制读取 | 上一轮 | 现在 | 说明 |
|---|---|---|---|
| 义务 lifecycle + `has_admitted_demand` | TASK 通道 `<task>#obligation_lifecycle` | **`obligation_revisions`**（kind=OBLIGATION，id=`obligation_id`） | `ObligationAccountView` 没有版本计数器，所以 `semantic_revision` 留 0，靠 content hash 携带 `(lifecycle, has_admitted_demand)` 两项；**没有账户记录**时 hash 的是一份独立的 "absent" payload，与活账户不会撞成同一个值（有一条用例钉住） |
| 审批状态 + 版本 | TASK 通道 `<task>#approval` | **`authority_revisions`**（kind=AUTHORITY，id=`task_id`） | `semantic_revision` 是 `TaskView.approval_revision`，content hash 覆盖 `ApprovalState.to_json()`（decision + 授权人 + 过期时间），所以"只是过期了"与"被撤销"都算变化 |
| `dispatch_generation` | TASK 通道 `<task>#dispatch_generation` | **仍在 TASK 通道，仍用命名空间 id** | 见下方说明 |
| `input_binding_revision` | TASK 通道 `<task>#input_binding_revision` | **仍在 TASK 通道，仍用命名空间 id** | 同上 |

函数拆为三个：`_task_control_reads()`、`_obligation_reads()`、`_authority_reads()`（原 `_control_reads()` 删除）。`CONTROL_READ_CHANNELS` 常量已按要求删除，`__all__` 同步去掉。

### 为什么 TASK 通道的两条仍保留命名空间 id

协调者的条件是"若 TASK 通道条目结构不支持子字段，保留命名空间但在 journal 写明"——确实不支持。`ReadItem` 是 `(kind, id, semantic_revision, content_hash)` 四元组，**一个条目只能承载一个版本号**，而这个 task 有三个互相独立的版本要记：合同修订号、局部 dispatch generation、input binding revision。塞进一个条目就必然有两个被另一个覆盖，`stale_after` 也就再也分不出是哪一轴动了。因此保持三个条目、用 `<task>`、`<task>#dispatch_generation`、`<task>#input_binding_revision` 三个 id 区分；三条都是货真价实的 TASK 读取，**只有 id 的拼法是权宜**。若将来要彻底消除它，需要的是 `ReadItem` 支持一个 `facet` / `field` 判别字段（或给 TASK 通道一个专门的控制子通道），这属于契约改动，本片不擅自发明——如需推进请作为 CR-5 下达。

## 2. `stale_after` 与用例

`stale_after` 的逐项比对现在覆盖六条 `ReadItem` 通道（goal / method / observation / acceptance / **obligation** / **authority**）加三个标量与三组集合读取。

用例调整（净 +8，192 → **200**）：
- 删除原 `test_the_read_set_records_every_control_channel_the_gates_read`（它依赖已删除的常量），换成五条更具体的：TASK 通道三个 id 并存、义务落在 OBLIGATION 通道且 kind 正确、审批落在 AUTHORITY 通道且 kind 正确、AUTHORITY 通道携带审批记录版本号、无账户与活账户的 hash 不同。
- `stale_after` 新增四条 lane-local 用例：AUTHORITY 通道内容变化、AUTHORITY 通道条目消失、OBLIGATION 通道内容变化、OBLIGATION 通道条目消失。
- 原先那四条"两次真实判定对比"的用例（生成号推进、输入绑定版本推进、审批变更、义务 lifecycle 变更 / demand 撤回）保持不变，现在自动走新通道。

**改挂后的载重自证**：把 `stale_after` 里新加的两行通道比对临时删掉重跑，**7 条失败**（`test_a_changed_approval_…`、两条 authority lane、两条 obligation lane、`test_a_changed_obligation_lifecycle_…`、`test_a_withdrawn_demand_…`），恢复后 200 全绿——新通道确实被检查，不是写进去就算数。

## 3. 门槛

| 项 | 命令 | 结果 |
|---|---|---|
| 本切片测试 | `pytest tests/orchestrator/full_target/test_readiness_reasons.py -q` | **200 passed**（审阅修复轮 192，本轮 +8） |
| lint | `ruff check <两个文件>` | All checks passed |
| format | `ruff format --check <两个文件>` | already formatted |
| 类型 | `mypy src/agent_orchestrator` | `eligibility.py` **0 error**；仓库余 17 条既存错误，全在 `evaluation/`、`runtime/deepseek_tokens.py`、`planning/htn/validation.py` |

分节条数：1→5、2→11、3→11、4→20、5→8、6→28、7→7、8→5、9→10、10→12、11→20、12→23、13→10、14→19、15→11。

未提交。本轮没有新的偏差；未决事项只剩上一节的 **偏差 8**（契约版 `CandidatePolicy` 与本地 `DispatchCandidacy` 同名不同义，已改名避让，等协调者裁决是否另行接入候选数上限判据）。
