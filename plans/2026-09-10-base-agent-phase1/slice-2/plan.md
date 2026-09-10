plan-status: finalized (由主编排者依据 BA-v1.0 拆片，2026-09-10)

# Slice 2 实施计划：生命周期与批量创建

- 仓库：`/Users/taiwan/PROJECTS/SimplaHarness/simple-harness-sdk`，基线 main = `4613e62`（S1 已交付）
- 上游规范：Host `plans/taskSys2/base-agent-phase1-plan.zh-CN.md`（BA-v1.0）§1.3 / §4.2 / §4.3 / §5 / §8 / §10 / §13
- 代码事实：同目录 `investigation.md`（**所有行号在 `4613e62` 上核对**）
- 切片划分：上级 `program.md` §2「S2」
- 验收：同目录 `acceptance.md`
- 前片：`../slice-1/{plan.md,acceptance.md,journal.md}`（遗留 L1–L12 见 journal §6）

## 0. 目标与不做的事

**目标**：把 BaseAgent 的持久生命周期补完——同一 Agent 的多条输入严格顺序处理并有队列配额；`close` 是"拒收新输入但不进终态"的持久操作且跨重启有效；一个配置模板可批量幂等地造出 N 个实例（同 key 同内容返回原 IDs、不同内容冲突、任一非法整批不留部分成功）；`open` 只对本 owner 开放；per-turn 限制真正执法；UNKNOWN/授权恢复保持同 Turn 同动作身份；`ask` 超时与调用方放弃等待都不取消底层 Turn。

**本片不做**（逐条在 acceptance「明确不包含」重述）：

- 有界 Context 装配、tokenizer 计数、增量 Journal（S3 的 BA13–BA19/BA22）。
- 混合召回与 Agent 间检索隔离（S4）。
- **多 Agent 并发公平限流、Provider/工具并发槽位、队列轮转调度**（S5 的 BA35；本片只做**单 Agent 的队列深度配额**）。
- 故障注入矩阵、`react_loop` 最终 CAS 与 stage 之间的**残留冻结窗口**（S5 的 BA31，S1 L1）。
- 已有 v9 库的就地升级器与迁移回执（S5 的 BA37）；**已存在的 v10 开发库在本片后会因描述符重算而拒绝打开**（investigation S2-D9）。
- exact-wheel conformance 与发布说明（S5 的 BA40）。
- **L5 / F-BA-1**：主编排者裁决**并入本片**（T10 / AC13，次要），不再列为不包含。
- 修复 `../baseline-known-failures.txt` 里的既有红。
- 任何对 `simple_harness_memory` 的依赖；向模型暴露 `create_many` / `close` / `cancel_turn`（控制面只对调用方开放，不进工具目录）。

**执行顺序（9 个任务）**：

```
T1 基线复核 + schema v10 增补（第 0 步无代码改动）
  → T2 输入队列护栏与顺序保证（BA08）
  → T3 close 控制面（BA12）
  → 【T3.5 价值验证里程碑：一条 pytest = 顺序处理 + close 拒收 + 重启仍 close】
  → T4 open 的 owner_scope 闸门（BA05）
  → T5 create_many 批量幂等（BA02 / BA03 / BA04）
  → T6 per-turn 限制与 turn deadline 执法（L3）
  → T7 UNKNOWN / 授权恢复的同 Turn 同动作身份（BA11 / L4 / D7 裁决）
  → T8 cancel_turn 最小合同 + Turn 失败不清空 session/费用（BA10）
  → T9 uow facade 下沉（L12）+ 回归与收尾
```

**价值验证里程碑 = T3.5**：一条 pytest 命令同时证明主要矛盾的两面——"同一执行身份严格串行消化两条输入"与"close 之后拒收新输入、Agent 不进终态、重启后仍拒收"。

---

## T1 · 基线复核 + schema v10 增补

### 第 0 步：基线复核与锚点核对（无代码改动）

产出 `plans/2026-09-10-base-agent-phase1/slice-2/baseline-recheck.md`：

1. 跑固定回归命令（见下"验证栏"），逐条比对 `../baseline-known-failures.txt`，并记录 S1 已转绿的两条（import purity、public-api 快照）——口径是**红集 ⊆ 基线红集**。
2. 跑 `.venv/bin/python -m pytest -q -p no:cacheprovider tests/agents tests/execution/test_base_agent_schema_v10.py`，期望 **83 passed / 1 skipped**（本文写作时实测 4.33 s）。
3. 核对本计划每个 `文件:行号` 锚点仍成立；若 main 前移，**先改本计划再动手**（BA-v1.0 §9.3）。
4. 记录当前 `fresh_descriptor().checksum`（v10 变更前值），供 journal 说明"v10 描述符已重算"。

### 改哪些文件/符号

**`src/simple_harness/execution/sqlite/base_agent/schema.py`**（全文 68 行，`DDL` 在 `:11-66`）

| 位置 | 改动 |
|---|---|
| `:12-23` `base_agent_bindings_v1` | 增两列：`lifecycle TEXT NOT NULL DEFAULT 'open' CHECK(lifecycle IN ('open','closing','closed'))`、`lifecycle_updated_at REAL` |
| `:66` 之前追加 | 新表 `base_agent_creation_batches_v1`：`batch_id TEXT PRIMARY KEY`（= `sha256(owner_scope\x00batch_key)`）、`owner_scope TEXT NOT NULL`、`batch_key TEXT NOT NULL`、`batch_fingerprint TEXT NOT NULL CHECK(length(batch_fingerprint)=64)`、`agent_ids_json TEXT NOT NULL`、`config_hashes_json TEXT NOT NULL`、`state TEXT NOT NULL CHECK(state IN ('reserved','committed'))`、`receipt_json TEXT`、`created_at REAL NOT NULL`、`updated_at REAL NOT NULL`、`UNIQUE(owner_scope,batch_key)`，`STRICT` |
| `:66` 之前追加 | 新表 `base_agent_control_commands_v1`：`command_id TEXT PRIMARY KEY`、`agent_id TEXT NOT NULL REFERENCES base_agent_bindings_v1(agent_id)`、`kind TEXT NOT NULL CHECK(kind IN ('close','cancel_turn'))`、`target_turn_id TEXT`、`control_generation INTEGER NOT NULL`、`request_hash TEXT NOT NULL CHECK(length(request_hash)=64)`、`receipt_json TEXT NOT NULL`、`created_at REAL NOT NULL`，`STRICT`；外加 `CREATE INDEX base_agent_control_commands_v1_agent_idx ON base_agent_control_commands_v1(agent_id, kind);` |

**`src/simple_harness/execution/base_agent.py`**：`AgentBindingRecord`（`:38` 附近）增 `lifecycle: str`、`lifecycle_updated_at: float | None`；新增 `AgentCreationBatchRecord`、`AgentControlCommandRecord` 两个 frozen dataclass。
**`src/simple_harness/execution/sqlite/base_agent/turns.py`**：`_binding`（`:27-39`）与 `insert_binding`（`:89-126`）的列表补新列（插入时显式写 `'open'`）。

