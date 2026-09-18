# H1-A2a 实施日志：PlanningDecisionEnvelopeV1、全部载荷与封闭枚举

**日期：** 2026-09-18  
**基线：** `b13e757b74aff9987eebf796ad1b01a3e27b4808`（H1-A1 收官）  
**分支：** `h1-a2a-envelope-types`  
**依据：** `simpleharness-llm-native-htn-execution-plan-v2.zh-CN.md` §13–§32；`LLM-native-HTN计划V2-裁定补遗-2026-09-18.zh-CN.md` §一/§二/§三；`H1-V2对照源码冲突检查-2026-09-18.zh-CN.md` §5.1、§6。

---

## 一、范围（本片交付）

1. `PlanningRefKind` 增加第 16 个成员 `METHOD_INSTANCE = "method_instance"`（BL-1）。
2. `VersionedTypeRefV1 {id, version, content_hash}`：`version` 为 `int >= 1`，`content_hash` 为 64 位小写十六进制，字段名按冲突检查 §5.1 使用 `version`（非 `semantic_revision`）。
3. 补充封闭枚举（全部 `StrEnum`）：`UncertaintySeverity`、`AlternativeDisposition`、`BlockerCode`、`ResumableIf`、`RepairKind`、`BindExistingGoalMode`。`AssumptionRisk`、`PlanningDecisionStatus` 等 A1 已交付的枚举不改。
4. 子结构 dataclass（V2 §20–§23 + 补遗二）：`AssumptionV1`、`PlanningUncertaintyV1`、`AlternativeSummaryV1`、`ReplanTriggerHintV1`。`AssumptionV1.required_for` 与 `ReplanTriggerHintV1.suggested_decision` 取 `PlanningDecisionType` 的值。
5. 载荷 dataclass：
   - REFINE（§24）：`RefineDecision`；
   - REPAIR（§25/§26）：`RepairReplaceMethodDecision`、`RepairProposeSuccessorDecision`；
   - BIND_EXISTING_GOAL（§27）：`BindExistingGoalDecision`；
   - DECLARE_BLOCKED（§28）：`BlockedItemV1` + `DeclareBlockedDecision`；
   - WAIT（§29）：`WaitDecision`；NO_CHANGE（§30）：`NoChangeDecision`；
   - 仅解码三类（补遗二 §三）：`RequestEvidenceDecision`、`RequestHumanDecision`、`ProposeMethodDecision`。
6. `PlanningDecisionEnvelopeV1`（§13 十字段，全部必填）：严格 `from_json`（未知字段拒、缺字段拒、类型错拒、按 `decision_type` 分派唯一载荷类型）、`to_json`，以及 §15 的 `canonical_decision_json` / `canonical_decision_hash`。
7. 限额全部接入：`reason_refs <= 32`、`assumptions <= 16`、`alternatives <= 8`、`uncertainties <= 16`、`replan_triggers <= 16`、`bindings <= 64`、`wait_for <= 32`、`blockers <= 16`、`options <= 12`、`arguments <= 32`；`rationale` 1–4000 字符、`subject_key` 1–256 字符。
8. `reason_refs` 以四元组 `(kind, id, semantic_revision, content_hash)` 去重，重复即拒。
9. 测试：
   - 更新 `tests/orchestrator/full_target/test_planning_decision_contract.py` 的「引用种类」钉子（15 → 16，含 `method_instance`）。
   - 新增 `tests/orchestrator/full_target/test_planning_decision_envelope.py`。

## 二、假设（补遗二未覆盖处按 §5.1/§6 执行，写在此处）

