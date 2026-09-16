# P1.1 实施记录（HTN 契约类型与四值谓词求值器，纯内存）

日期：2026-09-16｜基线：SDK main `873fd4a`｜执行：单个 Opus 子代理｜实际耗时：约 1 小时 15 分钟（含规范阅读约 25 分钟）

规范来源：`simple_harness/plans/taskSys2/升级planV1/v1.4/simpleharness-full-target-1.4/complete-plan.zh-CN.md`
§4 ADR-01～13、§6.1–6.6、§7.3、§11.5、§13、§14.3、§18.3、§23、§24.1、§25.1；
附件 TG §3.1–3.3/§4.3/§9；附件 AER §3/§4.1/§5.2/§6.1/§8.1–8.3/§12.2 与其 4 份 schema、8 个 fixture；
主计划 4 份 schema 与 `schema-fixtures/fixtures.json` 的 13 个样本。

## 1. 实现清单

### 生产代码（7 个新文件 + 1 个改动文件）

| 文件 | 行数 | 主要类型 |
|---|---:|---|
| `src/agent_orchestrator/contracts/semantic_base.py` | 429 | 边界校验器（`identifier/text/hash_hex/index/flag/json_value/json_object/sequence_of/identifiers/enum_of/fields_of/schema_version`）、`reject_executable`、`EvidenceRefKind`(7 种)/`TypedRefKind`(12 种)、`VersionedRef`、`EvidenceRef`、`TypedRef`、`content_hash_of` |
| `contracts/evidence_state.py` | 802 | `TruthValue`、`SupportCount(t,f)`、`Validity`、`Availability`、`PreconditionPhase`+`phase_check_points`、`TemporalUse`、`QueryCompleteness`、`ObservationRecord`、`EvidenceEntry`、`EvidenceSnapshot`、`WitnessPurpose`、`WitnessDecision`、`RecheckOutcome`、`ValidityWitness`、`PreconditionWitnessRecord`、`merge_observations` |
| `contracts/htn.py` | 2858 | 12 个身份 NewType + 6 个版本轴 NewType（§6.4 五类 + `input_binding_revision`）与其构造函数；`TaskForm`、`RelationKind`、`ReleaseCondition`、`Requiredness`、`ReusePolicy`、`ObligationRelation`、`SourceRevisionPolicy`、`PortCardinality`、`ApplicabilityCheckPolicy`、`MethodRegistryStatus`、`RegistryAuthor`、`RunningWorkPolicy`、`ReadItemKind`；值/条件 AST（`ParameterValue/ConstantValue/OutputValue/ObjectValue/ArrayValue`、`PredicateCondition/AllCondition/AnyCondition/NotCondition/ConstantCondition`、`parse_condition`、`StructureBudget`、`condition_digest`）；`MethodRef`、`MethodStep`、`MethodOrdering`、`CriterionLink`、`MethodComposition`、`MethodContract`、`MethodRegistration`+`admit_method`；`GoalSignature`、`PortSpec`、`OrderConstraint`、`DataRequirement`、`BoundInput`、`PreconditionRef`；`Binding`、`ChildBinding`、`MethodInstanceDraft`、`MethodOccurrenceBinding`、`TaskSemanticBindingV1`；`ReadItem`、`SupportSetRead`、`ScopeEpochRead`、`AbsenceRead`、`SemanticReadSet`；`PlanProposal`（4 种 plan_operation）与 `ProposedPlanDelta`（`OccurrenceSpec`、`ObligationCoverage`）、`require_commit_ready`；`ExecutionFeedbackV1`（见 §3 偏差 1） |
| `contracts/obligations.py` | 627 | `ObligationLifecycle`、`FuelStatus`、`ShapeChange`、`AchieveOutcomeAdmission`、`Selector`、`SatisfactionPolicy`、`Obligation`、`ExpansionRecord`、`ObligationAccountView`、`FuelDecision`、`ObligationLedger`、`BoundReachedReport`、`achieve_outcome_admission`、`funding_owner_conflicts` |
| `contracts/resolution.py` | 1899 | `CriterionOrigin`、`RequirementClass`、`EvaluationKind`、`RequiredEvidencePolicy`、`AmendmentPolicy`、`Criterion`；成功表达式受限 AST（`CriterionExpr/AllExpr/AnyExpr`、`parse_success_expression`、`hard_constraints_not_independent`、`unknown_expression_criteria`）、`RequirementsRevision`；`ReviewPurpose`(6)+`ReviewAccount`+`REVIEW_PURPOSE_ACCOUNTS`+`account_for_purpose`、`CriterionVerdict`、`CheckExecution`、`ReviewVerdict`、`ReviewBinding`、`CriterionOutcome`、`ReviewPackage`、`ReviewRecord`、`match_review_criteria`；`Acceptance`、`ResolutionCriterion`、`GoalResolution`、`DeliveryStage`/`DeliveryReceipt`；`OperationId`/`OperationOccurrenceId`、`OperationKind`、`OperationEnvelope`、`envelope_conflict`（`OPERATION_PAYLOAD_CONFLICT`）、`ReconciliationOutcome`、`ReconciliationResult`、`may_rehandoff` |
| `knowledge/__init__.py`、`knowledge/predicates.py` | 292 | `WorldAssumption`、`ArgumentType`、`PredicateParameter`、`PredicateSignature`、`ArgumentCheck`、`PredicateRegistry`、`proposition_key`、`closed_world_denial_admissible`、`atom_truth` |
| `planning/htn/applicability.py` | 665 | `SupportProvenance`、`not_truth/all_truth/any_truth`、`ConditionEvaluation`、`ground_value`、`evaluate_condition`、`GateDecision`+`authorization_gate`、`CapabilityRecord`/`CapabilitySnapshot`、`ApplicabilityStatus`、`ApplicabilityReport`、`assess_method`、`RecheckStatus`/`RecheckResult`/`recheck_method_instance` |
| `contracts/__init__.py`（改） | +40 | 以子模块方式再导出 `evidence_state/htn/obligations/resolution/semantic_base`，并按名导出 20 个高频类型 |

