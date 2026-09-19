# H1-F 实施日志：PlanningDecision 确定性准入

**片名：** `h1-f-decision-admission`
**开工基线：** `0d89307`（`docs(h1): archive full-gate report run on merged main (D)`）
**权威规格：** V2《simpleharness-llm-native-htn-execution-plan-v2.zh-CN.md》第 12、16–19、24–34、39、40、43 节；补遗《LLM-native-HTN计划V2-裁定补遗-2026-09-18.zh-CN.md》（含末尾 2026-09-19 06:30 追加裁定）
**本片日志：** 仅本文件；其他片的 journal 未改。

---

## 1. 交付与范围

新增两个文件（白名单内），未改任何既有文件：

| 路径 | 说明 |
|---|---|
| `src/agent_orchestrator/planning/decision_admission.py` | 纯函数准入模块：`AdmissionContext`、`AdmittedPlanningDecision`、`admit_planning_decision` 及只读视图 |
| `tests/orchestrator/full_target/test_planning_decision_admission.py` | 161 个用例：分阶段正反例、黄金夹具遍历、顺序稳定性、反馈不泄漏、变异判别（第 12 节处置轮补 29 条） |

**纯函数约束（无副作用）：** 模块不访问数据库、不 import 存储层读写类（`storage/` 下任何模块）、不 compile、不 commit、不发事件。只 import `contracts/`（`planning_decisions`、`evidence_state`、`htn`、`models`、`semantic_base`）——全部是数据契约，无 I/O。

**未改的冻结件：** `contracts/planning_decisions.py`（信封/载荷/枚举/哈希三个函数）、`contracts/schemas/*.json`、`tests/orchestrator/full_target/fixtures/planning_decision_v1/invalid/*.expect.json`（全部原样，一行未动）。新增的 `decision_admission.py` 未被任何其他模块 import，线上接线（`hierarchical_dispatch.py`）属 H1-H，本片未碰。

---

## 2. 测试先行（红 → 绿）

先红后绿，再补判别力，最后把夹具清单改成「遍历而非钉数量」。完整提交列表以 `git log --oneline 0d89307..HEAD` 为准；按时间顺序：

```
test(h1-f): red tests for planning decision admission (ordered checks, golden fixtures)
test(h1-f): pin stage order, budget-account and one-problem-per-finding semantics
feat(h1-f): planning decision admission (ordered deterministic checks, typed admitted command, model-facing feedback)
test(h1-f): walk the H1-F fixture list instead of pinning its count
docs(h1-f): implementation journal (ordered admission stages, code-to-test table, mutation battery)
```

**红：** 先写 `test_planning_decision_admission.py` 并提交（`202907b`）。此时实现文件不存在，跑任意用例都是：

```
E   ModuleNotFoundError: No module named 'agent_orchestrator.planning.decision_admission'
```

**绿：** 再加入 `decision_admission.py`（`8f861ca`），targeted 全绿（处置轮补强后为 161 条，见第 12 节）：

```
PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_admission.py -q -p no:cacheprovider
161 passed in 0.23s
```

**全量：** `tests/orchestrator/full_target` 在实现提交后：

```
PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target -q -p no:cacheprovider
3639 passed, 2 skipped in 134.10s (0:02:14)
```

（2 个 skip：`test_panda_backend.py` 未配置 `SH_PANDA_PARSER`；`test_real_provider_hierarchical_smoke.py` 需 `--run-real-provider`。二者与本片无关。基线 `plans/llm-native-htn/H0/test-results.json` 记 full_target=2960；实现提交后 3582、处置轮后 3639，均 ≥ 2960，0 新失败。）

**ruff：**

```
uv run --offline ruff check src/agent_orchestrator/planning/decision_admission.py tests/orchestrator/full_target/test_planning_decision_admission.py
All checks passed!
```

**哨兵：** `grep -rn "_new_mode" src/agent_orchestrator | wc -l` → **26**，与开工时一致，未新增。

---

## 3. §43 顺序：一个阶段一个阶段

`admit_planning_decision(decision, *, context)` 的控制流即 §43 顺序；每个阶段独立构造、独立判定，**先失败先返回**，不同阶段的错误绝不混报。