**不改**：`sqlite/schema.py`（`fresh_descriptor()` 在 `:69-73` 由 `base_agent.schema.DDL` 现算，自动重算）、`short_context_migration.py`、`database.py`、`audit_schema.py`。

### 新增模块

- `src/simple_harness/execution/sqlite/base_agent/batches.py`（T5 用）
- `src/simple_harness/execution/sqlite/base_agent/control.py`（T3/T8 用）

（本任务只建空骨架 + 记录/读函数签名，逻辑在 T3/T5 落。）

### 必须保留的旧约束

- `legacy_v7/v8/v9_descriptor().checksum` 三个值**一位不变**（`tests/execution/test_base_agent_schema_v10.py:24-29` 硬编码期望）。
- `accepted_descriptor_rows()` 仍同时接受纯 v9 组合与 v10 组合；已有 v9 库仍能 `Database.open`。
- `migrate_execution_to_v9` 仍升到 **9**。
- **不改** `base_agent_turns_v1.phase` 的 CHECK 枚举（investigation S2-D1）。
- 只有一个 v10：新表**追加进同一份 DDL**，不新建第二个描述符（`program.md` §3.3）。

### 验证栏（oracle）

改 `tests/execution/test_base_agent_schema_v10.py`：

- `test_fresh_open_is_v10`：`BASE_AGENT_TABLES` 扩为 6 张，仍是 `<= table_names(...)`；`schema_version == 10`。
- `test_descriptor_checksums_are_stable`：**不动**（仍只钉 7/8/9），跑绿即证明 v9 未被污染。
- `test_v9_database_still_opens`：**不动**，跑绿。
- 新增 `test_bindings_default_lifecycle_is_open`：直连 sqlite 读 `PRAGMA table_info(base_agent_bindings_v1)`，`lifecycle` 存在且 `dflt_value` 为 `'open'`；插一行不带 lifecycle → 读回 `'open'`。
- 新增 `test_creation_batch_unique_owner_scope_batch_key`：同 `(owner_scope,batch_key)` 二次插入 → `sqlite3.IntegrityError`。
- 新增 `test_control_command_id_is_unique_and_kind_is_checked`：同 `command_id` 二次插入 → `IntegrityError`；`kind='reboot'` → `IntegrityError`。
- 新增 `test_v10_descriptor_changed_and_is_the_only_ten`：`fresh_descriptor().version == 10 and name == "0010_fresh"`；`{d.version for row in accepted_descriptor_rows() for d in row} == {7,8,9,10}`；`len({d.checksum for row in accepted_descriptor_rows() for d in row if d.version == 10}) == 1`。

回归命令（第 0 步与收尾各一次）：

```
.venv/bin/python -m pytest -q -p no:cacheprovider \
  --ignore=tests/integration/runtime/test_context_use_admission.py \
  --ignore=tests/integration/runtime/test_context_use_durable.py \
  --ignore=tests/integration/runtime/test_context_use_public_memory.py
```

**算对**：红集 ⊆ `../baseline-known-failures.txt`；新红一条即阻断。

**覆盖 AC**：AC1/AC3/AC5/AC6 的持久化基座。

---

## T2 · 输入队列护栏与顺序保证（BA08）

### 改哪些文件/符号

**`src/simple_harness/execution/sqlite/base_agent/turns.py`**

- `open_turn`（`:155-200`）：在 `INSERT` 之前、同一 connection 内加队列配额判定——
  `SELECT COUNT(*) FROM base_agent_turns_v1 WHERE agent_id=? AND phase IN ('queued','running','result_pending')`；`>= max_pending_inputs` 时抛新异常 `PendingInputsExhausted(UnitOfWorkConflict)`。
  **幂等重放先于配额**：`read_turn_by_input` 命中（`:172-176`）时直接返回，不受配额影响。
  新增参数 `max_pending_inputs: int | None = None`（None = 不限，兼容既有调用）。

**`src/simple_harness/execution/sqlite/uow.py`**

- `submit_agent_input`（`:1216-1273`）：新增 keyword `max_pending_inputs: int | None = None`，透传给 `turns.open_turn`（`:1231-1240`）。**判定与写入同一事务**（与 `delegations.reserve_delegation` 的配额处置同范式，`sqlite/base_agent/delegations.py:74-79`）。

**`src/simple_harness/runtime/kernel.py`**

- `signal_base_agent_input`（`:1718-1760`）：新增 keyword `max_pending_inputs: int | None = None`，透传到 `:1745` 的 `self._uow.submit_agent_input(...)`。**内核不解释这个数字**（不 import `agents`，只搬运）。

**`src/simple_harness/agents/base.py`**

- `submit`（`:92-119`）：传 `max_pending_inputs=self._config.limits.max_pending_inputs`；`except UnitOfWorkConflict` 分支（`:111-112`）先判 `PendingInputsExhausted` → 抛新公开错误 `AgentPendingInputsExhausted`（`code="agent_pending_inputs_exhausted"`），否则维持 `AgentInputConflict`。

**`src/simple_harness/agents/contracts.py`**：新增 `AgentPendingInputsExhausted(AgentError)`；只加进 `agents/__init__.py` 的 `__all__`，**不进根包**。

### 必须保留的旧约束

- continuation 的 FIFO 认领与单 drive task 机制**一行不改**（`uow.py:4174-4181`、`live_index.py:23-27`、`kernel.py:2894-2895`、`:3405-3411`）。
- `UNIQUE(agent_id,input_id)` / `UNIQUE(agent_id,seq)` 语义不变；同 `input_id` 同内容重放仍返回原回执。
- `signal_base_agent_input` 的既有签名向后兼容（新参数带默认值），S1 的用例不改。

### 验证栏（oracle）

新文件 `tests/agents/test_input_queue.py`：

- **`test_two_concurrent_inputs_are_processed_in_order`**（BA08 决定性）：
  `await asyncio.gather(agent.submit("A", input_id="i1"), agent.submit("B", input_id="i2"))` →
  (a) 两个 turn 的 `seq` 为 `{1,2}` 且与 `continuations.fifo_seq` 的先后**一致**；
  (b) `provider.requests` 恰好 2 条，第 2 条的 messages 里**含**第 1 轮的 assistant 回答与第 1 条用户消息；
  (c) 两条 continuation 均 `ACKED`；
  (d) `run_events` 中 `run.waiting` 事件的 `turn_id` 顺序 = seq 顺序；
  (e) run 全程无 `completed/failed/cancelled` 事件。
