# H1-A2b 实施日志：PlanningDecision JSON Schema、黄金样例与打包

**基线 HEAD：** `72d440c`（`test(h1-a2a): pin closed-enum decode paths and non-ascii canonical json`）
**工作分支：** `h1-a2b-schema-fixtures`；本片提交信息 = `feat(h1-a2b): planning-decision JSON Schema, golden fixtures and packaging`
**规格依据：** `simpleharness-llm-native-htn-execution-plan-v2.zh-CN.md` §14/§16/§46；`LLM-native-HTN计划V2-裁定补遗-2026-09-18.zh-CN.md` §二（BL-1/BL-5/BL-6）/§三/§四；`H1-V2对照源码冲突检查-2026-09-18.zh-CN.md` §1.2/§5.1。

## 一、范围与白名单

只改白名单内文件：

- `src/agent_orchestrator/contracts/schemas/planning-decision-v1.schema.json`（新增）
- `src/agent_orchestrator/contracts/schemas/__init__.py`（新增，包数据可被 `importlib.resources` 定位）
- `tests/orchestrator/full_target/fixtures/planning_decision_v1/{valid,invalid}/*.json`（新增）
- `tests/orchestrator/full_target/test_planning_decision_json_schema.py`（新增）
- `plans/llm-native-htn/H1/journal-A2b.md`（本文件）

**未改** `pyproject.toml`：Hatchling 的 `[tool.hatch.build.targets.wheel] packages` 已覆盖整个 `src/agent_orchestrator`，`.json` 随包进入 wheel（见下实测），因此无需增加包数据配置。这是本片对「允许最小修改 `pyproject.toml`」的兑现方式——核验后确认不必改。

## 二、Schema 决策

1. Draft 2020-12，`$id = urn:simpleharness:planning-decision:v1`，顶层十字段与 `ENVELOPE_FIELDS` 逐值一致，`additionalProperties:false`。
2. 每个封闭枚举在 `$defs` 声明**一次**：`decisionType / planningRefKind / assumptionRisk / uncertaintySeverity / alternativeDisposition / blockerCode / resumableIf / repairKind / bindExistingGoalMode`，其余位置用 `$ref`。`repair_kind` 在两种载荷里用 `$ref + const` 双约束（枚举封闭 + 子形状判别）。
3. 顶层 `allOf/if-then` 把 `decision_type` 绑定到唯一载荷：八个类型各自 `$ref` 一个载荷 `$defs`；`REPAIR` 用 `oneOf` 指向 `REPLACE_METHOD` / `PROPOSE_SUCCESSOR` 两形状。
4. `$defs` 覆盖：`planningRef / versionedTypeRef / assumption / uncertainty / alternative / replanTrigger` 加十种载荷及内层对象（`blockedItem / evidenceQuestion / humanOption`）。
5. 限额与 §16 及 Python 常量逐值一致：`rationale<=4000`、`subject_key<=256`、`reason_refs<=32`、`assumptions<=16`、`alternatives<=8`、`uncertainties<=16`、`replan_triggers<=16`、`bindings<=64`、`wait_for<=32`、`blockers<=16`、`options<=12`、`arguments<=32`、`questions 1-8`；`planningRef.id<=256`，其余 id/text 上限取 `semantic_base` 的 `MAX_ID/MAX_TEXT/MAX_LIST`。
6. 合同层可判的语义用 JSON Schema 直接表达：`REUSE_ACCEPTED` 的 `resolution_ref` 必须为 `planningRef`、`SHARE_ACTIVE` 必须为 `null`；`method_instance` 引用用 `allOf + kind const` 钉 kind；`blockedItem` 在 `code=OTHER` 时 `required:["detail"]`。
7. 按 §32，`bindings` / `arguments` / `method_proposal` 是**任意域参数映射**，Schema 不给它们 `properties`/`propertyNames`，只限 `maxProperties`（或仅 `type:object`），不扫描系统字段名。顶层 `payload` 同样开放，形状由 `if-then` 收口。

**未在规格覆盖处自定的最小决定（写在此处，供后续核验）：**

