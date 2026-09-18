# H1-B 实施日志 — migration 19 三张表 + PlanningDecisionStore

**片名：** H1-B（migration 19 三张表 + Store 读写 + 幂等）
**分支：** `h1-b-decision-store`，基线 `b13e757`（H1-A1 已合入）
**范围：** 任务书草案 §B.1–B.5 + 裁定补遗五；不改 `commit_service` / `MissionSpec` / 派发路径。

---

## 1. 范围与产出

### 新增

| 路径 | 职责 |
|---|---|
| `src/agent_orchestrator/storage/planning_decision_schema.py` | migration 19 的 `DDL`（一条迁移三张表，§8.2/§36 逐字）与 `TABLES` |
| `src/agent_orchestrator/storage/planning_decision_store.py` | `PlanningDecisionStore`：组合现有 `Store`，只在 `Store.transaction()` 内写 |
| `tests/orchestrator/full_target/test_planning_decision_store.py` | B.2 表 S1–S10 + 状态前进/终态吸收 + 升级路径 + 旧协议不写新表 |
| `tests/orchestrator/full_target/test_planning_decision_request_binding.py` | B.2 表 R1–R3 + 整条 binding 往返 |

### 修改（仅追加）

| 路径 | 改什么 |
|---|---|
| `src/agent_orchestrator/storage/schema.py` | **只追加** `from .planning_decision_schema import DDL as DDL_V19`、`Migration(19, "orchestrator-planning-decision-v1", DDL_V19)`、`__all__` 中 `"DDL_V19"`。1–18 的 ddl / name / 校验和一字未动。 |

实现签名与草案 §B.1「必须实现的 Store 签名」完全一致（`bind_mission_protocol` /
`get_mission_protocol` / `insert_planning_request` / `get_planning_request` /
`record_planning_decision` / `get_planning_decision` /
`get_planning_decision_by_attempt`）。

### 明确未做（本片边界）

- 不改 `MissionSpec`、不接线 `create_mission`、不 `append_event`、不进 `policy_snapshot`；
- 不发任何 `PlanningDecision*` 事件（`grep` 本片 diff：零 `append_event` 新增）；
- 不碰 `commit_service` / `hierarchical_dispatch` / `event_handler` / `SNAPSHOT_FIELDS`；
- 不在本片实现 admission / codec / adapter / 派发（后续片）；
- 不读环境变量猜协议模式。

---

## 2. 假设清单（草案 §0.3：规格未覆盖处的自我裁定，均向后兼容）

1. **§36 状态顺序即前进方向。** V2 §36 只列出八个值，没有画箭头。本片把「列表顺序」当作 rank：`UNREADABLE < DECODED < REJECTED < ADMITTED < COMPILED < COMMIT_REJECTED < COMMITTED < NO_STATE_CHANGE`；搬到更大的 rank 是前进，搬到更小或相同的 rank 是回退（拒绝）。**不许从终态回退**取 `TERMINAL_STATUSES = {UNREADABLE, REJECTED, COMMIT_REJECTED, COMMITTED, NO_STATE_CHANGE}`，终态吸收（此后任何变更都 `StoreConflict`）。若指挥者另有箭头图，仅需替换 `STATUS_ORDER` / `TERMINAL_STATUSES` 两个常量。
2. **同 status 重放是幂等返回。** 同 `(request_id, attempt_ordinal, raw_output_hash)` + 同 `decision_id` + 同 status → 返回已有行，不再写。上游可在已知更晚的 status 时补写列（`COALESCE`，只填不抹）。
3. **`canonical_json` 原样持久化。** §15 canonical 化属 codec；`canonical_hash` 是这些字节的摘要，Store 若重排会让 hash 描述库中不存在的东西。Store 只拒「不是 JSON」的列。
4. **`rejection_codes_json` / `detail_json` 用 `canonical_json`。** 草案 §B.1 规则第 7 条：「JSON 列用 `canonical_json`」。读出时解码回 `list` / `dict`（返回 `dict[str, Any]`）。
5. **`mission_planning_protocols` 幂等键 = 四元组。** 同 mission 的 `(protocol_version, package_version, prompt_version, binding_hash)` 全同 → 幂等返回；任一不同 → `StoreConflict`（「创建后不可切协议」）。
6. **`planning_requests` 幂等键 = `request_id` + canonical 全等。** 同 id、同内容 → 返回已有行；同 id、异内容 → `StoreConflict`。
7. **`binding_hash` 校验为 64 位小写 hex。** 沿用 `semantic_base.hash_hex`，与 A.1 对 `package_hash`/`prompt_hash` 的要求一致。
8. **`detail_json` 空对象允许。** 只有 `document` 必须是 JSON 对象；`detail` 无必填键约束（本片无 admission 语义）。
9. **外键默认开启。** `Store.open` 已 `PRAGMA foreign_keys = ON`，`planning_decisions.request_id` 的外键因此真生效；未登记 request 的 decision 必须 `StoreConflict`（不要裸 `IntegrityError`）。