1. `method_instance` 引用的 revision/hash 来源（`plan_revision` / `parameters_digest`）属存储与准入层；合同层只校验其形状（四元组 + 必填 `content_hash`），不校验来源。
2. `VersionedTypeRefV1` 与 `semantic_base.VersionedRef` 线上同形但类型独立；合同层不把 `goal_type_ref` 当 `visible_refs` 四元组匹配。
3. 仅解码三类的内层 `method_proposal`：合同层**不** import `planning` 包，只要求它是 JSON 对象并原样保留；用现有 `MethodProposal` 编解码校验是 H1-C 的事（补遗二 §三）。
4. `REQUEST_EVIDENCE` 的题目数按补遗二 §三理解为 1–8（`{"questions":[...]}`，空列表拒）；`REQUEST_HUMAN` 选项 0–12。两者分别用常量 `MIN_PD_EVIDENCE_QUESTIONS=1` / `MAX_PD_EVIDENCE_QUESTIONS=8` 与 `MAX_PD_HUMAN_OPTIONS=12` 钉死。
5. `bindings` / `arguments` 是任意 JSON 值映射：不扫描键名（§32），仅限条数；`method_instance` 之外不再为 `method_ref` / `replacement_method_ref` / `old_task_ref` 等额外钉死 `kind`，因为 V2/补遗只对实例引用规定了 `kind=method_instance`。
6. `SHARE_ACTIVE` 时 `resolution_ref` 必须为 `null`，`REUSE_ACCEPTED` 时必填；`BlockerCode.OTHER` 时 `detail` 必填（补遗二 §二）。

## 三、测试先行与结果

- **红：** 新测试文件先写；首次运行 `ImportError: cannot import name 'AlternativeDisposition'`（红），确认枚举/载荷/信封均未实现。
- **绿：** 实现后两文件 targeted 全绿。

实现后 targeted 命令与尾行（原样粘贴）：

```
PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_contract.py tests/orchestrator/full_target/test_planning_decision_envelope.py -q -p no:cacheprovider
........................................................................ [ 49%]
........................................................................ [ 99%]
.                                                                        [100%]
145 passed in 0.09s
```

`ruff`（原样粘贴）：

```
PYTHONPATH=src uv run --offline ruff check src/agent_orchestrator/contracts/planning_decisions.py tests/orchestrator/full_target/test_planning_decision_contract.py tests/orchestrator/full_target/test_planning_decision_envelope.py
All checks passed!
```

全量 `full_target` 回归（原样粘贴，取自本轮运行）：

```
PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/ -q -p no:cacheprovider
3105 passed, 2 skipped in 123.71s (0:02:03)
```

## 四、覆盖率要点

- 每个载荷类型一个合法样例往返一致（`test_valid_payload_round_trips` 11 例）+ 每个载荷至少 3 个反例。
- 信封十个字段各一个缺失反例；另有未知字段、类型与载荷不匹配、`schema_version` 非 1、`subject_key` / `rationale` 上下界、未知 `decision_type`。
- 各限额上界 + 1 反例（reason_refs、assumptions、alternatives、uncertainties、replan_triggers、bindings、wait_for、blockers、options、arguments）。
- 重复引用拒（四元组），仅 hash 不同的引用不算重复。
- `SHARE_ACTIVE` 带 `resolution_ref` 拒、`REUSE_ACCEPTED` 缺 `resolution_ref` 拒；`OTHER` 无 detail 拒；`method_instance` 引用缺 `kind` 拒（缺省 kind 直接由四元组必填字段拒）。
- canonical hash 键序无关、数组序相关；`canonical_decision_hash` 为 64 位小写十六进制。
- 所有封闭枚举（`PlanningRefKind`、`UncertaintySeverity`、`AlternativeDisposition`、`BlockerCode`、`ResumableIf`、`RepairKind`、`BindExistingGoalMode`）以字面量清单逐个钉死。

## 五、未做项（属 H1-A2b，不在本片）

1. JSON Schema 文件 `src/agent_orchestrator/contracts/schemas/planning-decision-v1.schema.json`（补遗二 §四 明确含 `$defs` 全量正文）。
2. 黄金样例目录（valid ≥ 11 / invalid 每拒绝码 ≥ 1，含 `expected_stage / expected_code` 标注）。
3. `contracts/schemas/*.json` 作为包数据进安装包的打包改动。
4. `test_planning_decision_json_schema.py`（`importlib.resources` 读取 + `$id` / `required` / `enum` 与 Python 枚举逐值一致）。
5. 三型仅解码的准入层行为（`DECISION_NOT_ENABLED_IN_PHASE`）与 `PlanningDecisionEvaluated` 事件接线（H1-F/H1-H）。

## 六、允许改动文件

```
src/agent_orchestrator/contracts/planning_decisions.py
tests/orchestrator/full_target/test_planning_decision_contract.py
tests/orchestrator/full_target/test_planning_decision_envelope.py   (新增)
plans/llm-native-htn/H1/journal-A2a.md                               (新增)
```