- **`test_context_is_never_written_concurrently`**：spy `SqliteContextPort.append`，记录 `(append_id, expected_revision, wall)`；断言 revision 严格递增、无重复 `append_id`、无 `UnitOfWorkConflict`；断言 append 调用区间**两两不重叠**（同一 run 的 append 不交错）。
- **`test_only_one_drive_task_per_run`**：包裹 `Runtime._drive`，记录进入/离开；断言同一 run_id 的执行区间不重叠，且 `len(live.active_run_ids()) <= 1`（对该 run）。
- **`test_pending_inputs_quota_rejects_the_third_input`**：`AgentLimits(max_pending_inputs=2)` + provider 阻塞；投 i1、i2 成功，i3 抛 `AgentPendingInputsExhausted`；断言 `base_agent_turns_v1` 仍是 2 行、`continuations` 仍是 2 行；放开 provider 后 i1/i2 正常提交，随后 i3 可成功投递。
- **`test_quota_does_not_block_idempotent_replay`**：队列已满时重放 i2（同内容）→ 返回原回执、不抛。

**覆盖 AC**：AC2（BA08）、AC1 的一半（里程碑的顺序部分）。

---

## T3 · `close` 控制面（BA12）

### 新增模块

- `src/simple_harness/execution/sqlite/base_agent/control.py` —— 连接级 helper（接受外层 connection，不自开事务）：
  `read_binding_lifecycle(connection, agent_id)`、
  `record_control_command(connection, *, command_id, agent_id, kind, target_turn_id, request_hash, receipt, now) -> (AgentControlCommandRecord, created)`（同 `command_id` 重放：`request_hash` 相同返回原行；不同抛 `UnitOfWorkConflict`）、
  `set_lifecycle(connection, *, agent_id, lifecycle, now) -> AgentBindingRecord`（`open→closing→closed` 单向，逆向抛冲突；同态重放放行）、
  `bump_control_generation(connection, agent_id) -> int`。

### 改哪些文件/符号

**`src/simple_harness/execution/sqlite/base_agent/turns.py`**

- `open_turn`（`:155-200`）：**在幂等重放判定之后、配额判定之前**读 binding 的 `lifecycle`；不是 `'open'` → 抛 `AgentClosedError(UnitOfWorkConflict)`。
  **顺序理由**：已受理的输入重放不应因 close 而变成错误（"close 不擦除历史"）。

**`src/simple_harness/execution/sqlite/uow.py`**：新增三个薄 facade（各自一个事务，≤ 20 行）：
`close_agent(*, agent_id, command_id, request_hash, now) -> AgentControlCommandRecord`、
`mark_agent_closed(*, agent_id, now)`、
`read_agent_control_command(command_id)`。

**`src/simple_harness/agents/runtime.py`**

- `AgentRuntime` 增 `async def close_agent(self, agent_id, *, command_id, drain_timeout=30.0) -> AgentClosingReceipt`：
  1. 事务 1 → `uow.close_agent(...)`（lifecycle `open→closing`、`control_generation+1`、写控制命令行与回执）；
  2. drain：轮询 `uow.read_open_agent_turn(run_id)`（`uow.py:1299-1305`）直到 `None` 或 `clock() >= deadline`，sleep ≤ 0.2 s；
  3. 事务 2 → 无开放 Turn 则 `mark_agent_closed`；否则保持 `closing`。
  返回 `AgentClosingReceipt(agent_id, command_id, state, open_turn_id, control_generation, created_at)`。
- `shutdown`（`:264-267`）：**一行不改**，只在测试里断言它不改任何 binding 的 lifecycle。

**`src/simple_harness/agents/base.py`**

- 增 `async def close(self, *, command_id: str, drain_timeout: float = 30.0) -> AgentClosingReceipt`（转发 `runtime.close_agent`），并在返回后刷新 `self._binding`。
- `submit`（`:92-119`）：`except UnitOfWorkConflict` 分支加 `AgentClosedError` → 抛已存在的 `AgentClosed`（`contracts.py:66-67`）。
- `status`（`:162-180`）：`AgentStatus` 增 `lifecycle_state: str`（binding 的 `open/closing/closed`），`lifecycle` 属性（`:54-62`）保持"由执行状态与当前 Turn 投影"的旧语义，新增 `CLOSING/CLOSED` 优先级高于 IDLE、低于 FAILED。

**`src/simple_harness/agents/contracts.py`**：新增 `AgentClosingReceipt` frozen dataclass；只进 `agents/__init__.py`。

### 必须保留的旧约束

- **绝不使用内核 cancel**：不调 `RunClient.cancel` / `request_run_cancel` / `_terminalize_cancelled`（`kernel.py:1458`、`:3223`、`:3413`）；close 之后 Run 必须仍是 `WAITING`。
- close **不擦除历史**：`get_result` / `history()` / `turn_state` 在 closed 后行为不变。
- 内核不 import `agents`：闸门在 `sqlite/base_agent/turns.py`（execution 层）。
- `runtime.shutdown()` 不触碰任何 binding 行。

### 验证栏（oracle）

新文件 `tests/agents/test_agent_close.py`：

- **`test_close_rejects_new_input_and_keeps_the_agent_non_terminal`**：`close(command_id="c1")` → 回执 `state == "closed"`；`await agent.submit(...)` 抛 `AgentClosed`；`uow.read_run(run_id).state is RunState.WAITING`；`run_events` 无 `completed/failed/cancelled`。
- **`test_close_is_idempotent_by_command_id`**：同 `command_id` 二次 `close` 返回**同一** `AgentClosingReceipt`（逐字段相等），`base_agent_control_commands_v1` 仍 1 行，`control_generation` 只 +1；换 `command_id` 但 agent 已 closed → 返回 `state="closed"` 且**不再** bump generation。
- **`test_close_drains_the_open_turn_and_returns_a_queryable_receipt`**：provider 阻塞时 `close(drain_timeout=0.05)` → 回执 `state == "closing"` 且 `open_turn_id == 该 turn`；放开 provider，该 Turn 正常 committed；再次 `close`（新 command_id）→ `state == "closed"`。
- **`test_closed_agent_survives_restart`**：close → runtime 关闭 → 租约过期 → 新 runtime `open(agent_id)` → `submit` 仍抛 `AgentClosed`；`get_result(旧 turn_id)` 仍返回原结果、`history()` 长度不变、provider 调用计数不变。
- **`test_shutdown_does_not_close_any_agent`**：建 3 个 Agent → `await runtime.shutdown()` → 直接读库断言 3 行 `lifecycle == 'open'`、`control_generation == 0`；新 runtime 打开后三者都能 `ask` 成功。
- **`test_close_keeps_history_readable`**：先 ask 两轮，再 close，断言 `history()` 仍 2 条、`get_result` 两个 turn 都可读、`turn_snapshot`（T7 后）可读。

**覆盖 AC**：AC1（BA12）。

---

## T3.5 · 【价值验证里程碑】停而不死 + 严格串行

### 新增文件

`tests/agents/test_agent_lifecycle.py`（**一条命令即里程碑**）

