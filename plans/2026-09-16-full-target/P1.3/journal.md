# P1.3 · 替代任务沿 Obligation 继承失败计数与已消费额度（单路径接线）

- 计划包：FULL-TARGET-1.4 §6.1 / §8.4 / §15.2 / §18.2 / §18.5 / §23 / §24.1 裁决 7
- 基线：main HEAD `623d4c8`（工作树含 P1.2 / P2.1 / P2.2b 在途改动，本片未触碰）
- 耗时：约 1 小时 10 分（读码 25 分钟、测试先行 20 分钟、实现 10 分钟、回归与门槛 15 分钟）

## 1. 改动点

| 文件 | 性质 | 说明 |
| --- | --- | --- |
| `src/agent_orchestrator/orchestrator/obligation_commits.py` | 新建 197 行 | `ObligationCommitsMixin`，方法 `_inherit_obligation_on_replacement(conn, mission_id, old_task_id, new_task_id, *, reason)` 与私有辅助 `_successor_obligation` |
| `src/agent_orchestrator/orchestrator/commit_service.py` | 改 10 行（+9 / −1） | 1 行 import、1 行基类列表末尾加 mixin、7 行在 `_commit_graph_change` 的 `validated.superseded` 分支加一处调用 |
| `tests/orchestrator/full_target/test_obligation_inheritance.py` | 新建 548 行 | 29 条测试 |

`mission_tail_commits.py` **未改**：按交付项 2「二选一，选侵入最小的」，通读该文件确认它只承载 system pool / hold / allowance 语义，没有任何替代（supersede）路径；替代唯一发生在 `_commit_graph_change` 的 `superseded` 循环，因此接线点放在那里，`mission_tail_commits.py` 零改动。

### 行为

1. 旧 Task 无 `task_semantics` 绑定 → 一次 SELECT 后立即返回 `None`，不构造 `ObligationStore` / `HtnStore`，不写任何新表，不改任何旧事件 payload。
2. 有绑定 → 同一事务内（`Store.transaction()` 对同一 task 可重入，故复用提交事务）：
   - 读旧绑定的 `obligation_id`；
   - 替代命令未自带绑定时，把旧绑定复制给新 Task（`task_id` 换成新 id，`contract_revision=0`），**obligation_id 原样沿用**——这就是继承本身，因为失败计数 / spent / fuel 全部以 `obligation_id` 为键；
   - 替代命令自带绑定且是另一个 duty 时，写 `obligation_relations`（父=旧 duty、子=新 duty），`detail` 记 `reason` 与两个 task id；
   - `ObligationStore.load_ledger` → `ledger.note_shape_change(...)` → `ObligationStore.persist(ledger)`；`persist` 的 UPDATE 分支不碰 `spent_tokens`，其余计数按读回值原样写回，故 failure_count / spent_cost_micros / spent_attempts / fuel_limit / fuel_used / fuel_remaining 全不变；
   - 追加新事件 `ObligationInherited`。
3. `reason` → `ShapeChange` 映射（常量 `INHERITANCE_REASONS`）：`REPLACE→SUCCESSOR_TASK`、`RENAME→TASK_RENAMED`、`ROLE_CHANGE→AGENT_REASSIGNED`、`METHOD_CHANGE→METHOD_SWITCHED`。未知 reason 抛 `StoreError`，不写任何东西。
4. `new_task_id == old_task_id` 是原地改角色 / 改方法：只记 shape change，不复制绑定、不写关系（表有 `parent<>child` 约束）。

## 2. 事件

只**新增**一种事件类型 `ObligationInherited`，payload 走 `_emit` 的既有 canonical 通道，字段：
`obligation_id`、`successor_obligation_id`、`superseded_task`、`successor_task`、`reason`、`shape_change`、`relation`、`binding_inherited`、`failure_count`、`consumed_cost_micros`、`consumed_attempts`、`spent_tokens`、`fuel_limit`、`fuel_used`、`fuel_remaining`、`shape_changes`。