- `$defs` 命名采用 camelCase（`planningRef`、`repairReplaceMethodPayload` 等），与 §14 顶层 camelCase 一致；§46 未规定 `$defs` 名。
- `$defs/repairKind` 是该枚举的唯一声明点，载荷内用 `$ref + const`；这是把「枚举逐值一致」与「子形状唯一」同时钉住的最小结构。
- `assumption.required_for` 的 `maxItems` 取 `len(PlanningDecisionType)=9`（Python 侧 `sequence_of(limit=len(PlanningDecisionType))`），规格只给了 9 个决定类型名，未单列该上限。
- `reason_refs` 的重复拒绝由 Python `_unique_refs` 判定（`uniqueItems` 只能比对整对象），Schema 声明 `uniqueItems:true`，反例在 `STRUCTURE_INVALID` 下。

## 三、黄金样例

- `valid/` **11 个**：REFINE、REPAIR/REPLACE_METHOD、REPAIR/PROPOSE_SUCCESSOR、BIND_EXISTING_GOAL（REUSE_ACCEPTED）、BIND_EXISTING_GOAL（SHARE_ACTIVE）、DECLARE_BLOCKED、WAIT、NO_CHANGE、REQUEST_EVIDENCE、REQUEST_HUMAN、PROPOSE_METHOD。
- `invalid/` **39 个**（每个同名 `.expect.json`）：
  - `checked_in = H1-A2b`（合同层，`expected_stage = codec`）：`UNKNOWN_FIELD`、`MALFORMED_DECISION`（缺字段 + 载荷与类型不匹配）、`MODEL_SET_SYSTEM_FIELD`、`DECISION_TYPE_UNKNOWN`、`STRUCTURE_INVALID`（SHARE_ACTIVE 带 resolution_ref、超限额、重复引用）。
  - `checked_in = H1-F`（准入层，`expected_stage = admission`，样例结构合法、当前 `from_json` 可通过）：其余 30 个拒绝码（方法不存在/过期/退役/被拒/不适用/未授权、参数、证据、能力、数据、结构、ORDER/REFINEMENT 环、覆盖、预算、义务、授权、未决操作、运行中未对账、REPAIR/REUSE 不允许、规划上限、INTERNAL）。
  - `checked_in = H1-F`（文本层，`expected_stage = codec`）：`DECISION_BLOCK_MISSING`、`MULTIPLE_DECISIONS`、`MIXED_PROTOCOL_BLOCKS` 三个块扫描器用例（内容为 `{model_reply, note}` 描述子，不是可解码信封）。
- 36 个拒绝码一一对应，无缺无多。

## 四、测试策略（不引入 `jsonschema`）

`tests/orchestrator/full_target/test_planning_decision_json_schema.py`，**无第三方依赖**：

1. `importlib.resources.files("agent_orchestrator.contracts.schemas")` 读到 Schema 且 `$id` 正确；`$schema`/`required`/`properties` 与 `ENVELOPE_FIELDS` 一致。
2. 遍历 Schema，断 `enum`、`maxLength/maxItems/maxProperties/minItems/minLength`、`const`、`pattern`、`minimum` 与 Python 枚举/常量逐值一致；任一侧新增或放宽即红。
3. 极简结构自检：每个 `$ref` 以 `#/$defs/` 开头且目标存在；所有固定形状对象 `additionalProperties:false`（域参数映射除外）；`if-then` 覆盖全部九个 `decision_type`。
4. 每个 valid 样例经 `PlanningDecisionEnvelopeV1.from_json` 通过，`to_json` 往返逐字段一致；载荷键集合等于其绑定 `$defs` 的 `required`。
5. `H1-A2b` 反例被 `from_json` 拒绝；`H1-F` 准入反例当前解码通过（标记待 H1-F 落地）；三个文本层用例只断言其描述子与期望码。

## 五、红到绿

**红：** 新测试先写，首次运行 15 failed / 4 passed / 3 skipped（Schema 与样例目录不存在）。随后补齐 Schema 与样例：

```
$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_json_schema.py -q -p no:cacheprovider
86 passed in 0.08s
```

A2a 回归（未改实现，仍绿）：

```
$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_contract.py tests/orchestrator/full_target/test_planning_decision_envelope.py -q -p no:cacheprovider
151 passed in 0.09s
```

full_target 全量：

```
$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target -q -p no:cacheprovider
3197 passed, 2 skipped in 127.63s (0:02:07)
```