`planning/htn/__init__.py` 已由 P2.2 子代理创建，本片未改动；`backends/` 未触碰。

### 红测试（6 个新文件，133 条）

| 文件 | 条数 | 覆盖 |
|---|---:|---|
| `full_target_world.py` | —（夹具） | World（注册表+快照）、方法/任务/目标签名构造器、计划包 fixture 读取 |
| `test_predicate_truth_table.py` | 57 | NOT 四值全表、ALL/ANY 4×4 全表、两条反例、`ALL(p, NOT p)`、空 ALL/ANY、开/闭世界、(t,f) 不被冲淡、STALE 降级、授权门（纯观察放行／混合与空表达式不放行／非 TRUE 不放行）、AST 拒绝 eval/字符串/SQL/callable/超节点数、`assess_method` 六种状态、快照见证复检 |
| `test_htn_recursion_fuel.py` | 12 | 燃料按 obligation 计、改参数/换方法/换 Agent/改名不重置、耗尽→`BOUND_REACHED`、`BoundReachedReport`、重复展开不烧燃料、`achieve_outcome` 四种裁决、重复注册被拒 |
| `test_obligation_conservation.py` | 11 | 失败计数与已消费额度在改名/替代/换角色/换方法后累计、后继任务共用额度、新责任需新 id、展开历史保留、资金唯一归属、往返与未知字段拒绝 |
| `test_semantic_binding_codec.py` | 22 | `TaskSemanticBindingV1`/`MethodContract`/`MethodInstanceDraft`/`SemanticReadSet`/`GoalSignature` 往返与 hash 稳定、form↔operator_ref 一致、goal_id 必须是 typed TaskRef（裸串与错 kind 均拒）、`PlanProposal`↔`ProposedPlanDelta` 不可互换、单值端口唯一 binding、未知 occurrence 边被拒、registry_status 不可自填、TRIAL_ADMITTED 需 mission 作用域、前提阶段默认、13 个计划 fixture |
| `test_aer_contract_codecs.py` | 33 | 8 个 AER fixture（正/负）、负样本均因 `model_says_approved` 被拒、OperationEnvelope 同 id 异 payload 冲突/同 payload 非冲突、ReconciliationResult（空查不可重发、NOT_APPLIED_FINAL 需权威查询）、六用途账户归属、ReviewRecord 逐准则匹配（未知/重复/遗漏）、I07（未跑的必需检查不是 PASS）、成功表达式结构校验与硬约束独立 AND、ValidityWitness（USABLE 需 TRUE、epoch/not_after 失效）、DeliveryReceipt 阶段约束 |

红→绿：先写 `test_predicate_truth_table.py` 的两条反例与 NOT 表，确认对着空实现为红；随后逐模块补齐。过程中出现过 3 次真实红（`World` 未声明的谓词、`ValidityWitness.required` 被批量替换误删 `reason_codes`、空 `children` 的错误文案），均修复后转绿。

## 2. 与规范的关键对应