**未修改任何既有事件类型的 payload 结构**（§18.5 硬约束 1）。有专门测试断言 `TaskSuperseded` 的 payload 键集合仍是 `{reason, replaced_by}`，`TaskGraphChanged` / `TaskCommitted` 不含 obligation 字段；另有一条测试把同一旧模式 replace 在「mixin 关闭（monkeypatch 成 no-op）」与「开启」两个库上各跑一遍，逐字节比较 `events.payload_json` 全表相等。

## 3. 测试

新文件 29 条（要求 ≥20），其中 6 条变异自证（要求 ≥4）：

- 继承主路径 8 条：新 Task 绑定同一 duty、failure_count / cost / attempts / tokens / fuel 各不变、一条 `SUCCESSOR_TASK` 历史、`ObligationInherited` 事件字段、旧事件 payload 结构不变。
- 换角色 / 换方法 / 改名 5 条（T069、T025 持久化部分）：各记一条对应 shape change，三次叠加后历史 3 条而计数一条不动。
- 显式新 duty 2 条：写 `obligation_relations` 父子行、同一 duty 不写自指关系。
- 拒绝与事务 4 条：未知 reason 拒绝且零写入、绑定指向 Mission 不持有的 duty 拒绝、事务外调用拒绝、persist 之后注入异常 → 30 张新表计数复原、关系与账本历史都未落。
- 旧模式零回归 4 条：30 张新表全空、事件 payload 逐字节相等、无绑定分支不触达 obligation 层（把 `ObligationStore` / `HtnStore` monkeypatch 成抛异常仍返回 `None`）、旧 supersede 语义（CANCELLED / receipt / `supersedes_task`）不变。
- 变异自证 6 条：重置 failure_count、回满 fuel、给后继另发一个全新 duty、漏记 shape change、旧模式路径写新表、改写旧事件 payload —— 每个变异体都被对应断言抓住。

### 红 → 绿

把 `commit_service.py` 的调用点改成 `pass` 后重跑：**10 failed / 19 passed**（5 条主路径断言 + 5 条依赖调用点的变异自证失败）；恢复后 **29 passed**。

### 结果

- `tests/orchestrator/full_target/test_obligation_inheritance.py`：29 passed。
- `tests/orchestrator/full_target`（全目录，含 P1.2）：1057 passed / 1 skipped。
- `tests/orchestrator/{step02,step03,step05,step06,p33,p34,p35}`：1474 passed / 16 skipped / **1 failed**，即已知可忽略的 `p33::test_legacy_check_ast_and_default_retrieval_bytes_are_unchanged` 哈希断言。零新增失败。
- `ruff check`：三个文件全通过。
- `ruff format --check`：新文件与测试文件通过；`commit_service.py` 本就不是 format-clean（HEAD 版本同样报 reformat），已用「格式化后互比」验证我加的 10 行是 format-stable，未引入新的格式债。
- `mypy src/agent_orchestrator`：17 errors —— 与 HEAD 基线同样 17 条（全部在 `evaluation/*`、`runtime/deepseek_tokens.py` 的第三方 import-not-found），新文件零新增。
- `git diff --stat`：`commit_service.py | 10 +++++++++-`（+9 / −1），符合 ≤10 行；另有两个新建文件。未提交。

## 4. 偏差

1. **`mission_tail_commits.py` 零改动**。理由见上，属交付项 2 明示的二选一。
2. **关系 kind 用 `REFINES_PARENT` 而非 `SUPERSEDES`**，且只在「后继确实带另一个 duty」时才写。原因见下一节契约变更请求。同一 duty 被继承时按 `obligation_relations` 的 `CHECK(parent<>child)` 无法也不应写自指行；此时「被谁取代」记录在两处：新 Task 的 `task_semantics` 行（同一 `obligation_id`）与 shape change 的 `detail`（`REPLACE: <旧 task> -> <新 task>`）。
3. **只接 replace 一条路径（单路径接线）**。`ROLE_CHANGE` / `METHOD_CHANGE` / `RENAME` 由 mixin API 支持并由测试直接驱动，暂不在 `validated.roles` 等分支接线——一是本片标题即「单路径」，二是 `commit_service.py` 有 ≤10 行的硬预算。后续片若要接 `TaskRoleChanged` 分支，再加约 7 行即可。

## 5. 契约变更请求