（2 skip = `--run-real-provider` 专项 + `SH_PANDA_PARSER`，与 H0 口径一致。）

## 六、打包与静态检查

`uv build` 不联网时：

```
$ uv build --offline
Successfully built dist/simple_harness_sdk-0.12.2.tar.gz
Successfully built dist/simple_harness_sdk-0.12.2-py3-none-any.whl
```

wheel 内含 `agent_orchestrator/contracts/schemas/planning-decision-v1.schema.json` 与 `__init__.py`。解包到临时目录后以 3.12 解释器验证：

```
$ PYTHONPATH=. uv run --offline --python 3.12 python -c "from importlib import resources; p=resources.files('agent_orchestrator.contracts.schemas').joinpath('planning-decision-v1.schema.json'); print(p.is_file(), p.read_text()[:44])"
True {
  "$schema": "https://json-schema.org/draft/2020-12/schema
```

ruff（本片文件）：

```
$ uv run --offline ruff check src/agent_orchestrator/contracts tests/orchestrator/full_target/test_planning_decision_json_schema.py
All checks passed!
```

`reuse lint` 与全仓 `ruff check src tests` 在本片之前即失败（缺版权信息的既有文件；全仓 ruff 119 处既有告警）。`ruff` 未新增任何一处：本片改动的文件全部 `All checks passed!`。`scripts/check_source_provenance.py` 通过。

**REUSE 覆盖的新问题（写在此处并停在该点）：** `REUSE.toml` 的 `annotations.path` 只覆盖 `tests/**/*.json`、`plans/**/*.json`、`provenance/*.json`，**没有** `src/**/*.json`；因此新增的 `contracts/schemas/planning-decision-v1.schema.json` 与既有的 `planning/htn/seed_methods/{code,appworld}/*.json` 一样落在 `reuse lint` 的缺版权清单里。本片白名单不含 `REUSE.toml`，故**未改**它（照既有 seed_methods JSON 的惯例处理），仅在此记录，交给 H1 收尾或单独的文件头覆盖片统一决定是否给 `src/**/*.json` 加聚合注解。本片新增的 `__init__.py` 与测试文件均带 SPDX 头。

## 七、未做项（属于后继片）

- H1-C：`<planning_decision>` 块扫描/解析器（三个文本层反例的判定方）、`MethodProposal` 内层校验。
- H1-D/H1-E：package v4、prompt v8。
- H1-F：准入层（30 个 `H1-F` 反例的实际拒绝实现 + 新事件）。
- H1-H：编排接线；H1-I：真实模型/变异验收。

**测试计数（供最终回复引用）：** A2b 新增 86 passed（本文件）+ 11 valid / 39 invalid fixtures；full_target 3197 passed / 2 skipped。

## 八、第 1 轮处置（核验“修后可合”，2026-09-19）

核验报告：`plans/llm-native-htn/H1/reviews/核验-H1-A2b-2026-09-19.md`（独立会话，判定“有 P1 无 P0 ⇒ 修后可合”）。本轮修复 P1-A，并为 P1-B/P1-C 补上反向接受性测试；P2 的处置说明见下。**未改** `planning_decisions.py`、`pyproject.toml`，未触其它白名单外文件。

### P1-A：`$defs/blockedItem.detail` 可空性漂移（已修）

- 根因：Python `BlockedItemV1` 的注解是 `detail: str | None`，非 `OTHER` 时允许省略 `detail`，`to_json()` 会回吐 `"detail": null`；而 Schema 把它写成 `"type": "string"`，于是**一个 codec 认为合法的 decision 被 Schema 判为非法**——正是 V2 §14 要“其它语言 Host 与 fixtures 共用同一 wire 形状”的那条用途失效。
- 修复（只改 Schema 一处，不动 codec）：`src/.../planning-decision-v1.schema.json` 的 `$defs/blockedItem/properties/detail/type` 由 `"string"` 改为 `["string", "null"]`，`minLength`/`maxLength` 保持原位不动。
- 回归：`test_every_declared_cap_equals_its_python_constant` 仍能定位 `blockedItem/properties/detail/maxLength`，不被本次改动影响。

### P1-B / P1-C：补“codec 合法输出必须被 Schema 接受”的用例（已补）

