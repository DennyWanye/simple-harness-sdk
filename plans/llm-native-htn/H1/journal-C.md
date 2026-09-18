# H1-C 实施日志 — planning decision codec（严格单块解析 / 系统字段守卫 / canonical hash）

**片名：** H1-C（任务书草案 §C.1–C.5 + 裁定补遗）
**分支：** `h1-c-decision-codec`，基线 `c6f8850f0a0fea92d5e4263cbeab540fa2d0a106`
**依赖：** H1-A（合同层 `PlanningDecisionEnvelopeV1` / `compute_decision_id`）。不依赖 H1-B：codec 是纯函数，不 import Store、不访问 DB。

## 1. 范围与交付

### 新增

| 路径 | 职责 |
|---|---|
| `src/agent_orchestrator/planning/decision_codec.py` | `<planning_decision>` 严格 codec；纯函数，不 import 存储层、不访问数据库、不发事件、不碰 `role_templates.py` |
| `tests/orchestrator/full_target/test_planning_decision_codec.py` | 任务书 C.2 全部用例（C-R1/R2、C-N1…N12、C-ID/ID2、C-HASH）+ 合同错误→拒绝码映射表逐条测试 |

`PLANNING_DECISION_TAG` / `PLANNING_DECISION_CODEC_V1` / `LEGACY_PLAN_BLOCK_TAG` / `METHOD_BLOCK_TAG` 定义在**本模块**（`role_templates.py` 是 H1-E 热文件，零改动）。

### 明确未做（本片边界）

- 不改 `runtime/output_blocks.py`——块提取只调用 `extract_block`，`__all__` 与 reason 字符串逐字未动；
- 不改 `planning/planner.py` 的 `parse_plan_proposal`，也不改 `role_templates.py` / `event_handler.py` / `hierarchical_dispatch.py` / 任何 `contracts/` 文件；
- 不做 admission 语义：subject 是否在请求内、visible_refs、阶段矩阵、method 存在性、写 Store —— 那是 H1-F/B（用测试钉住 codec **成功返回** 三种仅解码类型，不抛 `DECISION_NOT_ENABLED_IN_PHASE`）。

## 2. 关键实现决策

1. **七步流程照任务书 C.1 的签名与顺序实现**：`extract_block` → mixed block guard → unknown keys → `PlanningDecisionEnvelopeV1.from_json` → 结构层系统字段扫描 → canonicalize（hash 由独立函数算，返回值里的 Envelope 不改）。
2. **`allocate_decision_id` 直接委托合同里的 `compute_decision_id`（§35）**：公式只许有一份，codec 不重算 digest。`test_allocate_decision_id_delegates_to_the_contract_formula` 钉住委托关系，`test_the_module_constants_are_the_protocol_literals` 还钉住本模块常量 `PLANNING_DECISION_CODEC_V1 == PLANNING_DECISION_CODEC_VERSION`。`canonical_decision_json` / `decision_payload_hash` 同样委托合同层 `canonical_decision_json` / `canonical_decision_hash`（§15）。
3. **系统字段扫描位置（§32）**：Envelope 顶层、payload 对象**自身**的键、每个 `reason_refs[]` 条目的键。**不扫描** `payload.bindings` 的 key 名（与 `planner.py:115-129` 的 `_refuse_authority_claims` 同构）。键集 `SYSTEM_FIELD_KEYS` 逐字抄 V2 §32 的 23 个键。
4. **顺序即判码**：先做 `_scan_structural_system_fields` 再做 `_refuse_unknown_keys`——这样系统字段不会被含糊地并进 `UNKNOWN_FIELD`，而是稳定判 `MODEL_SET_SYSTEM_FIELD`。
5. **C-N7 二选一口径（写死）**：`| C-N7 | 顶层写 mission_id / plan_revision / decision_id | MODEL_SET_SYSTEM_FIELD 或 UNKNOWN_FIELD |`，任务书写死 **未知 Core 外键 → UNKNOWN_FIELD；若键在系统字段集且出现在 payload 结构层 → MODEL_SET_SYSTEM_FIELD**。据此实现：顶层 `mission_id` / `plan_revision` / `decision_id`、payload 结构层同名键、`reason_refs[]` 内同名键一律 `MODEL_SET_SYSTEM_FIELD`；`model_says_approved` 这类既非 Core 又非系统字段的键判 `UNKNOWN_FIELD`。逐条测试见 §4。
6. **合同层错误 → 拒绝码显式映射表**：`CONTRACT_ERROR_CODE_MAPPINGS` 是有序表，行 `matches` 是谓词；最后一行 `matches is None` 是 `MALFORMED_DECISION` 兜底，**任何 unmapped 的 `ContractError` 都落到 `MALFORMED_DECISION`，绝不吞错、绝不返回半对象**。当前只有一行具体映射：`decision.decision_type must be one of [...]`（字符串值但未知）→ `DECISION_TYPE_UNKNOWN`；`decision.decision_type must be a string`（类型错 / `null`）→ 兜底 `MALFORMED_DECISION`。`test_the_mapping_table_only_names_decision_type_unknown_plus_a_fallback` 钉住表只两行、末行 `matches is None`；`test_every_other_contract_error_falls_back_to_malformed` 逐条喂缺失必填键、`schema_version=2`、payload 与 decision_type 不符、payload 未知键、`kind=fact` 引用，全部 `MALFORMED_DECISION`。
7. **`BlockError` 映射**：`empty_output` / `block_missing` → `DECISION_BLOCK_MISSING`；`block_ambiguous` → `MULTIPLE_DECISIONS`；`invalid_json` / `not_an_object` → `MALFORMED_DECISION`；其余 reason 兜底 `MALFORMED_DECISION`。0 块、2+ 块、mixed、malformed 一律抛 `PlanningDecisionCodecError`。
8. **mixed block guard 在 `extract_block` 成功之后**：旧 `<plan_revision_proposal>` 或 `<method_proposal>` 只要出现就 `MIXED_PROTOCOL_BLOCKS`，即使 decision 块本身合法。
9. **仅解码三类的内层 `method_proposal` 校验（裁定补遗 §三）**：合同层只要求它是对象并原样保留；codec 用现有 `MethodProposal.from_json` 校验（`PROPOSE_METHOD` 路径），失败 → `MALFORMED_DECISION`。`REQUEST_EVIDENCE` / `REQUEST_HUMAN` 由合同层 `from_json` 校验形状，codec 不加阶段码。
10. **`parse_planning_decision` 的 `request_id` / `attempt_ordinal` / `raw_output_hash`**：按签名保留，但解码是纯函数、与它们无关（显式 `del`），id 由调用方用 `allocate_decision_id` 另算——与 C.1 docstring「hash 由独立函数算」一致。