```
.venv/bin/python -m pytest -q tests/agents/test_agent_lifecycle.py
```

### 里程碑断言（一条用例内串起来）

`test_ordered_inputs_then_close_then_restart_still_closed`：

1. 用真实 `build_agent_runtime` + `ScriptedProvider(["A1","B1"])` 建一个 Agent；
2. `asyncio.gather` 并发投 i1/i2 → 两轮均 committed，`seq` 为 1、2，第 2 次 provider 请求含第 1 轮 assistant 回答（**顺序**）；
3. `close(command_id="c1")` → 回执 `state="closed"`；
4. `submit("第三条", input_id="i3")` 抛 `AgentClosed`；`base_agent_turns_v1` 仍 2 行；
5. `read_run(run_id).state is RunState.WAITING`，`run_events` 全量回放无终态事件（**不死**）；
6. runtime 关闭 → 时钟推进过租约 → 新 runtime `open(agent_id)` → `submit` **仍**抛 `AgentClosed`，`history()` 仍 2 条，`provider.calls == 2`（**跨重启持久**）。

### 最小验证动作

上面那一条命令全绿 = S2 主要矛盾的两面都被真实装配证明。此时 T4–T9 全部是"补齐与加固"，任何一条失败都不再动摇价值链。

---

## T4 · `open` 的 owner_scope 闸门（BA05）

### 改哪些文件/符号

**`src/simple_harness/agents/runtime.py`**

- `open`（`:327-332`）：`binding is None` **或** `binding.owner_scope != self._owner_scope` → 抛 `AgentNotFound(value)`（**同一异常类、同一消息模板**）。
- `binding()`（`:334-335`）：同样加 owner 过滤（它是 `AgentRuntime` 的公开读口）。
- `create`（`:274-313`）：`read_agent_binding` 命中但 `owner_scope` 不符 → 抛 `AgentNotFound`（不能因 `config_hash` 不同而泄露"这个 id 归别人"）。

**`src/simple_harness/agents/base.py`**：`get_result`（`:121-133`）/ `turn_state`（`:135-139`）/ `history`（`:182-190`）的 `agent_id` 归属校验保持不变（已按 `self.agent_id` 过滤）。

### 必须保留的旧约束

- `agent_id_for` 的摘要口径不变（`runtime.py:187-191`），同 owner 同 key 仍复现同一 Agent。
- 内核层**不加 owner 概念**（`signal_base_agent_input` 不改）：句柄只能经 `create`/`open` 取得，两者都被闸门覆盖；记为边界（investigation §8）。

### 验证栏（oracle）

新文件 `tests/agents/test_agent_open_scope.py`：

- **`test_open_across_owner_scope_is_indistinguishable_from_not_found`**：同一 DB 上建 `owner_scope="alice"` 与 `"bob"` 两个 `AgentRuntime`；alice 创建 agent A；
  `err_cross = pytest.raises(AgentNotFound, bob.open(A.agent_id))`、`err_missing = pytest.raises(AgentNotFound, bob.open("agent-" + "0"*32))`；
  断言 `type(err_cross.value) is type(err_missing.value)` **且** `str(err_cross.value) == str(err_missing.value).replace(缺失 id, A.agent_id)`（消息模板逐字相同，只差 id）。
- **`test_cross_owner_error_leaks_no_content`**：断言 `str(err_cross.value)` 不含 A 的 `config.name`、`instructions`、`run_id`、`creation_key`。
- **`test_cross_owner_cannot_read_results`**：alice 先 `ask` 一轮；bob 无法经任何公开入口拿到该 `AgentTurnResult`（`bob.open` 抛；`bob.binding(A.agent_id) is None`）。
- **`test_same_owner_open_still_works_after_restart`**：alice 重启后 `open` 仍成功且 `config_hash` 一致。
- **`test_create_with_foreign_agent_id_collision_raises_not_found`**：bob 用与 A 相同 `creation_key`（不同 owner → 不同 agent_id）仍能创建自己的 Agent，两者 `agent_id` 不同、Context 互不可见。

**覆盖 AC**：AC8（BA05）。

---

## T5 · `create_many` 批量幂等（BA02 / BA03 / BA04）

### 新增模块

- `src/simple_harness/execution/sqlite/base_agent/batches.py`：
  `reserve_batch(connection, *, batch_id, owner_scope, batch_key, batch_fingerprint, agent_ids, config_hashes, now) -> (AgentCreationBatchRecord, created)`（同 `(owner_scope,batch_key)` 命中：fingerprint 相同返回原行；不同抛 `BatchIdentityConflict(UnitOfWorkConflict)`）、
  `commit_batch(connection, *, batch_id, receipt, now)`、
  `read_batch(connection, owner_scope, batch_key)`。

### 改哪些文件/符号

**`src/simple_harness/agents/ports.py`**（`AgentRuntimePorts`，`:34-86`）

- 增 `max_agents: int = 1000`、`max_batch_size: int = 200`；`__post_init__`（`:62-85`）加正整数校验。

**`src/simple_harness/agents/runtime.py`**

- 新增模块函数 `batch_fingerprint(owner_scope, batch_key, configs) -> str` =
  `sha256(canonical_json({"owner_scope":…,"batch_key":…,"count":N,"config_hashes":[按提交顺序]}))`。
- `create_many`（`:315-325`）重写为四段：
  1. **整批准入校验（写前，零副作用）**：每项必须是 `AgentConfig`；`len(configs) >= 1` 且 `<= ports.max_batch_size`；`已有实例数 + N <= ports.max_agents`；每个 config 的 `tool_names ⊆ 运行时工具目录 ∪ {DELEGATE_TOOL_NAME}`。任一不过 → `AgentBatchRejected(error_code, index)`，**此前一行未写**。
  2. `agent_ids = [agent_id_for(owner_scope, f"{batch_key}:{i}") for i …]`；`reserve_batch`（事务）。
  3. 逐个 `create(config, creation_key=f"{batch_key}:{i}")`（已幂等，`runtime.py:274-313`）。
  4. `commit_batch`（事务，回执含 agent_ids 与 fingerprint）；返回按序 handle。
  幂等入口：`read_batch` 命中且 fingerprint 相同 → `reserved` 则续做第 3、4 步，`committed` 则**直接按回执顺序 `open`**；fingerprint 不同 → `AgentBatchIdentityConflict`。

**`src/simple_harness/agents/contracts.py`**：新增 `AgentBatchRejected`、`AgentBatchIdentityConflict`（均继承 `AgentError`，`code` 分别 `"agent_batch_rejected"` / `"batch_identity_conflict"`）。

### 必须保留的旧约束

- **创建阶段不调用模型**：driver 无输入即 `WAITING`（`agents/execution.py:142-161`），本任务不得引入任何 provider 调用。
- 单个 `create` 的幂等语义与 `creation_key` 唯一约束不变（`turns.py:103-107`）。
- 批次行只在**自己的**事务里写；不试图把 N 个 run 塞进一个事务（investigation S2-D3）。