未改 `contracts/` 下任何其它文件；未 push / stash / checkout / reset；未读密钥。

---

## 七、第 1 轮处置（核验：修后可合）

**依据：** `plans/llm-native-htn/H1/reviews/核验-H1-A2a-2026-09-18.md`（独立核验结论：无 P0，1 条 P1 测试缺口）。

**P1（canonical 值未钉死）：** 核验用 12 个变异测试本片实现；其中 M7（`canonical_decision_json` 去掉 `sort_keys`）与 M12（`canonical_decision_hash` 改成 `sha256(json + "x")`）**存活**——因为原测试只做了「两个自身结果相等 / 不相等」与 `!= ""` 这类弱断言，没有任何与独立基准的**等值**断言，`§15` 明确定义的输出物没有被真正钉住。

**修复（只改测试，不动实现）：** 在 `tests/orchestrator/full_target/test_planning_decision_envelope.py` 增加两个测试，全部以字面量/独立实现为准：

- `test_canonical_decision_json_matches_an_independent_canonicalisation`：用一个固定的 `CANONICAL_SAMPLE`，断言 `canonical_decision_json(envelope)` 等于**独立实现**（标准库 `json.dumps(..., sort_keys=True, separators=(",", ":"))`）的结果。M7 下必红。
- `test_canonical_decision_json_and_hash_are_pinned_literals`：断言 `canonical_decision_json` 等于逐字节钉死的字面量 `CANONICAL_SAMPLE_JSON`，`canonical_decision_hash` 等于字面量 `CANONICAL_SAMPLE_HASH`（`22111ad0…9af72`），并用 `hashlib.sha256(CANONICAL_SAMPLE_JSON)` 独立复算。M12 下必红。
- 顺带把 P2-2 的弱断言 `canonical_decision_hash(e1) != ""` 换成 `len(...) == 64`。

**变异复验（先红后绿）：** 在上述测试存在的前提下重跑 M7、M12，二者均 **KILLED**（M7：2 failed, 93 passed；M12：1 failed, 94 passed）；恢复实现后 targeted 全绿（见下）。实现文件 sha256 与处置前一致（`6d2bcc25…ed8b40`）。

**测试（原样粘贴）：**

```
$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_contract.py tests/orchestrator/full_target/test_planning_decision_envelope.py -q -p no:cacheprovider
........................................................................ [ 48%]
........................................................................ [ 97%]
...                                                                      [100%]
147 passed in 0.09s
```

**未做项：** 与本片一致（H1-A2b 的 Schema/黄金样例/打包；H1-F/H1-H 的准入与事件接线）。

---

## 八、第 2 轮处置（核验：修后可合）

**依据：** `plans/llm-native-htn/H1/reviews/核验-H1-A2a-2026-09-18.md`（复核 2；无 P0，新增 3 条 P1，均为测试缺口）。

**P1-2（新，`UncertaintySeverity` 解码路径未钉）：** 原测试只断言枚举类「有哪些成员」，没有经过 `from_json` 的解码路径。变异 N8（把 `uncertainties` 的 `severity` 校验改成直接赋值）下，本片 147 条与全量 3107 条全部放行，`{"statement":"u","severity":"BOGUS","affects":[]}` 被接受。

**P1-3（新，`AlternativeDisposition` 解码路径未钉）：** 同 P1-2 形态。变异 N9（把 `enum_of(...)` 换成直接赋值）下，`{"method_ref":null,"label":"l","disposition":"BOGUS","reason":"r"}` 被接受。

**P1-4（新，canonical「非 ASCII 不转义」未被钉住）：** 第 1 轮修复用的固定样例 `rationale` 是纯 ASCII，因此 `ensure_ascii=True` 的变异（N2）不被任何断言触及。规格 §15 要求 `canonical_decision_json == canonical_json(decision.to_json())`，而共享的 `simple_harness.contracts.canonical_json` 明确 `ensure_ascii=False`；V2 §13 的示例 `rationale` 本身就是中文。

**修复（只改测试，不动实现）：** 在 `tests/orchestrator/full_target/test_planning_decision_envelope.py` 内：