---

## 3. 红 → 绿

- **红：** 先写两个测试文件。首跑（`test_planning_decision_store.py`）收集即红：
  `ModuleNotFoundError: No module named 'agent_orchestrator.storage.planning_decision_store'`。
- **绿：** 实现 `planning_decision_schema.py` / `planning_decision_store.py` 并追加 migration 19 后，
  两个新文件 **35 passed**（下表给出实现后实测尾行）。

### 实测（命令与尾行原样；数字为处置轮复跑后的最终值 `6ec2cff`）

```text
PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_store.py tests/orchestrator/full_target/test_planning_decision_request_binding.py -q -p no:cacheprovider
35 passed in 0.40s
```

```text
PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_htn_store.py tests/orchestrator/full_target/test_htn_end_to_end.py -q -p no:cacheprovider
291 passed in 12.20s
```

```text
PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target -q -p no:cacheprovider
3047 passed, 2 skipped in 126.92s (0:02:06)
```

```text
PYTHONPATH=src uv run --offline pytest tests/orchestrator/step02 tests/orchestrator/step05 tests/orchestrator/step06 tests/orchestrator/step07 tests/orchestrator/p34 tests/orchestrator/p35 -q -p no:cacheprovider
560 passed, 13 skipped in 201.74s (0:03:21)
```

```text
PYTHONPATH=src uv run --offline ruff check src/agent_orchestrator/storage/ tests/orchestrator/full_target/test_planning_decision_store.py tests/orchestrator/full_target/test_planning_decision_request_binding.py tests/orchestrator/full_target/test_htn_store.py tests/orchestrator/full_target/test_htn_end_to_end.py
All checks passed!
```

哨兵 `self._new_mode(mission)` 计数实测仍 **19**（未新增调用点）。

### 已知非本片红（基线已存在，未修）

`tests/orchestrator/p33` 的 8 个用例与 `tests/orchestrator/gap_phase1` 的若干 collection error
在本机与基线 `b13e757` 上同样失败（tokenizer 未 pin / sandbox 进程清单不可用 / kernel AST 基线 hash 与当前 Python 版本不符），
与本片改动无因果关系：我在同一容器把基线 `b13e757` 用 `git archive` 解到 `/tmp` 后单跑，得到同一失败集合。

---

## 4. 钉死 18 的既有测试清单（逐条点名，均为「预期内 schema head 前进」）

| 文件 | 行（基线 b13e757） | 原断言 | 改为 | 为什么不是行为回归 |
|---|---|---|---|---|
| `tests/orchestrator/full_target/test_htn_store.py` | 581–584（`test_migration_eighteen_is_the_new_head`） | `SCHEMA_VERSION == 18`；`SCHEMA_NAME == "…witness-subject"`；`MIGRATIONS[-1].ddl is validity_subject_schema.DDL` | 函数改名 `test_migration_nineteen_is_the_new_head`；`== 19`；`"orchestrator-planning-decision-v1"`；`is planning_decision_schema.DDL` | 新增 head，18 不动 |
| 同上 | 606（`test_the_fifteen_older_migrations_keep_their_checksums`） | `len(MIGRATIONS) == 18` | `== 19` | 冻结清单本身仍是 1–15。|
| 同上 | 635（`test_migration_eighteen_is_pinned_and_creates_no_table`） | `MIGRATIONS[-1].checksum == MIGRATION_18_CHECKSUM` | `MIGRATIONS[17].checksum == MIGRATION_18_CHECKSUM` | 18 现在不是 head，**字节仍逐字不变** |
| 同上 | 757–758（`test_migration_eighteen_upgrades_an_existing_library_in_place`） | `applied[-1] == (18, SCHEMA_NAME, MIGRATION_18_CHECKSUM)`；backup `pre-schema-18` | `applied[-2] == (18, "…witness-subject", MIGRATION_18_CHECKSUM)`；`applied[-1] == (19, SCHEMA_NAME, …)`；backup `pre-schema-19` | 升级 drill 现在落到 19，18 仍被验证 |
| 同上 | 1045–1052（`test_upgrading_a_copy_of_a_v15_library_keeps_every_old_row`） | `list(range(1, 19))`；`applied[-1]`=18/17/16 三行 | `list(range(1, 20))`；`applied[-1]`=19，`applied[-2]`=18，`applied[-3]`=17，`applied[-4]`=16 | 多一行 19；16/17/18 的 checksum 断言逐条保留 |
| 同上 | 1069（`test_the_upgrade_writes_a_backup_of_the_old_library`） | backup `v15.db.pre-schema-18.backup` | `…pre-schema-19.backup` | 备份名跟随最高待应用迁移 |
| 同上 | import 块 | —— | 增加 `planning_decision_schema` | 新 head 的引用 |
| `tests/orchestrator/full_target/test_htn_end_to_end.py` | 844–846（`test_migration_seventeen_is_additive_and_eighteen_is_the_head`） | `SCHEMA_VERSION == 18` | 函数改名 `…eighteen_is_still_present`；`== 19`；并追加 `MIGRATIONS[17].version == 18`、`MIGRATIONS[18].version == 19` | 17/18 仍在；只是 head 前进 |