- **§6.6 三条钉死规则**：`not_truth` 保留 UNKNOWN/CONFLICT；`all_truth`/`any_truth` 用优先级表而非 (t,f) 的 and/or，两条反例（`ANY(UNKNOWN, CONFLICT)=CONFLICT`、`ALL(UNKNOWN, CONFLICT)=CONFLICT`）单独立测；快照见证与复检不一致返回 `METHOD_INSTANCE_INVALIDATED`（返回码，不是 CONFLICT，也不触发整体重规划）。
- **§6.6 C28 / AER §9.2**：不做 negation-as-failure。`EvidenceEntry` 在 `f>0` 而无观察记录、也无权威负观察时直接拒绝构造；`closed_world_denial_admissible` 要求封闭域 + `authoritative_negative` + `f>0`；`ObservationRecord.is_authoritative_negative` 额外要求查询水位与覆盖范围同时存在。
- **§6.6 规则 2（授权）**：`SupportProvenance` 六值，`AUTHORIZING_PROVENANCE` 只含 `OBSERVED` 与 `CLOSED_WORLD_AUTHORITATIVE`。任何 `MISSING/STALE/CONSTANT/UNRESOLVED` 参与的 TRUE 都不放行，因此空 ALL 的 TRUE 与"闭域缺失 + 开域未知"的混合结果一律被拦。
- **§6.4 / TG 裁决 7**：`MethodInstanceDraft.goal_id` 是 `TaskRef`，线上形态是 `{"kind":"task","id":...}` 判别联合；`task_ref_from_typed` 明确拒绝裸字符串并在错误信息里写明"前缀不构成 kind"，`obligation_id` 是独立字段。
- **§6.4 燃料**：`ObligationLedger` 全部计数以 `obligation_id` 为键；`note_shape_change` 是"工作换形状"的唯一入口，只记历史不动计数；耗尽返回 `FuelStatus.BOUND_REACHED`；`achieve_outcome_admission` 在燃料耗尽时返回 `FUEL_EXHAUSTED_NO_ESCAPE`，在非显式选择时返回 `NOT_EXPLICITLY_SELECTED`。
- **ADR-13 / C29**：`SemanticReadSet` 含 `requirements_revision`、goal/method/observation/acceptance 修订、`manager_epoch`、`budget_grant_revision`、`support_sets`（带 `member_digest`）、`scope_epochs`、`absences`（TG §11.2"不存在也是读取事实"）。`ProposedPlanDelta` 必须携带它。
- **§7.3 / §6.3**：`MethodContract` 本身不含 `registry_status`（与 schema `additionalProperties:false` 一致）；准入由 `MethodRegistration`/`admit_method` 写入，`RegistryAuthor.MODEL` 只能提交 DRAFT，`TRIAL_ADMITTED` 必须带 mission 作用域。
- **§13 v1.4**：`REVIEW_PURPOSE_ACCOUNTS` 是全量映射，六个用途都有账户，测试逐一覆盖，避免预算漏计。
- **§25.1 裁决 2**：成功表达式只有 `criterion/all/any`，children 非空；`hard_constraints_not_independent` 结构级拒绝"硬约束落在 OR 分支里"或"硬约束根本不在表达式里"；本片不求值（求值在 P1.1b）。
- **§14.3 v1.4**：`OperationEnvelope` 逐字段对齐 schema，`envelope_conflict` 同 id 异内容返回 `OPERATION_PAYLOAD_CONFLICT`；`ReconciliationResult` 拒绝"非权威查询 + NOT_APPLIED_FINAL"，`may_rehandoff` 只在服务端可证或旧请求确证不可用时为真。
- **I07**：`CriterionOutcome` 在 `verdict=PASS` 且 `check_execution ∈ {NOT_RUN, ERROR, CANCELLED}` 时拒绝构造。**I18**：`ValidityWitness.decision=USABLE` 要求 `truth=TRUE`。

## 3. 与规范/任务书的偏差及理由

1. **新增 `ExecutionFeedbackV1`（在 `contracts/htn.py`）**——任务书交付物清单里没有它，但完成门槛要求"主计划 13 个 fixture 全部按 `expect_structure_valid` 通过/拒绝"，其中 3 个是 `execution-feedback-v1`。仓库没有 jsonschema 依赖，结构级判定只能由 codec 给出，因此补了这一份边界 codec（只做结构，不做 §9.1 的分类与修复决策）。P3.1 若要扩展 ExecutionFeedback 语义，应在此类型上增量，不要另起一份。
2. **新增 `contracts/semantic_base.py`**——任务书列的是 6 个交付文件；这一份是第 7 个，放共用的边界校验器与两种 typed ref。不建它的话，四个契约模块要么互相导入私有 `_text` 之类的名字，要么在 evidence_state 与 htn 之间形成循环（htn 需要 `TruthValue`/`PreconditionPhase`，evidence_state 需要校验器）。任务书允许新建文件，故按此分层：`semantic_base → evidence_state → htn → {obligations, resolution}`。
3. **`GoalSignature` 定义在 `htn.py`，由 `resolution.py` 再导出**——任务书把它列在 resolution.py。但 `TaskSemanticBindingV1`（htn.py）需要它，而 resolution.py 需要 htn.py 的 `TaskRef/ObligationId/MethodInstanceId`，定义在 resolution 会造成循环。语义上它也是规划侧概念。`from agent_orchestrator.contracts.resolution import GoalSignature` 依旧可用。
4. **`ValidityWitness.purpose` 采用 7 值**——§11.5 正文写 PLAN/START/MAINTAIN/ACCEPT/DISCLOSE（5 值），`annex/aer-1.0/schemas/validity-witness.schema.json` 的 enum 是 7 值（多 CONTEXT、RECOVERY）。任务书要求"按 schema"，且 schema 是线上合同，故取 7 值超集；fixture 正样本用的是 START。
5. **`EvidenceRefKind`(7) 与 `TypedRefKind`(12) 分成两个枚举**——主计划 4 份 schema 的 `evidence_ref.kind` 是 7 值，AER 4 份 schema 的 `ref.kind` 是 12 值。合成一个超集枚举会让主计划 schema 接受 `task`/`acceptance` 等本不允许的值，因此按各自 schema 分开，共用同一 `{id, revision, content_hash}` 形状。
6. **`ProposedPlanDelta.referenced_occurrences`**——设计稿没有这个字段。ORDER/DATA 端点可能指向本次增量之外的既有节点，若只允许指向本增量新建的 occurrence，编译器就无法表达"新节点排在既有节点之后"。因此增加一个显式的"本增量引用到的既有 occurrence"列表，端点必须落在"新建 ∪ 引用"里，既保住结构校验又不过度收紧。
7. **前提阶段的默认语义**——§6.6 写"未声明阶段的前提默认 SELECT，并在 ACCEPT 时再检查一次"。实现为 `phase_check_points(None) == (SELECT, ACCEPT)`，而显式声明 `SELECT` 只在 SELECT 检查。这样"显式声明 SELECT"不会额外获得 ACCEPT 复检，"不声明"也不会少一次复检；两者语义不同，测试钉住。
8. **`assess_method` 的判定优先级**——规范未固定顺序。实现取 `TYPE_ERROR → CAPABILITY_UNAVAILABLE → FALSE → CONFLICT → UNKNOWN → APPLICABLE`：结构性问题先于部署事实，部署事实先于证据结论（前两者不是靠取证能改变的）。已在模块 docstring 与测试中写明。
9. **计划包 fixture 从原地读取**——`tests/orchestrator/full_target/full_target_world.py` 通过相对路径（可用 `SIMPLEHARNESS_PLAN_PACK` 覆盖）定位 `simple_harness/plans/.../simpleharness-full-target-1.4`，不把 fixture 复制进 SDK 仓库，规范改动会直接表现为红测试。计划包不在时这两条测试 `skipif` 跳过（本机在，实际执行）。