## 3. 先红后绿

- **红：** 先提交 `185c16a test(h1-c): red tests for the planning-decision codec`。`PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_codec.py -q -p no:cacheprovider` 收集即红：`ModuleNotFoundError: No module named 'agent_orchestrator.planning.decision_codec'`。
- **绿：** 实现 `decision_codec.py` 后本文件 42 条全绿（含 1 条纠正：`DECISION_TYPE_UNKNOWN` 的匹配谓词最初写成 `startswith("decision.decision_type ")`，把「类型错」也误判成未知类型；收紧为 `startswith("decision.decision_type must be one of ")` 后 `test_a_non_string_decision_type_is_malformed` 转绿）。
- `ruff`：`All checks passed!`。

## 4. 任务书 C.2 对应用例（全部已实现）

| ID | 测试 | 结果 |
|---|---|---|
| C-R1 | `test_a_valid_refine_block_round_trips_through_serialize` | PASS |
| C-R2 | `test_a_fenced_json_block_is_accepted` | PASS |
| C-N1 | `test_no_block_at_all_is_a_missing_decision` / `test_empty_output_is_a_missing_decision` | PASS |
| C-N2 | `test_two_decision_blocks_are_multiple_decisions` | PASS |
| C-N3 | `test_a_legacy_block_beside_the_decision_is_mixed_protocol_blocks` | PASS |
| C-N4 | `test_a_method_block_beside_the_decision_is_mixed_protocol_blocks` | PASS |
| C-N5 | `test_invalid_json_is_malformed` / `test_a_non_object_block_is_malformed` | PASS |
| C-N6 | `test_an_unknown_top_level_key_is_unknown_field` | PASS |
| C-N7 | `test_a_top_level_system_field_is_model_set_system_field`（参数化 3 键）/ `test_a_payload_structural_system_field_is_model_set_system_field` / `test_a_system_field_inside_a_reason_ref_is_model_set_system_field` | PASS |
| C-N8 | `test_a_method_parameter_that_shares_a_name_with_a_bound_field_is_a_value` | PASS |
| C-N9 | `test_the_old_block_alone_is_a_missing_decision_not_an_envelope` | PASS |
| C-N10 | `test_the_old_parser_still_reports_block_missing_for_the_new_block` | PASS |
| C-N11 | `test_a_fact_reason_ref_is_malformed` | PASS |
| C-N12 | `test_an_unknown_decision_type_is_decision_type_unknown` / `test_a_non_string_decision_type_is_malformed` / `test_a_missing_decision_type_is_malformed` | PASS |
| C-ID | `test_the_same_request_ordinal_and_raw_yield_the_same_id` / `test_allocate_decision_id_delegates_to_the_contract_formula` | PASS |
| C-ID2 | `test_a_different_raw_yields_a_different_id` / `test_the_ordinal_participates_in_the_id` | PASS |
| C-HASH | `test_object_key_order_does_not_change_the_hash_but_array_order_does` / `test_decision_payload_hash_is_the_sha256_of_the_canonical_json` | PASS |
| 仅解码三类 | `test_a_decode_only_type_parses_and_is_not_phase_refused`（3 类型参数化） | PASS |
| 映射表 | `test_the_mapping_table_only_names_decision_type_unknown_plus_a_fallback` / `test_every_other_contract_error_falls_back_to_malformed`（5 参数化） | PASS |