| # | 阶段 | 判据 | 拒绝码 |
|---|---|---|---|
| 1 | 请求身份 / 上下文 | 不是 `PlanningDecisionEnvelopeV1` 或不是 `AdmissionContext` | `INTERNAL_CONTRACT_ERROR` |
| 2 | 包 / 提示词绑定 | `binding` 的 package_version / package_hash / prompt_version / prompt_hash ≠ 当前 | `PACKAGE_HASH_MISMATCH` |
| 3 | 计划修订号（§34） | `base_plan_revision` / `requirements_revision` / `scope_epoch_digest` ≠ 当前 | `REQUEST_BINDING_STALE` |
| 4 | 规划对象（§19） | `subject_key` 不在请求记录的 `planning_subjects` 里 | `SUBJECT_NOT_IN_REQUEST` |
| 5 | 引用清单（§17、§18） | 决定引用的四元组 ≠ **请求记录保存的** `visible_refs` 中任一条 | `REF_OUTSIDE_CONTEXT` |
| 6 | 阶段启用（§12） | `decision_type`（REPAIR 用 `REPAIR/<repair_kind>`）不在 `enabled_decision_types` | `DECISION_NOT_ENABLED_IN_PHASE` |
| 7 | 预算 / 上限 | 非 WAIT/NO_CHANGE/DECLARE_BLOCKED 时：坐标耗尽或账户不可用 | `PLANNING_BOUND_REACHED` / `BUDGET_INSUFFICIENT` |
| 8 | 载荷（§24–§31） | REPLACE_METHOD 的目标实例 / 运行中工作 / 修复许可；PROPOSE_SUCCESSOR 的开义务 / 精化环；BIND 的复用 / 共享 | `REPAIR_NOT_ALLOWED` / `METHOD_RETIRED` / `RUNNING_WORK_NOT_RECONCILED` / `OBLIGATION_NOT_OPEN` / `REFINEMENT_CYCLE` / `REUSE_NOT_ALLOWED` |
| 9 | 方法 / 适用性 / 证据 / 授权 / 能力 | 方法存在性 → 版本与哈希 → 生命周期 → 适用对象 → 参数 → 谓词 → 前提真值 → 授权 → 批准 → 能力 | `METHOD_NOT_FOUND` / `METHOD_STALE` / `METHOD_RETIRED` / `METHOD_REJECTED` / `METHOD_INAPPLICABLE` / `PARAMETER_INVALID` / `EVIDENCE_REQUIRED` / `EVIDENCE_CONFLICT` / `METHOD_NOT_AUTHORIZED` / `AUTHORIZATION_REQUIRED` / `CAPABILITY_MISSING` |
| 10 | 操作闸门 | 非起止型决定遇到 UNKNOWN 操作 | `OPERATION_UNRESOLVED` |
| 11 | 结构前置（编译前） | ORDER 环 → DATA 无生产者 → 覆盖缺口 | `ORDER_CYCLE` / `DATA_UNBOUND` / `COVERAGE_GAP` |
| — | 通过 | 返回 `AdmittedPlanningDecision`（原决定 + 已解析对象绑定 + 已解析方法引用 + `canonical_hash`） | — |

**「先失败先返回」的证据：**

- `test_a_package_mismatch_outranks_a_stale_binding`：同时错包和错修订号 → 只报 `PACKAGE_HASH_MISMATCH`（第 2 阶段先于第 3 阶段）。
- `test_a_stale_binding_outranks_a_bad_subject`：修订号不对且 subject 未知 → 只报 `REQUEST_BINDING_STALE`，`problems[0].field_path == "/plan_revision"`（绑定错误不指向 subject）。
- `test_a_bad_subject_outranks_a_ref_outside_context`：subject 未知且引用越界 → 只报 `SUBJECT_NOT_IN_REQUEST`。
- `test_the_binding_revision_outranks_the_phase_gate`：修订号不对且类型未启用 → 只报 `REQUEST_BINDING_STALE`。
- `test_a_payload_error_outranks_the_method_library` / `test_a_payload_error_outranks_an_unresolved_operation`：REPAIR 载荷错误先于方法库 / 操作闸门。
- `test_the_bound_outranks_an_exhausted_budget`：坐标与预算同时耗尽 → 只报 `PLANNING_BOUND_REACHED`。
- `test_the_same_input_yields_the_same_rejection_twice`：同一输入两次 `equality` 相等，码集稳定。