## 4. 未做的事项（留给后续小片）

- 成功表达式的**求值**、硬门与必需检查完整性 → P1.1b `verification/acceptance_rules.py`（本片只做结构校验，`parse_success_expression` 与 `hard_constraints_not_independent` 可直接复用）。
- 带极性理由的最小不动点、无锚 SCC、`EVALUATION_INCOMPLETE` → P1.1c `knowledge/justifications.py`。
- `RecheckOutcome` 五分类的实际重算、epoch 屏障、dirty 队列、`VALIDITY_RECHECK_PENDING` → P3.5/P3.6（本片只给出枚举与 `ValidityWitness.is_fresh_for` 的纯判断）。
- 落库（`storage/obligation_store.py`、`htn_store.py`）与迁移 → P1.2；替代任务沿 Obligation 继承计数的接线 → P1.3。
- grounding / compiler / registry / validation 与种子方法库 → P2.1（本片只给类型与 `assess_method`）。
- `ReviewPackage` 的冻结、取证与独立性执行协议 → P6.0/P6.1（本片只给不可变数据类型与逐准则匹配的纯函数）。
- `CarryForwardReceipt`、`Commitment/ExecutionCycle`、`CriterionCoverageProposal` 未建模，本片范围外。

## 5. 验证结果

```
# P1.1 五个红测试文件
uv run --frozen --group dev --extra local-capacity pytest \
  tests/orchestrator/full_target/test_predicate_truth_table.py \
  tests/orchestrator/full_target/test_htn_recursion_fuel.py \
  tests/orchestrator/full_target/test_obligation_conservation.py \
  tests/orchestrator/full_target/test_semantic_binding_codec.py \
  tests/orchestrator/full_target/test_aer_contract_codecs.py -q
→ 133 passed

# lint / format / 类型
uv run --frozen --group dev ruff check src/agent_orchestrator/contracts src/agent_orchestrator/knowledge \
  src/agent_orchestrator/planning/htn/applicability.py tests/orchestrator/full_target
→ All checks passed
uv run --frozen --group dev ruff format --check <同上>
→ 39 files already formatted
uv run --frozen --group dev mypy src/agent_orchestrator
→ P1.1 的 7 个新文件与 contracts/__init__.py 零错误
  （其余错误来自既有 evaluation/ runtime/ 的可选依赖缺失，以及 P2.2 子代理正在写的 backends/panda.py）

# 隔离门槛
grep -E "from \.\.(storage|orchestrator|scheduling|artifacts)|import (storage|orchestrator|scheduling|artifacts)" <7 个新文件>
→ 零命中
git status --short
→ 仅 M contracts/__init__.py 与新增文件；未触碰 orchestrator/、scheduling/、artifacts/、storage/

# 既有回归
uv run --frozen --group dev --extra local-capacity pytest tests/orchestrator/step02 tests/orchestrator/step03 tests/orchestrator/p33 -q
→ 1020 passed, 5 skipped, 1 failed
  唯一失败是既知的 p33 `test_legacy_check_ast_and_default_retrieval_bytes_are_unchanged`
  （Python 3.14 的 AST 转储 hash 基线漂移，与本片无关）
```

## 6. 交接提示

- 新契约的入口在 `agent_orchestrator.contracts` 包：`evidence_state / htn / obligations / resolution / semantic_base` 五个子模块整体再导出，另有 20 个高频类型按名导出。
- 所有 codec 都是"严格对象解码"：缺必填字段、多未知字段、`"1"` 冒充 `1` 都是 `ContractError`。8 个 AER 负样本与 6 个主计划负样本正是靠这一条被拒的，新增字段时务必同步 `fields_of` 的 required/optional 清单（本片开发中因批量文本替换误删过一次 `reason_codes`，由 fixture 测试当场抓出）。
- 条件 AST 与成功表达式各有独立的节点预算（均 512），且都走 `reject_executable`；扩展 AST 时不要绕开 `parse_condition`/`parse_success_expression` 这两个唯一入口。

---

## 7. 审阅修复（2026-09-16，独立审阅结论"需修后合并"）

