# H1 实施日志（HTN-LLM-NATIVE-2.0）

**实施者会话。** 只交付，不自证。独立核验会话对照本目录与 `/tmp` 输出复核。

---

## H1-A1：PlanningDecision 合同核心（enums / refs / request binding / feedback / decision_id）

- **基线 HEAD：** `e1684b7a617cba1a18ba9f70215a39c4aa805c3a`（`docs(h0): realign the baseline package to the V2 plan layout`）
- **依据：** `simpleharness-llm-native-htn-execution-plan-v2.zh-CN.md` §11–§17、§33–§40、§16；`H1-A-B-C任务书草案-2026-09-18.zh-CN.md`；`H1-V2对照源码冲突检查-2026-09-18.zh-CN.md` §5.1、§7、§8。
- **范围：** 只做协议*核心*。

### 新增文件

- `src/agent_orchestrator/contracts/planning_decisions.py`
  - 限额常量（§16，全部 11 个，含 `MAX_PD_ARGUMENTS`）= V2 字面值。
  - 五个 `StrEnum`：`PlanningDecisionType`（9）、`PlanningRefKind`（15）、`PlanningDecisionRejectionCode`（36）、`PlanningDecisionStatus`（8）、`AssumptionRisk`（3），成员名与值逐字取 V2。
  - `DecisionEnablement` + `H1_DECISION_ENABLEMENT`：§12 H1 列，按类型/REPAIR 子类索引的机器可读表。
  - 严格 dataclass（frozen/slots，`to_json`/`from_json`，未知/缺字段/错类型均 `ContractError`）：`PlanningRefV1`、`PlanningRequestBinding`、`PlanningProblemDetailV1`、`PlanningRetryBudgetView`、`PlanningFeedbackV1`。
  - `compute_decision_id(request_id, attempt_ordinal, raw_output_hash)`：`\x1f` 分隔 + 末段 codec 版本，`sha256.hexdigest()[:24]`，前缀 `pd-`；`attempt_ordinal` 必须为 ≥0 的 int（bool 拒绝）。
  - `__all__` 列全公开名。
- `tests/orchestrator/full_target/test_planning_decision_contract.py`（先红后绿）

### 测试先行

- 先写测试：`ModuleNotFoundError: No module named 'agent_orchestrator.contracts.planning_decisions'`（红）。
- 实现后：**`39 passed in 0.05s`**（`PYTHONPATH=src uv run pytest tests/orchestrator/full_target/test_planning_decision_contract.py -q -p no:cacheprovider`）。
- `uv run ruff check <两新文件>` → **All checks passed!**
- 覆盖：五枚举成员名/值/个数逐字钉死（字面量清单）；11 个限额常量值；每个 dataclass 往返一致 + 未知字段/缺字段/错类型各一反例；`PlanningRefV1` 五种非法输入；`decision_id` 由测试内 `hashlib` 独立算出的字面量期望值 + 同输入同结果 + 任一输入变则变 + 非法 ordinal 拒绝；H1 启用表与 §12 一致。

### 变异（H1 纪律：≥1 killed；本片贡献 3）

| 变异 | 结果 |
|---|---|
| M1 改 `PLANNING_DECISION_CODEC_VERSION` 常量 | **KILLED**（`test_decision_id_matches_the_independent_literal`） |
| M2 `PlanningRefV1` 丢掉 `content_hash` 校验 | **KILLED**（非法输入 value3） |
| M3 把 `PROPOSE_METHOD` 的启用项从 decode-only 改成 executable | **KILLED**（`test_h1_decision_enablement_matches_section_12`） |

### 采用的假设（冲突检查 §6、§7；按「不答复则按建议 + 假设」执行）

- **BL-1（引用三种形状 / `method_instance`）：** 本片**未**加 `method_instance`，`PlanningRefKind` 保持 V2 §17 的 15 个值；等计划作者裁定。`PlanningRefV1` 与 `VersionedRef` 是分开的类型。
- **BL-2（各 kind 的 revision/hash 来源）：** 本片只做形状校验（id ≤256、`semantic_revision` ≥1、`content_hash` 为 64 位小写 hex）；来源表（§5.1）留给后续片接线时绑定。
- **BL-3（`$defs` 与小字符串封闭枚举）：** 本片不含 Envelope/payload/`$defs`，故未定小字符串封闭枚举。
- **BL-4（三型仅解码 payload 规格）：** 未做（属 Envelope/payload，等裁定）。
- **BL-5（空 payload 非合法 REFINE 黄金样例）：** 未做黄金样例。
- **BL-6（Schema 进包 + 无 jsonschema 测试策略）：** 未做 JSON Schema 文件。
- **C2 枚举假设：** `PlanningDecisionStatus` 取值用 §6/§36 的 8 个：`UNREADABLE / DECODED / REJECTED / ADMITTED / COMPILED / COMMIT_REJECTED / COMMITTED / NO_STATE_CHANGE`。
- **BL-7（`intent_id`）：** `PlanningRequestBinding` 在 §34 字段之外**追加** `intent_id: str`（必填），按冲突检查 BL-7 的建议。
- **C1 占位 payload / 第 4 条载荷字段集：** 本片不涉及（等 Envelope/事件片）。

### 明确未做（等待裁定的 BL-1…BL-6）