### 验证栏（oracle）

新文件 `tests/agents/test_create_many_batches.py`：

- **`test_hundred_idle_agents_make_no_provider_request`**（BA02）：`create_many([cfg]*100, batch_key="b100")` →
  `provider.calls == 0`；`base_agent_turns_v1` 0 行；`base_agent_bindings_v1` 100 行、100 个不同 `agent_id`/`run_id`；
  **不复制模型服务**：`runtime.kernel._ports.provider` 是同一个对象（`id()` 相等）且 `ProviderInvocationCoordinator` 只有 1 个实例；
  重启后 `recover()` 不激活它们：`len(runtime.kernel._live.active_run_ids()) == 0` 且 `provider.calls == 0`（依据 `list_recoverable_root_runs` 不含 WAITING，`uow.py:7319-7328`）。
- **`test_same_batch_key_same_content_returns_the_same_ids`**（BA03 前半）：二次 `create_many` 同内容 → `[a.agent_id for a in …]` **逐位相等**；`base_agent_creation_batches_v1` 仍 1 行；`bindings` 行数不变；跨 runtime 重启后再调一次仍相等。
- **`test_same_batch_key_different_content_conflicts`**（BA03 后半）：改动其中一个 config 的 `instructions` → `AgentBatchIdentityConflict`，`code == "batch_identity_conflict"`；断言**未新增**任何 binding/run/batch 行。
- **`test_invalid_config_leaves_no_partial_batch`**（BA04 决定性）：5 个 config，第 3 个的 `tool_names=("no_such_tool",)` → `AgentBatchRejected` 且 `error.index == 2`；断言 `base_agent_bindings_v1`、`runs`、`conversation_run_modes`、`base_agent_creation_batches_v1` **行数与调用前逐表相等**。
- **`test_batch_size_and_instance_caps_are_enforced_before_any_write`**：`max_batch_size=3` 时投 4 个 → `AgentBatchRejected("agent_batch_too_large")`，零写入；`max_agents=2` 时第二批越界同理。
- **`test_reserved_batch_is_resumed_not_duplicated`**：在 `reserve_batch` 之后、第 2 个 `create` 之前注入崩溃（fault 钩子或打断 `create`），重试同 `batch_key` → 补齐剩余实例、总数正确、`agent_ids` 与第一次预留的完全一致、batch 行仍 1 行。

**覆盖 AC**：AC5（BA03）、AC6（BA04）、AC7（BA02）。

---

## T6 · per-turn 限制与 turn deadline 执法（L3）

### 改哪些文件/符号

**`src/simple_harness/agents/execution.py`**

- `_reserved_provider_turns`（`:372-383`）扩成 `_checkpoint_totals(checkpoint_port, run_id) -> (provider_turns, tool_calls, started_at | None)`（一次读，三个字段：`provider_turns_reserved_total`、`tool_calls_reserved_total`、`started_at`）。既有调用点 `:186`、`:262`、`:299` 改成取第一项。
- `AgentExecutionDriver.__init__`（`:89-110`）：不再持有单例 `self._loop`；改持 `self._collaborator_limits`（lifetime）、`self._effects`、`self._clock`、`self.policy_fingerprint`。
- `start`（`:112-335`）：在 `mark_agent_turn_running`（`:187-194`）之后、`self._loop.run(...)`（`:227`）之前——
  1. 从 `input_value["base_agent_binding"]` 取 `agent_id`，用 `invocation.services.react_checkpoint` 读的 `agent_config`？**不行**（driver 不该读 agents 配置表）。改为：**per-turn 限额随输入下发**——`start_input_for`（`runtime.py:194-220`）在 `base_agent_binding` 里加 `"limits": config.limits.to_json()` 的**per-turn 子集**（4 个字段），driver 只读 start snapshot，**不查库、不 import agents**；
  2. 派生 `turn_limits = TerminationLimits(max_turns=min(lifetime.max_turns, t0.provider_turns + per.max_model_calls_per_turn), max_tool_calls=min(lifetime.max_tool_calls, t0.tool_calls + per.max_tool_calls_per_turn), max_wall_seconds=min(lifetime.max_wall_seconds, max(0.0, turn_created_at - t0.started_at) + per.turn_deadline_seconds) or 微小正数, max_cost_micros=lifetime.max_cost_micros, max_consecutive_same_tool=lifetime.max_consecutive_same_tool)`；
     `turn_created_at` 从 continuation payload 带下来（`uow.submit_agent_input` 已经在 payload 里注入 `seq`，`uow.py:1252`，照抄这一手注入 `created_at`）。
  3. **每次调用新建** `ReActLoop(collaborator=AgentLoopCollaborator(limits=turn_limits), effects=self._effects, clock=self._clock, policy_fingerprint=self.policy_fingerprint)`。
- `build_agent_execution_driver`（`:386-428`）：`policy_payload`（`:408-418`）**保持只含 lifetime limits**，指纹不变。

### 必须保留的旧约束（本任务的核心是"什么都不动"）

- **`react_loop.py` 与 `termination.py` 一行不改**（`../investigation.md` §16 与 S1 的非功能表）。
- `policy_fingerprint` **不随 per-turn 限额变化**：否则 `kernel.py:2915-2917` 的预检会把所有已存在的 Run 判成 `runtime_policy_mismatch` 并终态化。
- `TerminationState` 的 totals 语义不变（reservation、永不重置）。
- 超限仍是 **失败的 Turn**（`agents/execution.py:248-266` 的既有分支），不是拒绝输入、不是终态。
- `AgentLimits` 的 lifetime 字段与 `termination_limits()`（`config.py:95-107`）不变。

### 验证栏（oracle）

新文件 `tests/agents/test_turn_limits.py`：