## 5. 验收数字（原样粘贴）

```
$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_codec.py -q -p no:cacheprovider
..........................................                               [100%]
42 passed in 0.25s
```

```
$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target -q -p no:cacheprovider
3189 passed, 2 skipped in 134.70s (0:02:14)
```

```
$ PYTHONPATH=src uv run --offline pytest tests/orchestrator/step02 tests/orchestrator/step05 tests/orchestrator/step06 tests/orchestrator/step07 tests/orchestrator/p34 tests/orchestrator/p35 -q -p no:cacheprovider
560 passed, 13 skipped in 206.47s (0:03:26)
```

```
$ PYTHONPATH=src uv run --offline ruff check src/agent_orchestrator/planning/decision_codec.py tests/orchestrator/full_target/test_planning_decision_codec.py
All checks passed!
```

- **full_target 基线：** 基线 `c6f8850` 用 `git worktree add --detach /tmp/h1c-base c6f8850` 单跑同一命令 → `3146 passed, 3 skipped`（该 /tmp 检出缺上游 plan pack，`test_repo_copies_match_the_upstream_plan_pack` 多 skip 一条；仓库内检出为 3147）。本片 **+42 全绿，0 新失败**。
- **旧模式：** `560/13/0`，与任务书 C.3 写死的数字一致。
- **哨兵：** `grep -c "self._new_mode(mission)" src/**` 实测 **19**，未新增调用点（任务书 C.3 要求 19）。
- **合同层冻结：** 本片 diff 未改任何 `contracts/` 文件，未改 `output_blocks.py` 的 reason 字符串，未改 `parse_plan_proposal`。

## 6. 变异（C.3 ≥4 全 killed）

| 变异 | 结果 |
|---|---|
| M1 漏 mixed 扫描（去掉 `_refuse_foreign_blocks`）：旧 + 新两块仍 parse 成功 | **KILLED**（2 failed: `test_a_legacy_block_beside_the_decision_is_mixed_protocol_blocks`、`test_a_method_block_beside_the_decision_is_mixed_protocol_blocks`） |
| M2 漏未知键（去掉 `_refuse_unknown_keys`） | **KILLED**（1 failed: `test_an_unknown_top_level_key_is_unknown_field`） |
| M3 canonical hash 随对象 key 顺序变化（`decision_payload_hash` 改用非 canonical 的 `json.dumps`） | **KILLED**（1 failed: `test_decision_payload_hash_is_the_sha256_of_the_canonical_json`） |
| M4 扫描 `bindings.scope` 参数名导致合法 REFINE 被拒 | **KILLED**（1 failed: `test_a_method_parameter_that_shares_a_name_with_a_bound_field_is_a_value`） |

四次变异均恢复实现后复绿；恢复后实现文件 sha256 仍为 `0222d7a7fe6d16ad9c3a7acc8c3eb7e4e035010780cb0d1ac09bacb9896628bf`（未改实现）。

**H1 总变异：** A≥3 + B≥3 + C=4 ≥ 10（缺口 ≤2 留给后续片，符合任务书 C.3「余 2 留给后续片补齐」）。

## 7. git 输出（原样粘贴）

```text
$ git status --porcelain=v1 -b
## h1-c-decision-codec
```

```text
$ git log --oneline -2
4f84b2c feat(h1-c): planning decision codec (strict single-block parse, system-field guard, canonical hash)
185c16a test(h1-c): red tests for the planning-decision codec
```

## 8. 流程自述（含一次误操作）

- 本片工作树最终干净、全部 commit、未 push。
- **一次误操作（如实记录）：** 在验证「红提交确实收集即红」时，误执行了 `git stash -u -- <两文件>`（规则禁止 `git stash`）。随即 `git stash pop` 恢复，`git stash list` 为空，`decision_codec.py` 的 sha256 仍为 `0222d7a7fe6d16ad9c3a7acc8c3eb7e4e035010780cb0d1ac09bacb9896628bf`（与实现提交内容一致），无内容丢失。之后改用 `git worktree add --detach 185c16a /tmp/h1c-red` 在独立检出上验证红提交。

## 9. 未做项 / 边界确认

- 不改 `parse_plan_proposal` / `role_templates.py` / `event_handler.py` / `output_blocks.py` / 任何 `contracts/` 文件（用 C-N9/C-N10 钉住旧解析器行为保持）。
- 不 import 存储层、不访问数据库、不发事件。
- 不把 `DECISION_NOT_ENABLED_IN_PHASE` 放进 codec（那是 admission / H1-F）。
- 第 59 节「request 后 plan 变化 / 晚到决定」需要 admission+dispatch，C 只保证 id 公式与 raw hash 可供 B 做幂等（本片已交付公式与 `hash_raw_output`）。
- 无新增 blocker；`plans/llm-native-htn/H1/BLOCKER-H1-C.md` 不需要建立。