**一处可多条、不同阶段不混报：** `test_a_one_stage_refusal_reports_one_problem_per_finding` 让 `plan_shape.order_cycle` 带 3 条 → 同一阶段 3 条 `PlanningProblemDetailV1`，`rejection_codes` 去重后只有 `ORDER_CYCLE` 一条。

---

## 4. 通过路径：`AdmittedPlanningDecision`

frozen dataclass，字段：

- `decision`：原 `PlanningDecisionEnvelopeV1`（逐字节原样，不重编码）；
- `subject`：`subject_key` 解析到的请求记录行（`MappingProxyType`）；
- `method_refs`：已解析的方法引用（`MethodRef`：id/version/content_hash）；
- `method_instances`：已解析的方法实例引用（REPLACE_METHOD 的 `rejected_method_instance`、BIND 的 `consumer_method_instance_ref`）；
- `canonical_hash`：§15 的 `canonical_decision_hash(decision)`。

对应用例：`test_the_admitted_command_carries_the_resolved_subject_methods_and_hash`、`test_the_admitted_command_records_resolved_method_instances`、`test_an_admitted_command_is_frozen`（改字段抛异常）、`test_the_admitted_decision_is_not_a_commit`（两次调用都通过=无副作用、不提交）。

**通过 ≠ 提交：** 模块不发事件、不写库、不 compile；返回的是命令，供 H1-G 适配层与 H1-H 接线消费。

---

## 5. 引用逐字节匹配（§17、§18、补遗）

- 四元组 `(kind, id, semantic_revision, content_hash)` **全部**参与比较；`_ref_key` 一次算全，任何一分量不同即不等。
- `test_a_one_character_difference_in_any_quadruple_component_is_refused`：对 `method_ref` 的 kind / id（多一个 `X`）/ semantic_revision / content_hash 各改一处 → 全部 `REF_OUTSIDE_CONTEXT`，`field_path == "/payload/method_ref"`。
- 引用位置用 JSON 指针定位：`reason_refs` 用 `/reason_refs/<i>`（`test_a_reason_ref_absent_from_the_saved_list_is_refused` 钉 `/reason_refs/0`），载荷内引用指向其字段。
- **补遗 2026-09-19 06:30 追加裁定：** 核对以**请求记录里保存的清单**为准，不从包体重算。`test_a_ref_in_the_saved_list_but_not_exposed_by_the_package_is_irrelevant` 用一个只含 `method_ref` 的 `visible_refs` 就能通过——模块从不持有包体，也不调用 `planner_package` 的收集器。

---

## 6. 拒绝码 → 用例名（V2 §33 全覆盖）

§33 共 36 个码。**属于本片（准入层）28 个**，每个至少一条用例：