- 新增 4 个解码路径反例（`UncertaintySeverity`、`AlternativeDisposition`、`AssumptionRisk`、`ReplanTriggerHintV1.suggested_decision`）：构造合法信封 + 未知枚举字面量，断言 `from_json` 抛 `ContractError`。
- 把固定样例 `CANONICAL_SAMPLE` 的 `rationale` 改成 V2 §13 示例的中文 `选择已注册且当前可适用的方法。`，同步更新字面量 `CANONICAL_SAMPLE_JSON` 与 `CANONICAL_SAMPLE_HASH`（`18bb6817…5751f`）。
- `test_canonical_decision_json_matches_an_independent_canonicalisation` 增加 `canonical_decision_json(envelope) == canonical_json(envelope.to_json())`（共享合同层实现）断言，把「非 ASCII 不转义」钉死。

**变异复验（先红后绿）：**

| 变异 | 结果 |
|---|---|
| N2 `canonical_decision_json` 改用 `ensure_ascii=True` | **KILLED**（2 failed, 101 passed；`test_canonical_decision_json_matches_an_independent_canonicalisation`、`test_canonical_decision_json_and_hash_are_pinned_literals`） |
| N8 `UncertaintySeverity` 去掉 `enum_of` | **KILLED**（1 failed, 102 passed；`test_uncertainty_severity_decode_path_is_closed`） |
| N9 `AlternativeDisposition` 去掉 `enum_of` | **KILLED**（1 failed, 102 passed；`test_alternative_disposition_decode_path_is_closed`） |

`AssumptionRisk` 去掉 `enum_of` 的等价变异同样被 `test_assumption_risk_decode_path_is_closed` KILLED。恢复实现后实现文件 sha256 仍为 `6d2bcc25…ed8b40`（未改实现）。

**测试（原样粘贴）：**

```
$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_contract.py tests/orchestrator/full_target/test_planning_decision_envelope.py -q -p no:cacheprovider
........................................................................ [ 47%]
........................................................................ [ 95%]
.......                                                                  [100%]
151 passed in 0.09s
```

**P2：** P2-4（WAIT / DECLARE_BLOCKED 两个限额反例落在同一参数化用例，失败信息不够可区分）本轮未改；P2-1（`decision_payload_hash` 命名 vs `canonical_decision_hash`）属接线片命名统一，留待 H1-C/H1-H。

**未做项：** 与本片一致（H1-A2b 的 Schema/黄金样例/打包；H1-F/H1-H 的准入与事件接线）。

---

## 九、第 3 轮处置（核验：修后可合）

**依据：** `plans/llm-native-htn/H1/reviews/核验-H1-A2a-2026-09-18.md`「复核 3」（无 P0，新增 1 条 P1 测试缺口）。第 2 轮处置交付的 `72d440c` 已修复 P1-2/P1-3/P1-4；本轮处理复核 3 新发现的 P1-5。

**P1-5（新，`AssumptionV1.required_for` 解码路径未钉）：** 补遗二 §2 规定假设的 `required_for` 取九个决定类型名，即解码时必须逐项校验为 `PlanningDecisionType`。原测试只喂合法值 `["REFINE"]`，从不喂未知值；变异 Q3（把 `sequence_of(..., _decision_type, ...)` 换成 `tuple(self.required_for)`）下，本片 151 条与全量 3111 条全部放行，`required_for=["NOT_A_DECISION_TYPE"]` 被接受并原样往返。

**修复（只改测试，不动实现）：** 在 `tests/orchestrator/full_target/test_planning_decision_envelope.py` 增加：

- `test_assumption_required_for_decode_path_is_closed`：合法信封 + `required_for=["NOT_A_DECISION_TYPE"]`，断言 `from_json` 抛 `ContractError`。

**变异复验（先红后绿）：** 在上述测试存在的前提下重跑 Q3，**KILLED**（1 failed, 99 passed；`test_assumption_required_for_decode_path_is_closed`）；恢复实现后全绿。实现文件 sha256 与处置前一致（`6d2bcc25…ed8b40`，未改实现）。

**测试（原样粘贴）：**

```
$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_contract.py tests/orchestrator/full_target/test_planning_decision_envelope.py -q -p no:cacheprovider
........................................................................ [ 47%]
........................................................................ [ 95%]
.......                                                                  [100%]
152 passed in 0.09s
```

**未做项：** 与本片一致（H1-A2b 的 Schema/黄金样例/打包；H1-F/H1-H 的准入与事件接线）。