- 位置：`tests/orchestrator/full_target/test_planning_decision_json_schema.py`。
- 新增一个**最小 Draft 2020-12 子集校验器**（`_json_kind` / `_type_matches` / `_deref` / `_validate`），只覆盖本 Schema 实际用到的关键字（`type/const/enum/minLength/maxLength/pattern/minimum/minItems/maxItems/uniqueItems/items/required/additionalProperties/maxProperties/anyOf/oneOf/allOf/if-then-else/$ref`），**不引入 `jsonschema` 依赖**。
- 新增反应用例 `test_codec_canonical_output_is_accepted_by_the_schema`：遍历 11 个 `valid/*.json` 加 3 个 codec 合法变体（P1-C 的三个可空字段各一），断言 `PlanningDecisionEnvelopeV1.from_json(...).to_json()` 的输出满足 Schema。这可堵住此前“单向镜像（Schema vs Python 常量）看不见字段 `type` 比 codec 注解更窄”的缺口。
- 先在红态确认（红：`variant-blocked-item-without-detail` 报 `$.payload.blockers[0].detail: type string != null`），再修 Schema 后转绿。

### 变异复验（证明 M7b 方向不再存活）

| 变异 | 目标 | 结果 |
|---|---|---|
| M7b（核验期 SURVIVED）`blockedItem.detail` 收紧回 `"string"` | schema blockedItem.detail | **KILLED**（1 failed / 99 passed） |
| 删 `alternative.method_ref` 的 `null` 分支 | schema alternative.method_ref | **KILLED**（1 failed / 99 passed） |
| 删 `assumption.suggested_predicate_key` 的 `null` 分支 | schema assumption.suggested_predicate_key | **KILLED**（1 failed / 99 passed） |
| 删 `bindGoalPayload.resolution_ref` 的 `null` 分支 | schema bindGoalPayload.resolution_ref | **KILLED**（1 failed / 99 passed） |

所有变异均“先 `cp` Schema 到 `/tmp` → 变异 → 跑测试 → 用副本恢复”，恢复后 `git status --short` 与变异前一致。

### P2 处置

- **P2-1（docstring 反引号不配平）：** 已按原始字节复核，`src/.../schemas/__init__.py:8` 实为 `` ``importlib.resources`` ``（首尾各 2 个 ASCII 反引号，U+0060，共 4 个），与 HEAD 提交、核验副本三方 sha256 完全一致。该条系报告渲染假象，**无需改动**。
- **P2-2（`proposeMethodPayload.method_proposal` 开放对象）：** 与 §32、补遗 §三一致，本片维持不变，留待 H1-C 收紧。
- **P2-3（REUSE 缺 `src/**/*.json` 覆盖）：** 白名单外，维持记录而不改动（见 §六）。

### 本轮测试（尾行原文）

```
$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_json_schema.py -q -p no:cacheprovider
100 passed in 0.09s

$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_json_schema.py tests/orchestrator/full_target/test_planning_decision_contract.py tests/orchestrator/full_target/test_planning_decision_envelope.py -q -p no:cacheprovider
251 passed in 0.13s

$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target -q -p no:cacheprovider
3211 passed, 2 skipped in 124.61s (0:02:04)

$ uv run --offline ruff check src/agent_orchestrator/contracts/schemas/__init__.py tests/orchestrator/full_target/test_planning_decision_json_schema.py
All checks passed!
```

独立复核：第 1 轮曾以临时 `--with jsonschema` 跑过 `Draft202012Validator`，但该 `--with` 需联网取包，离线环境**不可复现**（第 2 轮核验已指出，见其报告「附：对实施日志独立复核一句的厘清」）。此后所有独立复核一律改用仓内两套**离线**校验实现（核验方的 `mini_jsonschema` 与本片的 `_validate`），不再引用需要联网的 `--with`。

## 九、第 2 轮处置（核验“修后可合”，2026-09-19）

核验报告：`plans/llm-native-htn/H1/reviews/核验-H1-A2b-2026-09-19.md` 的「复核 2」。结论：上轮 P1-A/P1-B/P1-C 已全部修复并验证；本轮新发现 **P1-D（含并入的 P1-E）**——无 P0，仍判“修后可合”。本轮**未改 Schema、未改 `planning_decisions.py`、未改 `pyproject.toml`**，只补测试与文档。