| 拒绝码 | 用例名 |
|---|---|
| `SUBJECT_NOT_IN_REQUEST` | `test_a_bad_subject_outranks_a_ref_outside_context`、`test_every_h1f_fixture_yields_its_expected_code[subject-not-in-request]` |
| `REF_OUTSIDE_CONTEXT` | `test_a_one_character_difference_in_any_quadruple_component_is_refused`、`[ref-outside-context]` |
| `REQUEST_BINDING_STALE` | `test_plan_revision_mismatch_is_request_binding_stale`、`[request-binding-stale]` |
| `PACKAGE_HASH_MISMATCH` | `test_a_package_or_prompt_binding_mismatch_is_refused`、`[package-hash-mismatch]` |
| `DECISION_NOT_ENABLED_IN_PHASE` | `test_a_phase_refusal_outranks_a_payload_error`、`test_repair_kind_is_gated_by_the_enabled_set`、`[decision-not-enabled-in-phase]` |
| `METHOD_NOT_FOUND` | `test_a_method_the_library_does_not_hold_is_refused`、`[method-not-found]` |
| `METHOD_STALE` | `test_a_method_behind_the_library_revision_is_refused`、`[method-stale]` |
| `METHOD_RETIRED` | `test_a_retired_method_is_refused`、`test_an_instance_that_is_not_active_on_the_subject_is_refused`、`[method-retired]` |
| `METHOD_REJECTED` | `test_a_rejected_method_is_refused`、`[method-rejected]` |
| `METHOD_INAPPLICABLE` | `test_a_method_a_false_precondition_blocks_is_refused`、`test_a_method_that_does_not_apply_to_this_subject_is_refused`、`[method-inapplicable]` |
| `METHOD_NOT_AUTHORIZED` | `test_a_method_without_its_grant_is_refused`、`[method-not-authorized]` |
| `AUTHORIZATION_REQUIRED` | `test_a_withheld_approval_is_refused`、`[authorization-required]` |
| `PARAMETER_INVALID` | `test_bindings_that_do_not_fill_the_parameters_are_refused`、`[parameter-invalid]` |
| `EVIDENCE_REQUIRED` | `test_an_unknown_premise_is_evidence_required`、`test_an_unregistered_predicate_is_evidence_required`、`[evidence-required]` |
| `EVIDENCE_CONFLICT` | `test_a_conflicting_premise_is_evidence_conflict`、`[evidence-conflict]` |
| `CAPABILITY_MISSING` | `test_a_missing_capability_is_refused`、`[capability-missing]` |
| `BUDGET_INSUFFICIENT` | `test_exhausted_budget_is_refused`、`test_a_budget_account_that_is_not_available_is_refused_even_with_rounds_left`、`[budget-insufficient]` |
| `PLANNING_BOUND_REACHED` | `test_the_planning_bound_is_refused`、`[planning-bound-reached]` |
| `OPERATION_UNRESOLVED` | `test_an_unresolved_operation_blocks_refining`、`[operation-unresolved]` |
| `RUNNING_WORK_NOT_RECONCILED` | `test_running_work_on_the_retired_instance_is_refused`、`[running-work-not-reconciled]` |
| `REPAIR_NOT_ALLOWED` | `test_a_phase_that_does_not_allow_repair_is_refused`、`[repair-not-allowed]` |
| `OBLIGATION_NOT_OPEN` | `test_a_successor_on_a_closed_obligation_is_refused`、`[obligation-not-open]` |
| `REUSE_NOT_ALLOWED` | `test_a_resolution_that_is_not_current_cannot_be_reused`、`test_a_goal_that_is_no_longer_demanded_cannot_be_shared`、`[reuse-not-allowed]` |
| `REFINEMENT_CYCLE` | `test_a_successor_that_would_refine_itself_is_refused`、`[refinement-cycle]` |
| `ORDER_CYCLE` | `test_a_refinement_that_closes_an_order_cycle_is_refused`、`[order-cycle]` |
| `DATA_UNBOUND` | `test_a_data_edge_without_a_producer_is_refused`、`[data-unbound]` |
| `COVERAGE_GAP` | `test_a_refinement_that_leaves_an_obligation_uncovered_is_refused`、`[coverage-gap]` |
| `INTERNAL_CONTRACT_ERROR` | `test_a_non_envelope_decision_is_an_internal_contract_error`、`test_a_caller_that_passes_no_usable_context_raises_rather_than_half_returns`、`[internal-contract-error]` |

**不属于本片（8 个），属于 H1-A2b / H1-C：**

| 拒绝码 | 归属 | 现有用例 |
|---|---|---|
| `DECISION_BLOCK_MISSING` | H1-C codec | `test_every_h1f_fixture_yields_its_expected_code[decision-block-missing]`（本片遍历，走 codec） |
| `MULTIPLE_DECISIONS` | H1-C codec | `[multiple-decisions]` |
| `MIXED_PROTOCOL_BLOCKS` | H1-C codec | `[mixed-protocol-blocks]` |
| `UNKNOWN_FIELD` | H1-A2b / H1-C | `test_planning_decision_codec.py`、`[unknown-field]`（H1-A2b） |
| `MALFORMED_DECISION` | H1-A2b / H1-C | `test_planning_decision_codec.py`、`[malformed-*]` |
| `MODEL_SET_SYSTEM_FIELD` | H1-C codec（§32） | `test_planning_decision_codec.py`、`[model-set-system-field]` |
| `DECISION_TYPE_UNKNOWN` | H1-A2b / H1-C | `test_planning_decision_codec.py`、`[decision-type-unknown]` |
| `STRUCTURE_INVALID` | H1-A2b 契约层 | `[structure-*]`（三个） |

对照表本身由测试钉死：`ADMISSION_CODE_CASES`（28 个准入码 → 用例名）+ `CODEC_LAYER_CODES`（8 个上游码）必须恰好等于 `PlanningDecisionRejectionCode` 全集，且两集合不相交（`test_every_admission_rejection_code_has_a_case`）。这样漏一个码就是红测试，不需要人工核对表。