追加耗时约 35 分钟。工作树里同时有 P1.1b / P1.1c / P2.1b / P2.2 子代理的文件，全程未改动其内容；发现并发写入后，lint/format 命令已收窄到本片自己的文件。

### 必改项

**1（高）`ObligationLedger` 的部分写入 —— 已修**

`set_lifecycle` 原先先赋值 `account.lifecycle` 再校验 `resolution_ref`。后果有两层：一次被拒的 `SATISFIED` 调用会把义务留在 `SATISFIED`，此后 `achieve_outcome_admission` 返回 `OBLIGATION_NOT_ACTIVE`，义务被静默关闭；而且 `resolution_ref` 根本没有被保存，"已满足"没有任何凭据。

改法：先完整校验（`enum_of` + `optional_identifier` + SATISFIED 需 resolution_ref），再一次性写入 lifecycle 与 resolution_ref。`_Account` 与 `ObligationAccountView` 新增 `resolution_ref` 字段（初值取自 `Obligation.resolution_ref`），`to_json` 同步；这样"保存了"才是可观察的事实。

`record_spend` 同样是部分写入：`cost_micros` 先校验并累加，`attempts` 非法时抛错，已落账的费用留在账上，调用方重试就会重复计费。改为两个参数都校验完再累加。`record_failure` 与 `note_shape_change` 复核后本来就是"先构造值再写入"，无需改动，但一并补了测试钉住。

新增 6 条测试（`test_obligation_conservation.py`）：被拒的 `set_lifecycle`（缺 resolution_ref / 非法枚举值）、被拒的 `record_spend`、被拒的 `record_failure`、被拒的 `note_shape_change` 各一条"账本逐字段不变"，外加一条成功路径确认 `resolution_ref` 真的被保存。第一条同时断言被拒后 `achieve_outcome_admission` 仍为 `ADMISSIBLE`，直接钉住审阅指出的那条因果链。

**2（中）fixture 依赖仓库外目录 —— 已修**

原来 `full_target_world.py` 通过相对路径去隔壁 `simple_harness` 仓库读计划包。CI 只 checkout SDK，21 条 fixture 校验在 CI 里恒为空转 —— 这正是"接口存在就算完成"的一种（ADR-12）。

改法：把主计划 4 份 schema + `schema-fixtures/fixtures.json`（13 个样本）复制到 `tests/orchestrator/full_target/fixtures/plan_pack/`，AER 4 份 schema + 8 个 fixture + `index.json` 复制到 `fixtures/aer/`（沿用 P2.2 已有的 `fixtures/` 惯例）。`fixtures/SOURCE.md` 记录上游根目录、计划版本、复制日期与 18 个文件逐个的 SHA-256。

`load_plan_fixtures` / `load_aer_fixture_index` / `load_aer_fixture` 改读仓库内副本，两条 fixture 测试的 `skipif` 全部移除 —— 它们现在在任何环境都真的跑。原地读取降级为一条可选检查 `test_repo_copies_match_the_upstream_plan_pack`：先对 `SOURCE.md` 里的 18 个 SHA-256 逐一核对（这一半永远跑），上游计划包存在时再逐字节比对上游（本机执行，CI 跳过这一半）。规范改动因此会以"漂移"的形式变成红测试，而不是被一份陈旧副本悄悄吸收。

**3（中）schema 边界红测试 —— 已修**

审阅的 8 个变异里"放宽上限 ×2000"全绿逃逸，因为 fixture 的载荷离上限很远，往返测试怎么都过。所以上限必须被直接断言，而且要断言到 schema 而不是断言到另一份抄写的数字：

- `test_the_codec_caps_are_exactly_the_ones_the_schemas_declare`：扫描仓库内 4 份主计划 schema，断言其 `maxLength` 集合恰为 `{512, 20000}`、`minLength` 为 `{1}`、`minimum` 为 `{0, 1}`，再断言 `MAX_ID == 512`、`MAX_TEXT == 20_000`。
- AER 侧同理：`maxLength` 为 `{512, 10000}`、`maximum` 为 `{9007199254740991}`，断言 `MAX_REASON == 10_000`、`MAX_JSON_INT == 9_007_199_254_740_991`。
- 正好在界 / 越界成对：id 512 与 513；text 20000 与 20001；explanation 10000 与 10001；list 256 与 257；revision `0` 与 `-1`；`VersionedRef.version` `1` 与 `0`；revision 恰为安全整数上限与上限 +1。
- minLength 1：`""` / `" "` / `"\t\n"` 三种空白均被拒。
- minItems 1：`PlanProposal.read_set`、`PlanProposal.operations`、`ReviewRecord.criteria`、`FeedbackObservation.evidence_refs` 各一条。
- content_hash：63 位、65 位、大写、非十六进制、空串、带 `0x` 前缀共 6 种变体全部被拒。
- 整数严格性：`"1"` / `1.0` / `True` / `None` 冒充整数 revision 全部被拒。
- `test_the_list_cap_is_ours_and_is_stricter_than_the_schemas`：断言主计划 schema **没有** `maxItems`，并记下 `MAX_LIST == 256` 是 ADR-08 的部署侧硬上限、比 schema 严 —— 写成测试是为了避免后人"为了对齐 schema"把这个上限删掉。

### 建议项（均已一并做）