### P1-D / P1-E：`uncertainties` / `replan_triggers` 子形状的 `type` 未被任何用例钉死（已修）

- 现象（核验方脚本）：把 Schema 全部 70 个 `type` 关键字逐一翻转为异类，跑 `test_planning_decision_json_schema.py`，**9 个存活的全部**落在
  `$defs/uncertainty`、`$defs/uncertainty/properties/{statement,affects,affects/items}`、
  `$defs/replanTrigger`、`$defs/replanTrigger/properties/{description,referenced_predicates,referenced_predicates/items}`、
  `$defs/assumption/properties/suggested_predicate_key/anyOf/0`；三文件合跑仍 251 passed。
- 根因：11 个 valid 样例的 `uncertainties` 与 `replan_triggers` **恒为空数组**，`suggested_predicate_key` 变体只测了 `null`，因此反应用例只覆盖到“空数组可接受”，看不到元素内部的 `type`；限额镜像只比 `maxLength/maxItems`，不比 `type`。
- 修复（只补测试，不动 Schema——已复核 Schema 的这 9 处 `type` 本就正确）：在 `_codec_canonical_variants()` 增加三个 codec 合法变体：
  - `variant-uncertainty-with-affects`：`uncertainties=[{"statement":"…","severity":"LOW","affects":["obligation:o-1"]}]`
  - `variant-replan-trigger`：`replan_triggers=[{"description":"…","referenced_predicates":["pred.input_present"],"suggested_decision":"REFINE"}]`
  - `variant-assumption-with-predicate`：`assumptions[0].suggested_predicate_key="pred.x"`（非 `null` 字符串）
  这三个变体同时补齐 P1-E 的“形状声明了但零正例”覆盖缺口。反向测试参数由 14 增至 17。

### 变异复验（9 个 P1-D 存活点全部转 KILLED）

| 变异（Schema） | 结果 |
|---|---|
| `$defs/assumption/.../suggested_predicate_key/anyOf/0` string→integer | **KILLED** |
| `$defs/uncertainty` object→string | **KILLED** |
| `$defs/uncertainty/properties/statement` string→integer | **KILLED** |
| `$defs/uncertainty/properties/affects` array→string | **KILLED** |
| `$defs/uncertainty/properties/affects/items` string→integer | **KILLED** |
| `$defs/replanTrigger` object→string | **KILLED** |
| `$defs/replanTrigger/properties/description` string→integer | **KILLED** |
| `$defs/replanTrigger/properties/referenced_predicates` array→string | **KILLED** |
| `$defs/replanTrigger/properties/referenced_predicates/items` string→integer | **KILLED** |

上表每条均由 `test_codec_canonical_output_is_accepted_by_the_schema[variant-uncertainty-with-affects|variant-replan-trigger|variant-assumption-with-predicate]` 击中。另做**全量盲区扫描**：Schema 全部 71 个 `type` 关键字逐一翻转为异类后跑本测试文件，**SURVIVED = 0 / 71**（修复前为 9/70 存活）。所有变异均“备份 `/tmp` → 变异 → 跑测试 → 副本恢复”，恢复后 `git status` 干净，且 Schema 与 HEAD 逐字节一致。

### 证据可复现性提示的处置（核验报告“附”项）

- 第 1 轮日志尾部曾称“用 `jsonschema.Draft202012Validator`（临时 `--with`）复核”。该 `--with` 需联网取包，离线环境不可复现。本轮**已改写该句**，明确“不再引用需要联网的 `--with`，一律改用仓内离线校验实现”，并以后续盲扫（离线）作为证据。

### 本轮测试（尾行原文）

```
$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_json_schema.py -q -p no:cacheprovider
103 passed in 0.09s

$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_json_schema.py tests/orchestrator/full_target/test_planning_decision_contract.py tests/orchestrator/full_target/test_planning_decision_envelope.py -q -p no:cacheprovider
254 passed in 0.13s

$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target -q -p no:cacheprovider
3214 passed, 2 skipped in 127.62s (0:02:07)

$ uv run --offline ruff check src/agent_orchestrator/contracts/schemas/__init__.py tests/orchestrator/full_target/test_planning_decision_json_schema.py
All checks passed!
```