**「仅解码类型」一律 `DECISION_NOT_ENABLED_IN_PHASE`：** REQUEST_EVIDENCE / REQUEST_HUMAN / PROPOSE_METHOD 在 H1 的 `H1_DECISION_ENABLEMENT` 里是 `_DECODE_ONLY`（`executable=False`），准入第 6 阶段据此拒绝。`test_every_decode_only_valid_fixture_is_phase_refused` 对三个 `valid/` 夹具逐一断言。判据直接读代码常量 `H1_DECISION_ENABLEMENT`（测试里的 `H1_ENABLED` 由它派生），不另抄一张表。

---

## 7. 黄金夹具：脚本遍历，不手抄

`test_every_h1f_fixture_yields_its_expected_code` 由 `_h1f_cases()` 参数化——它扫描 `invalid/*.expect.json`，取全部 `"checked_in": "H1-F"` 的样例（当前 **31** 个；清单**靠遍历得出**，测试不钉数量，H1-I 加夹具不会误红），与 `.expect.json` 里的 `expected_stage` / `expected_code` 逐一比对：

- **admission 阶段（28 个）**：`PlanningDecisionEnvelopeV1.from_json(raw)` 解码，再交给准入；上下文由 `_context_for(code, raw)` 生成，用**该夹具自己的引用**补进 `visible_refs`（否则更早的引用阶段会先说话），只有 `REF_OUTSIDE_CONTEXT` 自己保留越界引用。
- **codec 阶段（3 个：`decision-block-missing` / `multiple-decisions` / `mixed-protocol-blocks`）**：这些 `.json` 是场景描述（`model_reply` 是一句说明，见补遗 §四），所以本片**构造**它们描述的原回复——由 `valid/refine.json` 经 `serialize_planning_decision` 得块，再拼两遍 / 拼一个 legacy 块——喂 `parse_planning_decision`，断言 `PlanningDecisionCodecError.code` 等于 expect 文件写的码。没有手抄任何码。

`valid/` 侧：8 个本阶段可执行的样例（refine / repair×2 / bind×2 / declare-blocked / wait / no-change）在匹配上下文下**通过**（`test_every_executable_valid_fixture_is_admitted`）；3 个仅解码样例**阶段拒绝**（`test_every_decode_only_valid_fixture_is_phase_refused`）。整个 `invalid/` 目录的 39 个样例都用 expect 文件驱动，没有手写清单。

---

## 8. 交付清单（实现）

`src/agent_orchestrator/planning/decision_admission.py`：

- `AdmissionContext`（frozen）：14 个必填 + 只读视图字段，**每个字段 docstring 写明来源**（请求记录、当前修订、包/提示词、方法库视图、谓词集合、活动方法实例、授权/能力/预算/操作/结构），在 `__post_init__` 里逐字段校验。
- 只读视图（都 frozen，各自校验并写来源）：`MethodView`、`MethodInstanceView`、`AuthorizationView`、`CapabilityView`、`BudgetView`、`OperationStateView`、`PlanShapeView`；`PlanningRetryBudgetView` 用契约层既有类型。
- `AdmittedPlanningDecision`（frozen）：原决定 + 解析对象绑定 + 解析方法引用 + `canonical_hash`。
- `admit_planning_decision(decision, *, context)`：§43 十一个阶段，先失败先返回，返回 `AdmittedPlanningDecision | PlanningFeedbackV1`。
- `_feedback`：构造 `PlanningFeedbackV1`——`status=REJECTED`、码去重保序、问题带 JSON 指针、`changed_refs=()`、`budgets=context.retry_budgets`（§39）。
- 内部错误（非信封 / 非上下文）→ `INTERNAL_CONTRACT_ERROR`；若上下文本身不可用（无法给出 §39 预算），改为抛 `ContractError` 而不是半返回。

**反馈可直接回喂：** `test_the_feedback_round_trips_as_previous_feedback` 断言 `PlanningFeedbackV1.from_json(feedback.to_json()) == feedback`、`planner_package._feedback_json(feedback) == feedback.to_json()`，且 `previous_decision_id == context.decision_id`、`budgets == context.retry_budgets`。`test_the_feedback_carries_no_internal_fields` 断言 `to_json()` 的键集合恰为 §39 六字段，问题键集合是 §40 的子集（无系统内部字段）。`test_every_problem_is_located_by_a_json_pointer` 断言每条问题的 `field_path` 以 `/` 开头。