**4 `ValidityWitness` 对齐 annex reference** —— `decision=USABLE` 现在同时要求 `truth=TRUE`、`freshness=CURRENT`、`availability=READABLE`，对应 `annex/aer-1.0/reference/protocol_rules.py` 的 `usable_for_execution`（`truth is TRUE and current and readable ...`）。`is_fresh_for` 的截止时间改为排他：`now_ms >= not_after_ms` 即过期，对应 reference 的 `now_ms < not_after_ms`。新增 4 条测试（REVOKED / UNAVAILABLE 被拒、BLOCKED 可记录任意组合、`now_ms == not_after_ms` 不 fresh）。AER 正样本 fixture 仍然通过（它是 TRUE + CURRENT + READABLE + START）。

**5 `predicates.py` 死代码** —— `atom_truth` 里"FALSE 且无观察记录则退回 UNKNOWN"的分支因 `EvidenceEntry.__post_init__` 的不变量（`f > 0` 必须有 counter-observation 或权威负观察）已不可达。删除该分支，代之以说明该不变量在哪里、以及 `closed_world_denial_admissible` 回答的是另一个问题（封闭域是否**有权**否定）的注释。`atom_truth` 现在就是 `entry.truth(now_ms=now_ms)`。

**6 记录两条移交事项** —— 见下 §8。

### 补全的偏差（原 §3 漏记）

10. **`assess_method` 的签名相对 §18.3 有改动**：
    - 第一参数 §18.3 写 `TaskSpec`，实现用 `TaskSemanticBindingV1`。本片没有 `TaskSpec` 这个类型，而 §18.5 / TG §3.2 规定的正是"现有 Task 的工作事实 + 版本化语义绑定"，适用性判断需要的 `typed_parameters`、`capability_requirements`、`form` 全在语义绑定上。P2.1 若引入 `TaskSpec` 作为更窄的只读视图，应让它由 `TaskSemanticBindingV1` 投影而来，而不是另建一份平行字段。
    - 增加了两个 keyword-only 参数：`registry: PredicateRegistry`（必需）与 `now_ms: int | None = None`。§18.3 的四参数签名没有地方传谓词注册表，但"解释器只接受注册表提供的谓词"（§6.6）要求它必须显式传入 —— 做成模块级单例会让测试之间互相污染，也会让"哪些谓词可用"变成隐式全局状态。`now_ms` 用于 `EvidenceEntry.not_after_ms` 的到期判断（§11.5 要求消费者核对 `not_after`），默认 `None` 表示不做时间判断。两者都是 keyword-only，位置参数仍与 §18.3 一致。

11. **`ObligationAccountView` 增加 `resolution_ref`**：见上 §7 第 1 条，为了让"保存了 resolution_ref"成为可断言的事实。

## 8. 移交给后续小片的两条注意

- **`semantic_base.py` 与 `models.py` 有重复**：`MAX_TEXT`、`MAX_LIST` 两个常量重名同值，`_text`/`_texts`/`_object`/`_enum` 与 `text`/`identifiers`/`json_object`/`enum_of` 职责重叠（旧的一组是 §26 六个契约在用，新的一组是 FULL-TARGET 契约在用，校验细节不完全相同：新的一组拒绝控制字符、区分 identifier 与 text、`index` 明确拒绝 `bool`）。本片没有动 `models.py`（超出允许改动范围，且旧事件字节不变是 P1 硬约束）。**建议在 P1.2 落库时一并收敛**：先让 `models.py` 的助手改为转调 `semantic_base`，用既有回归确认旧路径逐字节不变，再删重复常量。
- **`assess_method` 返回 `APPLICABLE` 不等于可以派发**：状态位只回答"这个方法此刻适用吗"，授权是另一轴。一个由空 `applicable_when`（空 ALL = TRUE）或"闭域缺失 + 开域未知"混合得到的 TRUE 会拿到 `APPLICABLE`，但 `report.authorization.allowed` 是 `False`。**P2.x 的派发路径必须同时读 `report.authorization`**，只看 `status is APPLICABLE` 就交接，等于绕过 §6.6 规则 2 与不变量 I18。`ApplicabilityReport.authorization` 因此是必填（非 None）的，别把它当可选诊断信息。

## 9. 审阅修复后的验证

```
uv run --frozen --group dev --extra local-capacity pytest \
  tests/orchestrator/full_target/test_predicate_truth_table.py \
  tests/orchestrator/full_target/test_htn_recursion_fuel.py \
  tests/orchestrator/full_target/test_obligation_conservation.py \
  tests/orchestrator/full_target/test_semantic_binding_codec.py \
  tests/orchestrator/full_target/test_aer_contract_codecs.py -q
→ 170 passed（修复前 133，新增 37 条）

uv run --frozen --group dev ruff check <本片 7 个源文件 + tests/orchestrator/full_target>   → All checks passed
uv run --frozen --group dev ruff format --check <同上>                                      → 41 files already formatted
uv run --frozen --group dev mypy src/agent_orchestrator                                    → 本片文件零错误

参考（非本片门槛）：tests/orchestrator/full_target 全目录 326 passed / 1 skipped，
含 P1.1b、P2.2 子代理的测试，确认本片改动没有影响并发小片。
未跑全量回归、未提交、未推送。
```

---

## 10. 契约第三轮（2026-09-16，汇总 P1.1b / P1.1c / P2.1b 的变更请求）