1. `PlanningDecisionEnvelopeV1`（模型线上对象，§13）。
2. 各 `decision_type` 的 payload dataclass（§24–§31）与系统字段禁止规则映射（§32）。
3. `AssumptionV1` / `PlanningUncertaintyV1` / `AlternativeSummaryV1` / `ReplanTriggerHintV1` 及 §6 的 `UncertaintySeverity` / `AlternativeDisposition` / `BlockerCode` / `ResumableIf` / `RepairKind` / `BindExistingGoalMode` 等小枚举。
4. JSON Schema 文件、`schemas/__init__.py`、黄金 fixtures（valid/invalid）。
5. `PlanningDecisionEvaluated` 事件载荷字段集（BL-9）。
6. `base_plan_revision != current → REQUEST_BINDING_STALE` 的判定逻辑（属 admission/接线）。

原因：这些部分都在 BL-1…BL-6 的裁定范围内，本片按「不答复则按假设」的处理方式，**只交付不受裁定影响的协议核心**，避免自行发明线上格式（wire format）。

### 边界

- 未改 `contracts/__init__.py`、`semantic_base.py`、`htn.py`、`SYSTEM_BOUND_FIELDS` 或任何既有 `src/` 文件。
- 未跑 grok CLI、未读密钥、未 push。

---

## H1-A1 处置（核验：修后可合）

- **依据：** `plans/llm-native-htn/H1/reviews/核验-H1-A1-2026-09-18.md`（核验副本目录）。核验未发现 P0，判定「修后可合」；P1 为一组测试缺口（规格常量可被静默改动而测试全绿）。
- **修复方式：** 只补测试，不改实现（`src/agent_orchestrator/contracts/planning_decisions.py` 零改动）。先确认缺口存在（把 `PLANNING_DECISION_SCHEMA_VERSION` 改成 `2`，旧测试仍 39 passed），再补钉死断言。
- **P1 修复：**
  - 新增 `test_wire_identity_constants_are_pinned`：逐字钉死 `PLANNING_DECISION_SCHEMA_VERSION == 1`、`PLANNING_DECISION_V1 == "planning-decision-v1"`、`LEGACY_PLANNING_PROTOCOL == "legacy-plan-proposal-v1"`、`PLANNING_DECISION_CODEC_VERSION == "planning-decision-codec-v1"`（KILL Y1/Y2/Y3）。
  - `PlanningRefV1` 负例补 `semantic_revision=0`（KILL Y6）。
- **P2 修复：**
  - `test_max_planning_ref_id_is_pinned_with_boundaries`：钉死 `MAX_PLANNING_REF_ID == 256`，并 256 接受 / 257 拒绝（KILL X5）。
  - `test_request_binding_package_version_lower_bound_is_pinned`：`package_version=0` 拒绝、`=1` 接受（KILL Y7）。
  - `test_planning_ref_all_fields_are_required_by_construction`：直接构造缺 `content_hash` 报 `TypeError`（KILL Y12）。
- **复验：** 上述 7 个原存活变异现全部 KILLED；恢复实现后 targeted **45 passed**。
- 未改 `contracts/` 既有文件；未 merge / push / stash / reset / checkout。

---

## H1-A1 第二轮处置（扩展变异 7 个 P1）

- **依据：** `plans/llm-native-htn/H1/reviews/核验-H1-A1-2026-09-18.md`（第二轮复核）。上一轮 7 个缺口已确认 KILLED；本轮扩展变异又发现 7 个存活变异（P1 测试缺口）。
- **根因：** 测试只覆盖了「字段存在 / 基本类型 / 能 `str()`」这条路径，没有覆盖 §39/§40 的**闭集枚举**与**字符串类型**语义，因此把校验换成 `str()` 后测试仍全绿。**实现本身正确**（7 处现均抛 `ContractError`），缺陷纯属测试未钉死。
- **先确认现状：** 逐一直接构造 `status="GARBAGE"`、`rejection_codes=("NOPE",)`、`problem.code="NOPE"`、`field_path=123`、`expected=123`、`intent_id=123`、`prompt_hash="not-a-hash"`，当前 `src` 全部抛 `ContractError`（红点仅在变异后暴露）。
- **P1 修复（每处 ≥1 反例）：**
  - `test_feedback_status_closed_set_is_enforced`：`status="GARBAGE"` 拒绝（KILL P15）。
  - `test_feedback_rejection_codes_element_closed_set_is_enforced`：`rejection_codes=("NOPE",)` 拒绝（KILL Q1）。
  - `test_problem_detail_code_closed_set_is_enforced`：`code="NOPE"` 拒绝（KILL Q2）。
  - `test_problem_detail_text_fields_reject_wrong_types`：`field_path=123` / `expected=123` / `observed=[1,2]` 拒绝（KILL P14、Q5）。
  - `test_request_binding_intent_id_and_prompt_hash_are_strict`：`intent_id=123`、`prompt_hash="not-a-hash"` 拒绝（KILL Q6、Q7）。
- **P2 顺手处理（4 条中的 2 条可测项）：**
  - `test_request_binding_base_plan_revision_lower_bound_is_pinned`：`-1` 拒绝、`0` 接受（KILL P4）。
  - `test_retry_budget_counters_lower_bound_is_pinned`：计数 `-1` 拒绝、`0` 接受（KILL P5）。
  - P2-5（`to_json` 的 `str(status)`）、P2-6（`decision_id` 的 `str(ordinal)`）经复核为**等价写法**，非缺口，未改。
- **复验：** 上述 9 个变异（7 P1 + 2 P2）现全部 KILLED；恢复实现后 targeted **52 passed**。`src/` 零改动（sha256 与上一轮一致）。
- 未改 `contracts/` 既有文件；未 merge / push / stash / reset / checkout。