---

## 9. 变异（本片 27 个，全 killed；处置轮再加 13 个，见第 12 节）

用脚本注入 27 个变异，逐个跑 targeted 套件，全部转红；恢复实现后 targeted 全绿。脚本清单一字排开（每个变异都对应至少一条用例）：

| # | 变异 | 结果 |
|---|---|---|
| M1 | 起止型决定绕过坐标检查 | KILLED |
| M2 | 阶段闸门排到绑定修订之前 | KILLED |
| M3 | 引用只比 id | KILLED |
| M4 | 修订号检查取消 | KILLED |
| M5 | 包检查取消 | KILLED |
| M6 | subject 检查取消 | KILLED |
| M7 | 方法生命周期检查取消 | KILLED |
| M8 | 方法版本/哈希退化为只比 id | KILLED |
| M9 | 参数检查取消 | KILLED |
| M10 | 谓词注册检查取消 | KILLED |
| M11 | 前提真值码表取消 | KILLED |
| M12 | 能力检查取消 | KILLED |
| M13 | 批准检查取消 | KILLED |
| M14 | 运行中工作检查取消 | KILLED |
| M15 | 复用检查取消 | KILLED |
| M16 | 共享检查取消 | KILLED |
| M17 | 开义务检查取消 | KILLED |
| M18 | 操作闸门取消 | KILLED |
| M19 | 结构前置检查取消 | KILLED |
| M20 | 修复许可检查取消 | KILLED |
| M21 | 内部契约守卫对所有输入返回反馈 | KILLED |
| M22 | REPAIR 启用键退化为 `REPAIR` | KILLED |
| M23 | 忽略预算账户可用性 | KILLED（补 `test_a_budget_account_that_is_not_available_is_refused_even_with_rounds_left` 后） |
| M24 | 反馈预算替换成 0 | KILLED |
| M25 | 拒绝码去重取消 | KILLED |
| M26 | 拒绝问题的指针写成 §32 字段名（`/plan_revision`） | KILLED |
| M27 | 拒绝反馈里泄漏内部操作 id | KILLED |

M23 第一轮**存活**（测试只用一个 round-count 覆盖了预算），补上「预算账户不可用但仍有轮次」的判别用例后 KILLED——这正是本轮测试补强的来由。

---

## 10. 边界与未做（明确）

- **未接线上：** 未改 `hierarchical_dispatch.py`、`event_handler.py`、`plan_commits.py`、`role_templates.py`、`planner_package.py`。本片只交付纯函数模块与测试，接线属 H1-G / H1-H。
- **未改契约：** `contracts/planning_decisions.py`、`contracts/schemas/*.json`、`fixtures/**/*.expect.json` 一行未动；未新增枚举、字段名或拒绝码。
- **未访问存储：** 模块不 import `storage/`，不 open DB，不做 compile / commit / 事件。
- **未做 H2 的能力：** `unrelated-change 精确重评`（第 34 节留到 H2）——H1 的绑定修订号直接比较。
- **结构前置（第 11 阶段）是调用方输入：** `PlanShapeView` 由调用方的结构预检填充；真正的编译器复检仍在 `compile_proposal`（H1-H 不动它）。本片只是把编译期会出现的结构拒绝名提前报给模型。
- **`INTERNAL_CONTRACT_ERROR` 的语义：** 表示调用方传入的「已解码件/上下文」本身不成立，不是模型错误；这条与 §33 的其它码一样可实测（非信封输入、无可用上下文输入）。
- **未跑真实模型、未读密钥、未 push、未 merge、未 stash / reset / checkout。**
- **未新增 `_new_mode` 哨兵**（保持 26），未改策略快照，未新增配置项。

---

## 11. 验收数字（实测）

| 项 | 实测 |
|---|---|
| targeted（本片） | `161 passed in 0.23s`（处置轮补强后，见第 12 节） |
| full_target | `3639 passed, 2 skipped in 134.10s (0:02:14)`（基线 2960，0 新失败） |
| ruff（本片 2 文件） | `All checks passed!` |
| 哨兵 `_new_mode` | 26（无新增） |
| 变异 | 27/27 + 13/13 killed（处置轮，见第 12 节） |
| 工作树 | clean（提交见文末） |