- **`test_per_turn_model_call_limit_fails_the_turn_not_the_agent`**：`AgentLimits(max_model_calls_per_turn=2)`，脚本 provider 让模型连续要求调工具 ≥3 次 → 该 Turn 结果 `state == failed` 且 `error["error_code"] == "react_max_turns_exceeded"`；`read_run(run_id).state is RunState.WAITING`；下一条输入正常 committed。
- **`test_per_turn_limits_do_not_reset_lifetime_totals`**：一轮失败后读 checkpoint，`provider_turns_reserved_total` **未被清零**且 ≥ 上一轮末值；再跑一轮，`provider_turn_ordinal_from` 严格大于上一轮的 `provider_turn_ordinal_to`。
- **`test_per_turn_tool_call_limit_is_enforced`**：`max_tool_calls_per_turn=1`，第二次工具批次 → `react_max_tool_calls_exceeded` 的失败 Turn。
- **`test_turn_deadline_uses_first_durable_admission_and_restart_does_not_extend_it`**：假时钟；`turn_deadline_seconds=1.0`，第一轮正常；构造一个跨重启的慢轮（stage 前中断 → 时钟推进 > deadline → 恢复重跑）→ 失败 `react_wall_clock_exceeded`，且断言判定用的是 `turn.created_at` 折算的偏移（对比：把 `turn_deadline_seconds` 调大后同场景成功）。
- **`test_policy_fingerprint_is_unchanged_by_per_turn_limits`**：两个 `AgentConfig`（per-turn 限额不同）建的 Agent，`driver.policy_fingerprint` 相同；两个 Run 的 start snapshot `policy_fingerprint` 也相同；`_drive` 不报 `runtime_policy_mismatch`。
- **`test_two_agents_with_different_per_turn_limits_do_not_interfere`**（并发安全）：`asyncio.gather` 让 A（限额 1）与 B（限额 8）同时跑；A 失败、B 成功；断言**没有**共享 collaborator 被互相改写（B 的失败 error 为空）。
- 回归：`tests/agents/test_agent_driver.py::test_budget_exceeded_is_a_failed_turn_not_a_dead_agent`（`:134`）一行不改仍绿。

**覆盖 AC**：AC10（次要，L3 闭环）；AC4 的前置（deadline 与恢复不延长）。

---

## T7 · UNKNOWN / 授权恢复的同 Turn 同动作身份（BA11 / L4 / D7）

### 改哪些文件/符号

**`src/simple_harness/agents/execution.py`**

- `start`：在派生 limits 之后、`self._loop.run(...)` 之前，用 `DurableReactCheckpoint`（已 import，`:58`；用法照抄 `_settle_failed_provider_turn` `:337-364`）
  1. `load_or_create` → 若 `state.phase in {"provider_reserved","tool_batch_reserved"}` 且 `state.active_turn_id not in (None, turn_id)` → 返回 `_binding_failure("base_agent_turn_identity_conflict", …)`（内核完整性故障，BA-v1.0 §1.3 允许 FAILED）；
  2. 否则 `state.active_turn_id != turn_id` 时 `cas(replace(state, active_turn_id=turn_id, last_observed_at=now))`。
     **注意**：`active_turn_id` 字段已存在（`termination.py:190`），只是 BaseAgent 路径从不写它（`react_loop.py:280` 的 `context_use_required` 恒为 False）。

**`src/simple_harness/agents/base.py`**

- 新增 `def turn_snapshot(self, turn_id) -> AgentTurnSnapshot`：`state`（沿用 `AgentTurnState`）+ `seq` + `input_id` + `blocked: bool` + `blocker: Mapping | None`（从 `uow.read_run(run_id)` 的 wait blocker 读）+ `provider_turn_ordinal_from`。
- `get_result`（`:121-133`）**合同不变**。
- `AgentTurnTimeout`（`contracts.py:70-77`）增 `receipt: AgentTurnReceipt | None`（BA-v1.0 §4.2「超时携带原回执」）；`wait_turn`（`base.py:141-154`）构造时带上；**旧的 `turn_id` 属性保留**，S1 用例不改。

**`src/simple_harness/agents/contracts.py`**：新增 `AgentTurnSnapshot`。

### 必须保留的旧约束

- S1 review F2 的 `_drive` BaseAgent 分支（`kernel.py:3086-3110`）**一行不改**：wait_blocker / authorization_wait 时不 ack、提交阻塞器后放弃租约。
- `AgentRuntimeReconciliation`（`agents/runtime.py:53-71`）不改。
- **checkpoint 仍是 run_id 单键 + `react.termination.v1` namespace**（D7 裁决"不分键"，理由见 investigation §9.2）；不新增 `ReactCheckpointPort` 方法（那会打 `public-api.json`）。
- `mark_agent_turn_running` 仍允许 `phase IN ('queued','running')`（`turns.py:275`）。

### 验证栏（oracle）

新文件 `tests/agents/test_turn_resume_identity.py`：

- **`test_unknown_provider_resumes_the_same_turn_with_the_same_request_identity`**（BA11 决定性）：transport 失败 → 阻塞器；断言 `base_agent_turns_v1` 该行 `phase == 'running'`、checkpoint `active_turn_id == turn_id`、`provider_request_id` 不变；`reconcile` 判 `CONFIRMED_NOT_STARTED` 后同一 Turn 续跑；断言 **provider invocation 行仍是 1 条**（同 `invocation_id`，不重复预留费用）、continuation 只被 ack 一次、`seq` 未变。
- **`test_authorization_wait_resumes_the_same_turn`**：授权端口先 pending 后 allow → 同 Turn 续跑；断言 effect 的 `effect_id`/`call_id` 与首次一致（`react_loop.py:1041-1053` 的确定性摘要），不产生第二个 effect 行。
- **`test_turn_snapshot_reports_blocked_with_a_reason`**：UNKNOWN 期间 `turn_snapshot(turn_id).blocked is True` 且 `blocker["kind"]` 非空；`get_result(turn_id) is None`（合同不变）；解除后 `blocked is False`、`state is COMMITTED`。
- **`test_resuming_a_different_turn_on_an_inflight_checkpoint_is_refused`**：人为把 checkpoint 置成 `phase='provider_reserved'` + `active_turn_id='other'`，驱动本 Turn → `DriverResult(FAILED, "base_agent_turn_identity_conflict")`，**不重复调用 provider**。
- **`test_checkpoint_stays_single_key_and_records_active_turn`**（D7 裁决的落地断言）：跑两轮后，`workflow_checkpoints`（或等价表）里该 run 只有 **1** 行 `react.termination.v1` 记录；`active_turn_id` 等于最后一轮的 `turn_id`；两轮的 `{run_id}:provider-turn:N` **N 严格递增、无重复**。
- **`test_ask_timeout_carries_the_receipt_and_caller_cancel_does_not_cancel_the_turn`**（BA09 加固）：`ask(timeout=…)` 超时 → `err.receipt.turn_id == err.turn_id` 且 `err.receipt.seq == 1`；另起一个 task 等 `wait_turn` 然后 `task.cancel()` → 底层 Turn 仍照常提交，`provider.calls == 1`。
- 回归：`tests/agents/test_agent_driver.py::test_unknown_provider_outcome_suspends_the_turn_and_resumes_after_reconcile`（`:219`）与 `tests/agents/test_build_agent_runtime.py::test_ask_timeout_does_not_cancel_the_turn`（`:178`）一行不改仍绿。

**覆盖 AC**：AC4（BA11）、AC3（BA09）。

---

## T8 · `cancel_turn` 最小合同 + Turn 失败不清空 session/费用（BA10）

### 改哪些文件/符号