**CR-P1.3-1：`ObligationRelation` 缺 `SUPERSEDES`。**

- 现状：`contracts/htn.py` 的 `ObligationRelation` 只有 `REFINES_PARENT` / `INDEPENDENT_AUTHORIZED`；`storage/htn_schema.py` 的 `obligation_relations.kind` CHECK 同样只允许这两个值。本片不得改 `contracts/`，`htn_schema.py` 又是 P1.2 在途文件，故无法按小片说明写 `kind=SUPERSEDES`。
- 另有 `contracts/htn.py` 的 `RelationKind.SUPERSEDES`（`TypedEdge` 用，端点可为 TASK↔TASK / OBLIGATION↔OBLIGATION），但 migration 16 没有 typed edge 表，落不了库。
- 请求：在 `ObligationRelation` 增加 `SUPERSEDES = "supersedes"`，并在 `obligation_relations.kind` 的 CHECK 里放行；或者明确裁定「duty 被继承时不写关系行，只靠 `task_semantics` 的 `obligation_id` 与 shape change 表达取代」，本片当前实现即后者。
- 影响面：加枚举值 + 改一处 CHECK；`obligation_relations` 主键已含 `kind`，不影响既有行。

---

# 审阅修复（2026-09-16，第二轮）

审阅结论「需修后合并」。CR-P1.3-1 已裁决：**维持现状，不加 `SUPERSEDES` 枚举**；继承由「`task_semantics` 同 `obligation_id` + `ObligationInherited` 事件 + shape change 行」三者共同表达。journal 第 4 节偏差 2 与第 5 节按此裁决作废，实现不变。

耗时：约 45 分钟。

## 必改三项

**1（中高）事件幂等键吞掉重复 re-plan —— 已修。**
原键 `mission:old:new:reason` 在同一 Task 连做两次 `ROLE_CHANGE` 时重复，`append_event` 按 `idempotency_key` 幂等返回旧行，第二条不同 payload 被静默丢弃，而 `obligation_shape_changes` 已落两行，两边对不上。现键并入刚写入的 shape-change 序号：`mission:old:new:reason:{view.shape_changes}`，payload 也新增 `shape_change_ordinal` 与 `graph_version`。
- 新测试 `test_the_same_reason_twice_writes_two_events_and_two_shape_changes`：两行 shape change、两条事件、ordinal `[1, 2]`、event id 互异。
- 新变异自证 `test_mutation_an_event_key_without_the_ordinal_is_caught`：变异体用旧键 → 账本 2 行而事件只有 1 条，断言被抓住。

**2（中）整册 `persist(ledger)` 抹掉邻居审计时间戳 —— 已修。**
`persist` 会重写每个 duty 的 `updated_at` / `obligation_json`，并 `DELETE` 后重插 `obligation_shape_changes` 全表、`created_at` 统一改成 now。现改为只调 P1.2 新的单行 `ObligationStore.note_shape_change(...)`（SQL 内取 ordinal、自带事务、只插一行），提交热路径不再出现 `load_ledger` / `persist`。计数读取改用 `note_shape_change` 的返回视图 + `spent_tokens()`。
- 新测试 `test_an_unrelated_duty_keeps_its_rows_and_timestamps_byte_for_byte`：另注册一个无关 duty 并给它一条更早的 shape change，replace 之后它的 `obligations` 行与 `obligation_shape_changes` 行（含 `created_at` / `updated_at`）逐字段不变，而被重规划的 duty 确实多出一行。

**3（中）复制语义绑定时未清派发态 —— 已修。**
新增模块级 `_successor_binding(binding, new_task_id)`：`contract_revision=0`、`dispatch_generation=0`、`input_binding_revision=0`、`adopted_method_instance_id=None`、`occurrence_binding=None`，`contract_hash` 用 `content_hash_of({inherited_from_task_id, inherited_from_contract_hash, inherited_from_contract_revision, task_id, contract_revision})` 重算（与 `grounding.task_binding_for` 同一套 `content_hash_of` 口径）。继承的是含义（duty、goal signature、typed parameters、ports、requirements、operator），不是派发权。
- 新测试 `test_the_successor_inherits_the_meaning_but_not_the_dispatch_state`：前任带 `adopted_method_instance_id=instance-9`、`occurrence_binding`、`input_binding_revision=5`、`dispatch_generation=4`、`contract_revision=7`，断言后继这些字段全部归零 / 置 None，`contract_hash` 与前任不同且仍是 64 位。