## 提交

实现期间（`0d89307..5804dc3`）共 9 笔，完整列表见第 12 节的「最终提交」一栏（以 `git log --oneline 0d89307..HEAD` 为准，不在此处复制一份会漂移的清单）。

**结论：** 本片交付如上，工作树在本文件提交后 clean。

---

## 12. 核验处置（第 1 轮：修后可合）

**核验报告：** `plans/llm-native-htn/H1/reviews/verify-H1-F-2026-09-19.md`（含第 1 轮 + 复核 2/3/4）。**结论：VERDICT: 修后可合**（无 P0）。受检 HEAD `5804dc3`。

### 12.1 问题收口

| 编号 | 类型 | 处置 | 说明 |
|---|---|---|---|
| P1-1 | **行为 bug** | 已修 | 空集合拼进 `expected`/`observed` 时准入抛 `ContractError`：空启用集 → 期望 `DECISION_NOT_ENABLED_IN_PHASE`；空 `bindings` + 必填参数 → 期望 `PARAMETER_INVALID`。空串改传 `None`（§40 允许 `str \| None`）。 |
| P1-2 | 测试缺口 | 已补 | 空 `visible_refs` 直接放行：加「空清单 + 决定引用四元组 → `REF_OUTSIDE_CONTEXT`」，并加「空清单 + NO_CHANGE 无引用 → 仍准入」以区分两种读法。 |
| P1-3 | 测试缺口 | 已补 | 方法身份只比 version 不比 hash：加「version 对、hash 不对 → `METHOD_STALE`」。 |
| P1-4 ~ P1-10、P1-13、P1-14 | 测试缺口 | 已补 | `_decision_refs` 的每个引用位置补一条「隐藏该引用 → `REF_OUTSIDE_CONTEXT`」，由 `CITATION_SITES` 参数化驱动（9 个位置），并另加每个位置「四元组动一个分量」的用例。 |
| P1-6（实例侧） | 测试缺口 | 已补 | 修复目标实例「同 id、错 hash / 修订号动一格 → `METHOD_RETIRED`」。 |
| P1-11、P1-12 | 测试缺口 | 已补 | `requirements_revision` / `scope_epoch_digest` 失配 → `REQUEST_BINDING_STALE`，指针各自钉死。 |
| P2-3 | 文档笔误 | 已修 | `ADMISSION_CODE_CASES["DECISION_NOT_ENABLED_IN_PHASE"]` 指向不存在的测试名，改为真名 `test_every_decode_only_valid_fixture_is_phase_refused`。 |
| P2-4 | 反馈质量 | 已修 | `METHOD_STALE` 的 `expected`/`observed` 改为 `版本@哈希`，哈希失配时两边不再都是 `"2"`。 |
| P2-6 | 测试/文档 | 已修 | 「无可用上下文」用例现在断言抛出的就是 `INTERNAL_CONTRACT_ERROR` 语义（消息前缀 + 构造同码问题）；本 journal 提交列表改为以 `git log` 为准，不再手抄。 |
| P2-1 | 不阻塞 | 备案 | 上下文类错误的 JSON 指针（`/package_hash` 等）在决定 JSON 上解析不到；§40 允许 `field_path=None`，不在模型 JSON 里的错误无指针可指，不改。 |
| P2-2 | 不阻塞 | 备案 | 实现在 §43 列表之外插入预算阶段与结构前置阶段；§43 所列阶段相对顺序未乱，两码均属 §33，属扩展而非颠倒。 |
| P2-5 | 不阻塞 | 备案 | `goal_type_ref` 不进 `visible_refs`、按类型注册表校验；`AdmissionContext` 最小集未含类型注册表，伪造该引用仍被准入。§43/任务书最小集未列此项，编译期也会接住，属扩展而非本片必做。 |

### 12.2 本轮新增用例（161 = 上轮 132 + 29）

先提交红测试（`64a5775`），再提交实现修复（`1fd8329`）。红态尾行：

```
4 failed, 157 passed in 0.36s
```

（4 条失败 = P1-1 的两条崩溃 + P1-3/P2-4 的两条身份反馈；其余 25 条新用例在旧实现上已绿——它们是判别力用例，靠变异电池证明其必要。）

### 12.3 变异电池（本轮 40 个，全 killed）