**`src/simple_harness/execution/sqlite/base_agent/control.py`**：增
`cancel_turn(connection, *, command_id, agent_id, turn_id, request_hash, cancel_result_json, cancel_result_hash, lease_epoch, now)`：
一个事务内——写控制命令行（幂等 by `command_id`）→ `bump_control_generation` → 若 `phase in ('queued','running')` 则复用 `turns.stage_result`（`turns.py:285-323`）把取消结果 stage 成 `result_pending`；若 `phase in ('result_pending','committed','failed')` 则**原样返回既有事实，不改写**。

**`src/simple_harness/execution/sqlite/uow.py`**：薄 facade `cancel_agent_turn(...)`（一个事务，≤ 20 行）。

**`src/simple_harness/agents/execution.py`**

- `AgentExecutionDriver.__init__` 增 `turn_cancellations: MutableMapping[str, CancellationToken] | None`（由 `assemble_runtime` 注入一个共享 dict）。
- `start`：建 `token = CancellationToken()`，`turn_cancellations[turn_id] = token`，`finally` 删除；把 `tool_cancel=token` 传给 `self._loop.run(...)`（`react_loop.py:268` 已支持该参数，**不改 react_loop**）。
- 新增 `except asyncio.CancelledError` 分支（放在其它 except 之后、`response = result.response` 之前）：
  仅当 `token.cancelled and not cancel.is_cancelled` **且**该 Turn 的持久 `phase == 'result_pending'`（取消结果已 stage）时，读回已 stage 的结果构造 `AgentTurnOutcome` 并返回 `DriverResult(WAITING, {"base_agent_stage":"cancelled"}, agent_turn_outcome=…)`；**其余情况原样 `raise`**（不吞真正的任务取消）。

**`src/simple_harness/agents/runtime.py`**：`assemble_runtime`（`:83-184`）建 `turn_cancellations: dict[str, CancellationToken] = {}` 并注入 driver；`AgentRuntime` 增 `async def cancel_turn(self, agent_id, turn_id, *, command_id) -> AgentTurnReceipt`（写库 → 若 `turn_id` 在本进程表里则 `token.cancel()`）。
**`src/simple_harness/agents/base.py`**：增 `async def cancel_turn(self, turn_id, *, command_id)`。

### 必须保留的旧约束

- **不走内核 cancel**：不碰 `self._cancels[run_id]`（`kernel.py:2845`）、不触发 `CANCEL_REQUESTED`（否则 `kernel.py:3171-3180` 会留下悬挂 claim / 终态化）。
- `_commit_agent_turn`（`kernel.py:3298-3332`）**一行不改**：driver 回交的必须**就是已 stage 的那份取消结果**，`:3320-3322` 的 hash 相等分支自然成立，continuation 正常 ack、Run 保持 WAITING。
- 取消结果用 `state=failed` + `error_code="agent_turn_cancelled"`，**不扩 `AgentTurnState` / phase 枚举**（investigation S2-D1）。
- 已发生的 effect / provider 费用**不回滚、不改写**（`executor.py:288-293`，`../investigation.md` §16.8）。
- 跨进程只保证**最终一致**（对端在下一次提交尝试时收敛），文档与验收显式声明，不写假装通过的测试。

### 验证栏（oracle）

新文件 `tests/agents/test_cancel_turn.py`：

- **`test_cancel_turn_ends_the_turn_without_killing_the_agent`**：provider 阻塞时 `cancel_turn(turn_id, command_id="x1")` → `wait_turn` 返回 `state == failed` 且 `error["error_code"] == "agent_turn_cancelled"`；`read_run(run_id).state is RunState.WAITING`；continuation `ACKED`；下一条输入正常 committed。
- **`test_cancel_turn_is_idempotent_by_command_id`**：二次同 `command_id` → 同回执、`base_agent_control_commands_v1` 1 行、`control_generation` 只 +1。
- **`test_cancel_after_result_pending_does_not_rewrite_the_fact`**：在 stage 之后 cancel → `get_result` 返回**原始的成功结果**，不是取消结果；控制命令行仍被记录（有回执，标 `state="already_settled"`）。
- **`test_cancel_does_not_claim_to_undo_real_effects`**：先让一次工具 effect 落账再 cancel → `execution_effects` 行数与内容不变；取消结果的 `error` 里不含任何"已回滚"字样（断言 `"rolled_back" not in json`）。
- **`test_cancel_bumps_control_generation`**：`binding.control_generation` 由 0 → 1。
- **`test_turn_failure_keeps_session_and_cost`**（BA10）：第 1 轮成功 → 第 2 轮被 cancel（或触发 per-turn 超限）→ 断言 (a) `history()` 仍含第 1 轮结果，(b) Context revision 未回退、第 1 轮消息仍在（读 `SqliteContextPort.load(run_id)`），(c) provider 费用账本的 `committed_micros` 未减少，(d) 第 3 轮的 provider 请求**仍含**第 1 轮的对话，(e) `base_agent_turns_v1` 三行俱在、seq 1/2/3。

**覆盖 AC**：AC9（BA10）、AC11（cancel_turn，次要）。

---

## T9 · uow facade 下沉（L12）+ D7 记账 + 收尾

### 改哪些文件/符号

**`src/simple_harness/execution/sqlite/base_agent/turns.py`**：新增两个连接级函数（接受外层 connection 与注入的 helper，不自开事务）：

- `submit_input(connection, *, …, enqueue_continuation, object_json)` —— 承接 `uow.submit_agent_input`（`:1216-1273`）的主体；
- `finalize_turn(connection, *, …, require_claim, read_progress_receipt, insert_event, object_json)` —— 承接 `uow.commit_agent_turn_result_and_idle`（`:1426-1541`，**约 116 行**）的主体：结果行 + progress receipt + ack CAS + runs CAS + event。

**`src/simple_harness/execution/sqlite/uow.py`**：两个 facade 收缩为"开事务 + 租约 + fault 钩子 + 转发"（目标各 ≤ 25 行）；`_fault` 的 5 个注入点（`:1468`、`:1476`、`:1506`、`:1523`、`:1524`、`:1540`、`:1541`）**位置与名字一字不改**（S1 的 fault 注入测试依赖它们）。

**文档记账**：在 `slice-2/journal.md` 与 `../investigation.md` 的 D7 行旁注明「S2 裁决：不分键，替代物 = `active_turn_id`；理由见 `slice-2/investigation.md` §9.2」。

### 收尾清单

1. 固定回归命令跑一次，红集 ⊆ 基线；
2. `.venv/bin/mypy` 保持 **0 issues**；
3. `.venv/bin/python -m pytest -q tests/agents tests/execution/test_base_agent_schema_v10.py` 全绿；
4. `tests/unit/contracts/test_public_api.py` **不因本片变红**（S2 的新符号只从 `simple_harness.agents` 导出，**不加进根包 `__all__`**，`public-api.json` 与 `__version__` 不动）；
5. `grep -rn "simple_harness_memory" src/simple_harness/agents/` 无匹配；
6. `testcase/base-agent-slice-2/README.md` + `testcase/index.md` 同步；
7. journal（中文）含遗留清单与"触碰既有红"的说明。