## 十、第 3 轮处置（核验“修后可合”，2026-09-19）

核验报告：`plans/llm-native-htn/H1/reviews/核验-H1-A2b-2026-09-19.md` 的「复核 3」。结论：上轮 P1-D/P1-E 已修复（9 个 `type` 存活全 KILLED、全量 `type` 盲扫 0/71）；本轮新发现 **P1-F**——9 个非载荷子形状 `$defs` 的 `required` / `properties` 未被任何用例钉死。无 P0，仍判“修后可合”。本轮**只改测试与日志，Schema/codec 零改动**。

### P1-F：对象 `$defs` 的 `required` / `properties` 未被钉死（已修）

- 现象（核验方脚本）：把 `planningRef / versionedTypeRef / assumption / uncertainty / alternative / replanTrigger / blockedItem / evidenceQuestion / humanOption` 任一 `required` 清空，或往其 `properties` 加一个未知字段，`tests/orchestrator/full_target` **全量仍绿**（当时 3214 passed, 2 skipped）；且被改坏的 Schema 会接受 codec 明确拒绝的 wire 对象（真漂移，非等价改写）。
- 根因：现有镜像只钉住**顶层信封**（`test_schema_identity_and_envelope_shape`）与 **10 个载荷**（`test_payload_defs_are_complete_and_match_the_codec_required_fields`），对 9 个“引用/摘要/条目”子形状从未把 `required`/`properties` 与 codec 的 `fields_of(required=…, optional=…)` 对表；反向用例只证“Schema 接受 codec 合法对象”，无法发现“Schema 也接受 codec 非法对象”。
- 修复（只补测试，不动 Schema）：
  1. 新增 `test_every_object_def_required_and_properties_match_the_codec`：**用脚本遍历 `$defs` 里所有 `type=="object"` 的条目（当前 19 个，不手抄名单）**；对每个形状在 codec 合法样例池中自动寻找“键集合覆盖全部 `properties`”的 seed 与其 JSON 指针，再以 **codec 自身**判定必填——从 seed 删某键若被 codec 拒绝则该键必填，所得集合必须等于 Schema 的 `required`。
  2. 新增 `test_every_object_def_rejects_missing_required_and_unknown_fields`（对 19 个对象 `$defs` 参数化）：对每个形状，逐个删除必填字段断言 `from_json` 拒绝，并加一个未知字段断言拒绝——全部经 Python 解码路径。
  3. 两个新用例的 `$defs` 名单、seed、必填集合、断言字段**全部由脚本从 Schema + codec 推导**，后续重命名/增删字段无需改名单。

### 变异复验（P1-F 两类存活点全部转 KILLED）

| 变异（Schema） | 范围 | 结果 |
|---|---|---|
| `required -> []` | 全部 19 个对象 `$defs` | **KILLED 19/19（SURVIVED 0）** |
| `properties += __unknown__` | 全部 19 个对象 `$defs` | **KILLED 19/19（SURVIVED 0）** |

其中核验方点名的 9 个非载荷子形状，两类变异各由新增用例命中（每条 2 failed）。所有变异均“备份 `/tmp` → 变异 → 跑测试 → 副本恢复”，恢复后 Schema 与 HEAD 逐字节一致、工作树干净。

### 本轮测试（尾行原文）

```
$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_json_schema.py -q -p no:cacheprovider
123 passed in 0.20s

$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_json_schema.py tests/orchestrator/full_target/test_planning_decision_contract.py tests/orchestrator/full_target/test_planning_decision_envelope.py -q -p no:cacheprovider
275 passed in 0.25s

$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target -q -p no:cacheprovider
3283 passed, 2 skipped in 123.94s (0:02:03)

$ uv run --offline ruff check src/agent_orchestrator/contracts/schemas/__init__.py tests/orchestrator/full_target/test_planning_decision_json_schema.py
All checks passed!
```

### P2 处置

- **P2-1（全量 `type` 盲扫口径）：** 已注意，后续脚本同时枚举字符串型与列表型 `type`。
- **P2-2（`method_proposal` 开放对象）、P2-3（REUSE 缺 `src/**/*.json`）、P2-4（`const: 1` 与 `1.0` 等价）：** 维持前两轮判断，不处理（已记录）。