两脚本、两轮，实现被改后各自刷新基准 `/tmp/mut_h1f/orig.py` 再跑（脚本用 `cp` 备份/恢复，未用 `git stash`）：

| 电池 | 数目 | 结果 |
|---|---|---|
| 原 27 个（含 M1–M27） | 27 | **27/27 killed** |
| 核验员存活变异 13 个（P1-2 ~ P1-14） | 13 | **13/13 killed** |

核验员记录的 13 个存活变异，逐个被新用例杀死（尾行原样）：

| 变异 | 尾行 |
|---|---|
| P1-2 空清单放行 | `6 failed, 155 passed in 0.35s` |
| P1-3 身份只比 version | `4 failed, 157 passed in 0.35s` |
| P1-4 不扫描 WAIT `wait_for` | `7 failed, 154 passed in 0.36s` |
| P1-5 不扫描 BIND 消费实例 | `6 failed, 155 passed in 0.39s` |
| P1-6 修复实例只比 id | `6 failed, 155 passed in 0.38s` |
| P1-7 不扫描 successor `old_task_ref` | `6 failed, 155 passed in 0.35s` |
| P1-8 不扫描 BIND `resolution_ref` | `6 failed, 155 passed in 0.35s` |
| P1-9 不扫描 `rejected_method_instance` | `6 failed, 155 passed in 0.36s` |
| P1-10 不扫描 successor `obligation_ref` | `6 failed, 155 passed in 0.38s` |
| P1-11 不比 `requirements_revision` | `6 failed, 155 passed in 0.39s` |
| P1-12 不比 `scope_epoch_digest` | `6 failed, 155 passed in 0.34s` |
| P1-13 不扫描 BIND `goal_ref` | `6 failed, 155 passed in 0.36s` |
| P1-14 不扫描 `replacement_method_ref` | `6 failed, 155 passed in 0.37s` |

（P1-4 第一次写成空 `elif` 体，是语法错误而非存活变异；已改成合法变异 `pass` 重跑，仍 killed。）

### 12.4 复用正文对照表

§6 的「拒绝码 → 用例名」表本轮只在 `DECISION_NOT_ENABLED_IN_PHASE` 一行更新了测试名（P2-3）。**归属不变：** 准入层 28 码属本片，其余 8 码（`DECISION_BLOCK_MISSING` / `MULTIPLE_DECISIONS` / `MIXED_PROTOCOL_BLOCKS` / `UNKNOWN_FIELD` / `MALFORMED_DECISION` / `MODEL_SET_SYSTEM_FIELD` / `DECISION_TYPE_UNKNOWN` / `STRUCTURE_INVALID`）属 H1-A2b / H1-C 的 codec 与契约层。

### 12.5 处置轮验收数字（实测尾行）

```
$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_admission.py -q -p no:cacheprovider
161 passed in 0.23s

$ uv run --offline ruff check src/agent_orchestrator/planning/decision_admission.py tests/orchestrator/full_target/test_planning_decision_admission.py
All checks passed!

$ grep -rn "_new_mode" src/agent_orchestrator --include='*.py' | wc -l
      26
```

### 12.6 最终提交（原样粘贴 `git log --oneline 0d89307..HEAD`）

```
1fd8329 fix(h1-f): refuse instead of crashing on empty problem sides; name both halves of a method identity
64a5775 test(h1-f): close the review's mutation holes (empty lists, hidden citations, same-id-wrong-hash instances)
5804dc3 docs(h1-f): journal tails after the feedback-leak tests (committed by the orchestrator: the agent's turn was cut off by the endpoint quota; 132 passed re-measured)
d897e97 test(h1-f): pin that a refusal never leaks a system field name or an internal id
f6ba22d docs(h1-f): make the journal count-independent (commit list from git log)
1285c16 docs(h1-f): fold the fixture-walk commit and mutation count into the journal
35310af test(h1-f): walk the H1-F fixture list instead of pinning its count
88314d3 docs(h1-f): implementation journal (ordered admission stages, code-to-test table, mutation battery)
8f861ca feat(h1-f): planning decision admission (ordered deterministic checks, typed admitted command, model-facing feedback)
e587f97 test(h1-f): pin stage order, budget-account and one-problem-per-finding semantics
202907b test(h1-f): red tests for planning decision admission (ordered checks, golden fixtures)
```