### 必须保留的旧约束

- **纯重构**：`tests/agents/test_turn_finalize.py`（5 例，含 fault 注入的"全有或全无"）与 `tests/agents/test_base_agent_kernel_spike.py`（3 例）**一行不改全绿**——这是本任务唯一的判据。
- 事务纪律：base_agent helper 不自开事务、不 await 网络。
- 单一 transaction owner（`kernel.py:1500-1504`）。

### 验证栏（oracle）

- `tests/agents/test_turn_finalize.py` + `tests/agents/test_base_agent_kernel_spike.py`：**未修改** 且 8 passed。
- 新增 `tests/agents/test_uow_facade_shape.py`：用 `inspect.getsource` 断言 `SqliteExecutionUnitOfWork.commit_agent_turn_result_and_idle` 与 `.submit_agent_input` 的**函数体行数 ≤ 25**（去掉 docstring 与空行），且各自只出现一次 `with self.database.transaction()`。
- 全套：`.venv/bin/python -m pytest -q tests/agents tests/execution/test_base_agent_schema_v10.py` 全绿；固定回归命令红集 ⊆ 基线。

**覆盖 AC**：非功能表的"事务纪律/facade 形状"；AC12（D7 记账）。

---

## 附 A：任务 → AC 覆盖矩阵

| 任务 | AC1 BA12 | AC2 BA08 | AC3 BA09 | AC4 BA11 | AC5 BA03 | AC6 BA04 | AC7 BA02 | AC8 BA05 | AC9 BA10 | AC10 L3 | AC11 cancel | AC12 D7 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| T1 schema | ○ | | | | ○ | ○ | | | | | ○ | |
| T2 队列 | | ● | | | | | | | | ○ | | |
| T3 close | ● | | | | | | | | | | | |
| **T3.5 里程碑** | ● | ● | | | | | | | | | | |
| T4 owner | | | | | | | | ● | | | | |
| T5 批量 | | | | | ● | ● | ● | | | | | |
| T6 限额 | | | | ○ | | | | | ○ | ● | | |
| T7 恢复身份 | | | ● | ● | | | | | | | | ● |
| T8 cancel | | | | | | | | | ● | | ● | |
| T9 下沉/收尾 | | | | | | | | | | | | ● |

●=主要交付　○=前置/部分

## 附 B：本片新增的公开符号（只从 `simple_harness.agents` 导出）

`AgentPendingInputsExhausted`、`AgentClosingReceipt`、`AgentBatchRejected`、`AgentBatchIdentityConflict`、`AgentTurnSnapshot`；`AgentStatus` 增 `lifecycle_state`；`AgentTurnTimeout` 增 `receipt`；`AgentRuntimePorts` 增 `max_agents` / `max_batch_size`；`BaseAgent` 增 `close` / `cancel_turn` / `turn_snapshot`；`AgentRuntime` 增 `close_agent` / `cancel_turn`。
**根包 `simple_harness.__all__` 与 `public-api.json` 不动**（investigation §13.6）。

## 附 C：本片发现、但本片不改的问题（记入 journal 遗留）

1. **`RunClient.cancel` 对 BaseAgent Run 没有围栏**：`_cancel_run`（`kernel.py:3223-3262`）不查 `api_mode`，拿到 run_id 的旧代码能把 BaseAgent 打成 `CANCELLED`。S2 只**加一条暴露该缺口的测试**（标 `xfail` 或断言当前行为）并记遗留；正式围栏（在 `_cancel_run` 前加 `require_legacy_or_unmanaged`）会动内核 cancel 路径，**归 S5 与 BA33/BA37 一并处置**。
2. **L5 / F-BA-1**：已裁决并入本片（T10 / AC13）。
3. **首轮 turn deadline 略短**（S2-D6 的副作用）：checkpoint 在第一轮才诞生，`max(0, turn.created_at - started_at)` 首轮为 0。
4. **已存在的 v10 开发库将拒绝打开**（S2-D9）：升级器归 S5 BA37。

---

## T10 · F-BA-1：推理型响应 / 空正文的可持久化（主编排者裁决并入 S2）

> S1 遗留 L5。真实端点（DeepSeek `deepseek-v4-pro`）修 wire 后 4 次里 2 次主 Agent 最后一次综合调用落到 `provider_response_not_durable`；再跑 6 次未复现。`investigation.md` §15 已定位两条闸门：`provider_invocations.py:282-287`（隐藏 reasoning 块且无 opaque ref）与 `:244-259`（非白名单块 / 空文本且无 tool_calls）。根因候选：`AgentRuntimePorts` 没有 `continuation_capability`，`build_agent_execution_driver` 用默认 `REASONING_DISABLED`。

### 改哪些文件/符号
- `src/simple_harness/agents/ports.py`：新增 `continuation_capability: ProviderContinuationCapability = ProviderContinuationCapability()` 字段（校验类型）。
- `src/simple_harness/agents/runtime.py::assemble_runtime`：把它透传给 `build_agent_execution_driver(continuation_capability=...)`；`provider_binding_fingerprint` 随之变化属预期（新 runtime 才生效，已存在 Run 的 start snapshot 记录的是各自 fingerprint）。
- `src/simple_harness/agents/execution.py`：空正文且无 tool_calls 的最终响应 → **可见的失败 Turn**（`error_code="provider_empty_response"`，带 `finish_reason`），不让它走到 dispatch 的持久化校验才炸；Agent 存活。
- `tests/agents/test_delegation_e2e_real_provider.py` 的报告字段（`main_error/finish_reasons/empty_contents`）已在 bfd8a31 加上，作为真实端点证据通道。

### 必须保留的旧约束
- `provider_invocations.py` 的持久化校验**不放宽**（隐藏推理块不得进入持久状态是安全边界）；`react_loop.py` 不改。

### 验证栏（oracle）
新文件 `tests/agents/test_provider_response_durability.py`：
- `test_empty_final_response_is_a_visible_failed_turn`：mock provider 返回 `content=""`、无 tool_calls、`finish_reason="stop"` → Turn `state==failed`、`error.error_code=="provider_empty_response"`、Run 仍 WAITING、下一条输入正常。
- `test_reasoning_block_without_opaque_ref_is_a_failed_turn_not_a_dead_agent`：mock 返回含 `reasoning` 内容块且 `opaque_continuation_ref=None`（默认能力）→ Turn 失败（`provider_response_not_durable` 或等价 code）、Agent 存活；同一响应在 `continuation_capability=OPAQUE_REFERENCE` 且带 `opaque_continuation_ref` 时 → Turn 成功，且持久 Context 里**没有** reasoning 块原文。
- `test_ports_capability_reaches_the_driver`：`assemble_runtime(...).driver.provider_budget_fingerprint` 随 `continuation_capability` 变化。

**覆盖 AC**：AC13（次要）。