追加耗时约 50 分钟。原则：只做加法 —— 新增可选字段、新类型、公开助手；既有字段语义不改。

**hash 兼容的做法**：所有新增的可选字段在 `to_json` 里**为空/为 None 时整键省略**。因此没有用到新字段的对象，序列化字节与本轮之前逐字节相同，content hash 不变；21 个 fixture 的 `to_json() == payload` 断言也因此继续成立。这条规则在每处新增字段旁都有注释，并各配了一条"未使用新字段的对象不含该键"的测试。

### 逐项

| # | 请求方 | 落点 | 做法 |
|---|---|---|---|
| 1 | P1.1b | `resolution.py` | `ReviewPackage` 新增 `producer_agent_ids: tuple[str,...]` 与 `reviewer_workspace_access: WorkspaceAccess`（NONE/READ_ONLY/WRITE，默认 READ_ONLY）。`WRITE` 在 `__post_init__` **拒绝**（AER §5.3：能改就不能独立判）。新增 `ReviewPackage.produced_by(agent_id)` 供 `independence_ok` 直读。 |
| 2 | P1.1b | `resolution.py` | 新增可选 `requirements_content_hash`；`_criteria_only_under_any` → 公开 `criteria_only_under_any()`（模块函数 + `ReviewPackage` 同名方法），返回 `frozenset`；新增 `ReviewPackage.hard_constraint_violations()`。硬约束结构规则见下"偏差"。 |
| 3 | P1.1b | `semantic_base.py` | 新增 `Provenance`（SYSTEM/TOOL/MODEL/HUMAN）与 `MODEL_SUBMITTABLE_PROVENANCE = {MODEL, HUMAN}`；`TypedRef` / `EvidenceRef` 各加可选 `produced_by`，并加 `from_model_json`（拒绝 SYSTEM/TOOL）。新增递归扫描器 `reject_model_claimed_provenance`，已接进 `PlanProposal.from_json` 与 `ExecutionFeedbackV1.from_json` —— 这两个按 §18.3 定义就是模型侧合同。`MethodContract` 未接（种子方法可能由系统/人编写并合法带 TOOL 依据），由调用方显式调用助手。 |
| 4 | P1.1c | `evidence_state.py` / `predicates.py` | `ObservationRecord` 加可选 `observer_id`；新增 `authoritative_negative_matches_observer(signature, observation)`：封闭域否定必须由该谓词签名列出的观察者产生。**未改** `is_autho­ritative_negative` 的既有判定（纯加法），身份核对是注册表侧的独立一问。 |
| 5 | P2.1b | `htn.py` | 新增 `GraphStructureBudget`（见第 11 项已扩到 9 个维度），与 `StructureBudget`（条件 AST 的可变计数器）**同文件不同名**，docstring 明确两者区别。 |
| 6 | P2.1b | `htn.py` | 新增 `PortOrdering`（BY_PRODUCER_ORDINAL/BY_KEY/EXPLICIT）；`PortSpec` 加可选 `ordering` + `order_key`。规则：SINGLE 不得声明 ordering；BY_KEY 必须带 order_key，其余不得带；新增 `PortSpec.set_order_declared` 与模块函数 `undeclared_set_ports(ports)`。 |
| 7 | P2.1b | `htn.py` | 新增 `ResourceRef(namespace, object_id)` 与 `SideEffectKind`；`TaskSemanticBindingV1` 加 `resource_reads` / `resource_writes` / `side_effect_kind`，compound 声明三者之一即拒（§6.2）。新增 `resource_conflicts(...)`：写写、读写重叠为冲突，读读不是。 |
| 8 | P2.1b | `htn.py` | 上提 `EndpointKind` / `NetworkEndpoint` / `TypedEdge`，带严格 codec 与 `TYPED_EDGE_ENDPOINTS` 逐关系端点判别式（SUPPORT/ASSUMPTION 源必为 EVIDENCE；supervision 源必为 SUPERVISOR；funding 两端必为 OBLIGATION；supersedes 两端同类）。`refinement`/`satisfies`/`ORDER`/`DATA` 在此处**被拒**（各有专用类型），避免执行依赖被写成通用边绕过检查。未改 `graph/task_network.py` 的同名类型，P2.1b 自行切换。 |
| 9 | P2.1b | `htn.py` | 新增 `assert_occurrences_match_bindings(occurrences, bindings)`：`TaskSemanticBindingV1` 是 `obligation_id` 与 `form` 的权威，不一致即 `ContractError`（不向任一方向"自动修正"）。`OccurrenceSpec` docstring 与 `ProposedPlanDelta.assert_consistent_with()` 同步写明。 |
| 10 | 追加 CR-6 | `htn.py` | `MethodInstanceDraft` 与 `ChildBinding` 各加可选 `goal_occurrence_id`；`MethodInstanceDraft.effective_goal_occurrence_id` 未声明时由 `goal_id` 派生。新增 `assert_method_instances_match_occurrences(drafts, occurrences)`：按 `(goal_id, goal_occurrence_id)` 校验（占位任务不符 / 占位是 primitive 均拒），已接进 `assert_consistent_with`。`ChildBinding` 追加一条：`reuse_policy=NEW_WORK` 的 slot 不得指向别人的 occurrence（TG 裁决 9 的四种去重分开）。 |
| 11 | 追加 | `htn.py` | `GraphStructureBudget` 补投影维度 `max_nodes` / `max_edges` / `max_fan_out`，与 `max_live_tasks` / `max_depth` / `max_expanded_nodes` / `max_candidates` / `max_recursion_fuel` 并列，**全部必填正整数**且由 `budget_version` 版本化；docstring 逐维写明含义（规划界 vs 投影界是两回事：计划可以在深度界内，却投影成没人该调度的图）。 |
| 12 | 追加 | `resolution.py` / `htn.py` | `match_review_criteria` 的 `optional_ids` 标注改 `frozenset[str]`（第 2 项把返回类型改成 frozenset 带出的）。同轮还修掉 `htn.py` 里一处 `keys` 与同函数上文 `keys: list[str]` 重名导致的 mypy 冲突（改名 `resource_keys`）。**`mypy src/agent_orchestrator` 现在对 `contracts/`、`knowledge/`、`planning/htn/applicability.py` 零错误**；仓库总数 17，全部来自既有 `evaluation/` `runtime/` 的可选依赖缺失。 |