> 除上表外，无其它既有测试因版本号改动而红：`git grep 'SCHEMA_VERSION == 18'`、
> `git grep 'MIGRATIONS) == 18'`、`git grep 'range(1, 19)'`、`git grep 'pre-schema-18'`
> 在 `tests/` 下只剩上表两文件命中（另加本片新增文件，其 18 断言为「18 仍是被钉住的历史行」）。

---

## 5. 变异（B.3 要求 ≥3 全 killed，实测全 killed）

| 变异 | 内容 | 结果 |
|---|---|---|
| M1 | 同 `(request_id, attempt_ordinal)` + 不同 `raw_output_hash` 被当成静默覆盖（`if` 分支改为 `pass`） | **KILLED**（`test_the_same_ordinal_with_a_different_raw_output_is_an_identity_conflict`；1 failed） |
| M2 | `get_mission_protocol` 缺行时返回伪造的新协议绑定（「缺行=新协议」） | **KILLED**（4 failed；含 S3、R3、legacy 不写新表） |
| M3 | 改 migration 18 的 `DDL` 字节但仍期望绿 | **KILLED**（5 failed；含 checksum 钉死与升级 drill） |

M1 的第一次尝试**SURVIVED**：我的 S8 用了「不同 raw ⇒ 不同 `decision_id`」，于是 `decision_id` 检查先于 raw 检查命中，
把 raw 检查整个遮住了。这正是核验清单要抓的形状，因此把 S8 补强为
「不同 raw + **沿用第一枚 `decision_id`** 也必须 `StoreConflict`」再复跑，M1 即被同一用例 KILLED。
（结论：缺陷在测试未钉死，实现本身正确。）

---

## 6. 核验清单对照（B.5）

- [x] `SCHEMA_VERSION == 19`；三表 `STRICT`（`test_a_fresh_library_has_the_three_strict_tables`）。
- [x] 迁移 1–18 checksum 未动（`git diff` 只增 3 行；`test_migrations_sixteen_seventeen_and_eighteen_keep_their_checksums`）。
- [x] 缺行 = legacy；不读环境变量（S3 / R3）。
- [x] 不可切换协议（S5 + 新 mission 不继承）。
- [x] 同 raw replay 幂等；同 ordinal 不同 raw → `StoreConflict`（S7 / S8）。
- [x] 无 `PlanningDecision*` 事件被 append（本片 diff 零 `append_event`）。
- [x] 未改 `policy_snapshot` / `SNAPSHOT_FIELDS` / `hierarchical_dispatch` / `event_handler`。
- [x] full_target 0 新失败（3047 passed）；旧模式 560/13/0；哨兵 19。
- [x] 钉死 18 的测试已改为 19 并在本文点名（§4）。
- [x] 变异 ≥3 全 killed（§5）。
- [x] §59 至少覆盖：同 raw replay、同 ordinal 不同 raw、写入后 rollback（S10 `InjectedCrash`、调用方事务回滚）。
- [x] 独立核验（不同会话）——已完成（核验报告：`plans/llm-native-htn/H1/reviews/核验-H1-B-2026-09-18.md`，结论「修后可合」）。

**提交：** `feat(h1-b): migration 19 and the planning-decision store`

---

## 7. 第 1 轮处置（核验：修后可合）

核验报告 `plans/llm-native-htn/H1/reviews/核验-H1-B-2026-09-18.md` 判「修后可合」：无 P0，1 个 P1、2 个 P2。以下逐条处置。