## 次要两项

- 未知 reason 改抛 `ContractError`（原 `StoreError`），对齐同目录 mixin 风格；对应测试同步改断言。
- shape change 的 `detail` 改为 canonical JSON `{"graph_version":…, "new_task_id":…, "old_task_id":…, "reason":…}`，可机读重建。为此给 `_inherit_obligation_on_replacement` 增加关键字参数 `graph_version: int | None = None`，`commit_service.py` 调用点多传一行 `graph_version=new_version`——**`commit_service.py` 的 diff 因此从 10 行变成 11 行（+10 / −1）**，超出原定 ≤10 行预算 1 行，理由即此项要求，请知悉。

## 追加要求：零裸 SQL

P1.2 存储层定稿后提供了 `HtnStore.task_semantics_of(mission_id, task_id)` 与 `ObligationStore.exists(mission_id, obligation_id)`。已把 `obligation_commits.py` 的两处裸 SQL 全部换掉，删除模块级常量 `_LATEST_BINDING`，并删掉 `import json`：
- 前任绑定查询 → `HtnStore.task_semantics_of`；
- duty 存在性检查 → `ObligationStore.exists`；
- 后继绑定查询 → `HtnStore.task_semantics_of`（`_successor_obligation` 改为 `@staticmethod`，参数从 `conn` 换成已构造的 `HtnStore`）。

`conn` 参数保留，现在只用于 `conn.in_transaction` 的事务守卫。

`tests/orchestrator/full_target/test_htn_store.py` 的 `KNOWN_RAW_SQL_DEBT` 两条已删，白名单为 `frozenset()`，静态守卫 `test_no_module_outside_storage_writes_the_new_tables_in_sql` 现在对全仓零豁免通过。这是本片对 P1.2 测试的唯一改动。

因为 legacy 分支现在会构造一次 `HtnStore` 做那次查询，`test_a_task_without_a_binding_touches_nothing_beyond_the_first_lookup` 的守卫同步收紧为：`ObligationStore` 一次都不构造、`HtnStore.put_task_semantics` 一次都不调用。

## 其他调整

`test_a_legacy_replacement_leaves_all_thirty_new_tables_empty` 改名为 `..._every_migration_16_table_empty`，断言从硬编码 30 改为 `set(counts) == set(TABLES) and len(counts) >= 30`——P1.2 在途给 migration 16 加了第 31 张表，硬编码会随并发改动误报；`set(counts) == set(TABLES)` 仍保证覆盖整个迁移而非子集。

## 修复后门槛

| 项 | 结果 |
| --- | --- |
| `test_obligation_inheritance.py` | **33 passed**（原 29 + 新 3 + 新变异 1），其中变异自证 7 条 |
| 三条新测试的红→绿 | 把三项修复逐一回退后重跑，三条**全部 FAILED**；恢复后全绿 |
| `tests/orchestrator/full_target`（全目录） | 1404 passed / 1 skipped |
| `step02 step03 step05 step06 p33 p34 p35` | 1474 passed / 16 skipped / 1 failed（仅已知可忽略的 `p33::test_legacy_check_ast_and_default_retrieval_bytes_are_unchanged`），零新增失败 |
| `ruff check` | 四个文件全通过 |
| `ruff format --check` | `obligation_commits.py`、两个测试文件通过；`commit_service.py` 是 HEAD 既有格式债，已互比确认新增 11 行 format-stable |
| `mypy src/agent_orchestrator` | 17 errors，与 HEAD 基线同样 17 条（全是第三方 import-not-found），零新增 |
| `git diff --stat` | `commit_service.py | 11 ++++++++++-`（+10 / −1）；另两个新建文件与本次 `test_htn_store.py` 的 1 行白名单删除 |

代码行数：`obligation_commits.py` 237 行，`test_obligation_inheritance.py` 676 行 / 33 条测试。未提交，未跑全量回归，全程未用 `git stash`。