### 本轮的偏差与理由

12. **第 2 项的硬约束结构校验只在 `requirements_content_hash` 存在时强制**。原请求是"`ReviewPackage` 构造时必须满足和 `RequirementsRevision` 相同的结构规则"。但 P1.1b 的 `test_package_expression_must_match_the_requirements_revision` 正是**故意构造一个把硬约束藏进 `any` 的被篡改 package**，用来证明它们的 requirements 摘要比对能抓住；在构造期一律拒绝会让那条测试无法建出被测对象，而我不能改他们的文件。

    折中：package 若声明了自己镜像哪一版要求（`requirements_content_hash` 非 None），就必须保持硬约束独立 AND（构造期拒绝）；未声明的 package 仍可构造 —— 那正是"被篡改的锚"的形状，抓它是摘要比对的职责。同时公开 `hard_constraint_violations()`，P1.1b 下次动自己文件时可直接升级为构造期拒绝。**这一条需要协调者裁决是否按原意收紧。**

13. **第 6 项没有把"集合端口必须声明顺序"做成构造期强制**。P2.1b 的 `test_projection_integrity.py` 有三处构造未声明 ordering 的 SET 端口；强制会让全目录变红，而我不能改他们的文件。改为：字段可选、声明本身严格校验、另给 `undeclared_set_ports()` 供编译/验证层拒绝。TG §4.3 的"必须"落在网络准入那一层，而不是端口字面量上。

14. **第 8 项未改 `graph/task_network.py`**。仓库里现在有两份 `TypedEdge`（contracts 的严格版 + P2.1b 的原版）。P2.1b 切换后删除旧的。

### 本轮新增测试

共 **+55 条**（170 → 225）：

- `test_semantic_binding_codec.py`（80 → 116）：GraphStructureBudget 往返 / 九维必填 / 零值拒绝、SET 端口顺序四组、`undeclared_set_ports`、未声明端口不含新键、primitive 资源声明往返、compound 拒绝、`resource_conflicts` 四种重叠 + 跨 namespace 同名不冲突、五种 typed 关系正向 + 四种端点错配拒绝 + 四种"有专用类型的关系不是通用边"、权威一致（正向 / 换责任 / 换 form / 无绑定 / delta 自检）、provenance（模型可 MODEL 不可 TOOL、feedback 不可 SYSTEM、未标注不含键）、CR-6 八条（派生 occurrence、两消费者各自 occurrence、错任务拒、primitive 拒、不在本增量中跳过、SHARE_ACTIVE 可指向共享 occurrence、NEW_WORK 拒、未共享不含键）。
- `test_aer_contract_codecs.py`（42 → 51）：producer_agent_ids、WRITE 拒绝、默认 READ_ONLY 与 NONE、绑定 requirements 的硬约束正/反、未绑定仍报告 violations、`criteria_only_under_any` 公开且忽略也出现在 any 之外的准则、requirements hash 必须是摘要、TypedRef 归属正/反、未知归属拒绝。
- `test_predicate_truth_table.py`（57 → 63）：注册观察者的封闭域否定被接受、陌生观察者 / 未署名 / best-effort / 开放域四种拒绝、带 observer 的观察往返。

### 本轮验证

```
uv run --frozen --group dev --extra local-capacity pytest tests/orchestrator/full_target -q \
  --ignore=tests/orchestrator/full_target/test_input_manifest_resolution.py
→ 597 passed, 1 skipped

uv run --frozen --group dev ruff check / ruff format --check <本片源文件 + tests/orchestrator/full_target>
→ All checks passed / all formatted
uv run --frozen --group dev mypy src/agent_orchestrator
→ contracts/、knowledge/、planning/htn/applicability.py 零错误；仓库总数 17（均为既有可选依赖缺失）
```

**全目录当前有一个收集错误，不是本片造成的**：`tests/orchestrator/full_target/test_input_manifest_resolution.py`（P2.2b）import `agent_orchestrator.artifacts.input_bindings`，该模块尚未落地 —— 测试文件先于实现写入，属于并发小片的在途状态。除该文件外全目录全绿。

未提交、未推送、未跑全量回归、未改动其他小片的文件。