### P1-1（必修）DDL 钉死测试只钉列名，未钉 `NOT NULL` / `PK`

- **问题：** `test_planning_decision_store.py:211` 的 `test_the_protocol_columns_are_exactly_the_section_eight_dot_two_columns`
  只比较 `PRAGMA table_info` 的**列名**。核验实测把 `planning_requests.intent_id`、`planning_decisions.status`、
  `planning_decisions.raw_output_hash`、`mission_planning_protocols.prompt_version` 任一列的 `NOT NULL` 去掉后，
  full_target **3044 全绿**（变异存活 = 测试缺口）。这与 B.5「三表 STRICT」「DDL 逐字一致」的验收意图不符。
- **修法：** 新增模块常量 `PROTOCOL_SCHEMA`，把三张表逐列钉成
  `(name, declared type, NOT NULL, PRIMARY KEY)`（按 `PRAGMA table_info` 顺序），测试改为整表逐列比对。
- **复验（变异全 killed）：**

| 变异 | 结果 |
|---|---|
| 去掉 `planning_decisions.status` 的 `NOT NULL` | **KILLED**（2 failed） |
| 去掉 `mission_planning_protocols.prompt_version` 的 `NOT NULL` | **KILLED**（2 failed） |
| 去掉 `planning_decisions.raw_output_hash` 的 `NOT NULL` | **KILLED**（2 failed） |
| 去掉 `planning_requests.intent_id` 的 `NOT NULL` | **KILLED**（2 failed） |
| 去掉 `mission_planning_protocols.mission_id` 的 `PRIMARY KEY` | **KILLED**（2 failed） |

### P2-1（建议）状态前进时 `detail_json` / `rejection_codes_json` 被无条件清空

- **问题：** `planning_decision_store.py:315` 的 UPDATE 对 `detail_json` / `rejection_codes_json` 无条件覆盖，
  与 `:269` docstring「never blanks a column」矛盾；核验实测 `DECODED` 写入 `detail`/`rejection_codes` 后，
  再以 `ADMITTED` + 空值推进会把两列清空。
- **修法（取核验建议的第一支）：** 两列改为「非空才覆盖」（与 `canonical_json/hash/decision_type/raw_artifact_ref` 同规则），
  并把 docstring 改成准确表述：`detail` / `rejection_codes` 为空 = 「本步没有新信息」，非空 = 本步的答案、覆盖旧值。
  先写红测试 `test_a_forward_step_does_not_erase_evidence`（修前 1 failed），再实现。
- **复验：** 变异「把两列改回无条件覆盖」**KILLED**（1 failed）；新增
  `test_a_forward_step_may_replace_the_evaluation_when_it_has_new_detail` 钉住「非空可覆盖」。

### P2-2（建议）migration 19 未钉冻结 checksum

- **问题：** `grep -rn "MIGRATION_19\|19_CHECKSUM" tests/` 无命中；16/17/18 都有硬编码 checksum 常量，19 只被
  `MIGRATIONS[-1].ddl is planning_decision_schema.DDL` 间接钉住，DDL 漂移不会以 checksum 形式报错。
- **修法：** 新增常量 `MIGRATION_19_CHECKSUM = "a50c5eaf2d4a137265623670ad39affa184473e6e3fd10e654a3ca28f48812e0"`，
  并在 `test_migration_nineteen_is_the_new_head` 断言 `schema.MIGRATIONS[18].checksum == MIGRATION_19_CHECKSUM`
  与 `schema.checksum() == MIGRATION_19_CHECKSUM`。

### 处置轮验收（命令与尾行原样）

```text
PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target/test_planning_decision_store.py tests/orchestrator/full_target/test_planning_decision_request_binding.py -q -p no:cacheprovider
35 passed in 0.40s
```

```text
PYTHONPATH=src uv run --offline pytest tests/orchestrator/full_target -q -p no:cacheprovider
3047 passed, 2 skipped in 126.92s (0:02:06)
```

```text
PYTHONPATH=src uv run --offline ruff check src/agent_orchestrator/storage/ tests/orchestrator/full_target/test_planning_decision_store.py tests/orchestrator/full_target/test_planning_decision_request_binding.py tests/orchestrator/full_target/test_htn_store.py tests/orchestrator/full_target/test_htn_end_to_end.py
All checks passed!
```

**处置提交：** `fix(h1-b): pin DDL NOT NULL/PK, keep evaluation on status advance, freeze migration 19 checksum`
