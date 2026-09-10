plan-status: finalized-v2.1 (第 1 轮挑战 21 条已落实；closure 复核 5 条必改 + 6 条 P2 已按附 E 裁决，2026-09-10)

# Slice 1 实施计划（修订版 v2）：委派最短价值链

- 仓库：`/Users/taiwan/PROJECTS/SimplaHarness/simple-harness-sdk`，基线 main = `fd12e7dd`（0.7.10）
- 上游规范：Host `plans/taskSys2/base-agent-phase1-plan.zh-CN.md`（BA-v1.0）
- 代码事实：本目录上级的 `investigation.md`；**本 v2 里出现的每个行号都在 `fd12e7dd` 工作树上重新核对过**，与初稿不一致处以本文为准（见文末「行号勘误」）
- 切片划分：上级 `program.md`
- 验收：同目录 `acceptance.md`（v2）
- 挑战与裁决：同目录 `challenge-round-1.md`（21 条，全部已裁决；落点见文末「挑战裁决落点」）

## 0. 目标与不做的事

**目标**：主 Agent 是一个 BaseAgent 实例（执行身份永不终态）；它收到复杂任务后，通过受配额的 `agent.delegate` 创建一个**同样永不终态**的子 BaseAgent 完成任务，并把子 Agent 的结果可靠取回、用于最终回答；主 Agent 之后仍可接第二条输入；进程重启后已提交结果可读且不重生成。

**本片不做**（放后续切片，见 `program.md`）：完整批量幂等 `create_many`、`cancel_turn`/`close`、有界 Context 装配与 token 计数、混合召回、并发公平限流、故障注入矩阵、v9→v10 已有库迁移器、exact-wheel 发布。

**v2 相对初稿的三条结构性改变**（来自裁决 #1 / #2+#3+#16 / #13）：

1. **彻底不用 `child_terminal_receipts` 做父子结果通道**。子 Agent 永不终态 ⇒ `kernel.py:3195-3240` 那条回执永远不会产生。委派结果与 UNKNOWN 结算一律**直接读子 Agent 的 `base_agent_turn_results_v1` 行**，父侧用「条件等待 + 超时」。
2. **不新增 start mode、不动 `start_snapshot.py`、不动 `StartModeDriverRouter`**。改用 `driver_kind="base_agent"`：`kernel.py:2735` 的 `self._drivers[snapshot.driver_kind]` 取到的就是真实 driver，`:2736-2738` 的指纹预检语义自然成立。Agent 绑定放 `start.input`。
3. **价值验证里程碑前移到 T6.5**（内核级 spike，不依赖 `agents` 包与 delegate 工具），围栏任务 T4 后移到里程碑之后。

**执行顺序（10 个任务，初稿 T1 并入 T2）**：

```
T2 基线复核 + schema v10
  → T3 契约骨架（含 runtime/agent_turn.py）
  → T5 结果载体 + _drive 分流 + 同事务 ack + recover 优先 finalize
  → T6 AgentExecutionDriver
  → 【T6.5 价值验证里程碑：内核级 spike】
  → T4 API 围栏与公开入口
  → T7 build_agent_runtime / BaseAgent / AgentRuntime
  → T8 agent.delegate
  → T9 mock 端到端（完整价值链验收）
  → T10 真实 provider + 公共 API 快照
```

- **价值验证里程碑 = T6.5**：一条 pytest 命令证明「同一个执行身份可以反复交结果而不死、输入 continuation 被 ack、stage 后 kill 重启只提交一次」——这是主要矛盾两面里「不死的执行身份」那一面的最小证明，此时还没有 `agents` 包、没有 delegate 工具、没有围栏。
- **完整价值链验收 = T9**：主 Agent 委派 → 子 Agent 出结论 → 结果回主 Agent → 重启后旧结果可读 → 主 Agent 第二轮。

---

## T2 · 基线复核 + schema v10：BaseAgent 四张表

> 初稿的 T1（基线复核，无代码改动）并入本任务的第 0 步，使总任务数 = 10。

### 第 0 步：基线复核与锚点核对（无代码改动）

产出 `plans/2026-09-10-base-agent-phase1/slice-1/baseline-recheck.md`：

1. 跑固定回归命令（见下「验证栏」），逐条比对 `../baseline-known-failures.txt`（75 条）。
2. 核对本计划里每个 `文件:行号` 锚点仍成立（若 main 有前移，**先改本计划再动手**，不搜索替换套补丁 —— BA-v1.0 §9.3 末句）。
3. 列出 v9→v10 预计会再次改变结果的既有红夹具：`tests/integration/execution/test_schema_v1.py`、`tests/execution/test_command_ingress.py`、`tests/integration/execution/test_open_close.py`、`tests/integration/runtime/test_short_context_migration.py`、`tests/integration/runtime/test_context_use_migration.py`、`tests/execution/test_execution_schema_v3.py`、`tests/execution/test_audit_schema.py`、`tests/execution/test_stage_audit_schema.py`、`tests/integration/test_context_authority_storage_v015.py`。

### 新增模块

- `src/simple_harness/execution/sqlite/base_agent/__init__.py`
- `src/simple_harness/execution/sqlite/base_agent/schema.py` — 导出 `DDL: str`

### 改哪些文件/符号

**`src/simple_harness/execution/sqlite/schema.py`**（全文 92 行）

| 位置 | 改动 |
|---|---|
| `:12` | `SCHEMA_VERSION = 9` → `10` |
| `:62-66` | 现 `fresh_descriptor()` 的函数体原样搬成新函数 `legacy_v9_descriptor()`，返回 `Migration(9, "0009_fresh", legacy_v8_descriptor().sql + short_context_schema.DDL, sha256(...))` —— **checksum 必须与改动前的 `fresh_descriptor().checksum` 逐字节相等** |
| `:62-66` | 新 `fresh_descriptor()` = `Migration(10, "0010_fresh", legacy_v9_descriptor().sql + base_agent.schema.DDL, sha256(...))` |
| `:69-73` `accepted_descriptor_rows()` | 由 4 组扩为 8 组：**保留** `(nine,)`、`(seven,nine)`、`(eight,nine)`、`(seven,eight,nine)`（旧 v9 库仍可打开），**新增** `(ten,)`、`(nine,ten)`、`(eight,nine,ten)`、`(seven,nine,ten)`、`(seven,eight,nine,ten)` |
| `:86-92` `__all__` | **裁决 #20**：补 `accepted_descriptor_rows`、`legacy_v7_descriptor`、`legacy_v8_descriptor`、`legacy_v9_descriptor`（当前 `__all__` 只有 5 个名字，三个 legacy descriptor 与 accepted rows 都是被外部模块 import 的实际公共面） |
| `:76-79` `migrations()` / `:82-83` `initial_migration()` | 不改（只返回 fresh descriptor ⇒ 历史 migration 永不重放，"不重写历史 checksum" 结构上自动成立） |

**`src/simple_harness/execution/sqlite/short_context_migration.py`**（**裁决 #8**，该文件正式列入改动面）

这是 v7/v8 → **v9** 的迁移器，它内部所有 `fresh_descriptor()` 的语义都是"v9 目标描述符"。v10 之后 `fresh_descriptor()` 变成 v10，四处必须改成 `legacy_v9_descriptor()`：

| 位置 | 现状 | 改成 |
|---|---|---|
| `:20` | `from .schema import accepted_descriptor_rows, fresh_descriptor, legacy_v7_descriptor, legacy_v8_descriptor` | 追加 `legacy_v9_descriptor` |
| `:83`（在 `_validate`，`:69-95`） | `expected.executescript(fresh_descriptor().sql)` | `legacy_v9_descriptor().sql` |
| `:114`（在 `_receipt`） | `receipt.new_descriptor_hash != fresh_descriptor().checksum` | `legacy_v9_descriptor().checksum` |
| `:191`（在 `migrate_execution_to_v9`，`:132-`） | `fresh_descriptor().checksum, version,` | `legacy_v9_descriptor().checksum, version,` |
| `:194` | `descriptor = fresh_descriptor()` | `descriptor = legacy_v9_descriptor()` |

**不改动**（已核对，不受 v10 影响）：

- `src/simple_harness/execution/sqlite/database.py`：`:211` 新库直建用 `fresh_descriptor()`（= v10，正确）；`:235` 已经用 `accepted_descriptor_rows()`。
- `src/simple_harness/execution/sqlite/audit_schema.py:97-107` `_expected_objects`：虽然用 `fresh_descriptor().sql` 起底，但只取 `OBJECTS`（audit 对象名白名单）子集比对，v10 新增表不在白名单内 ⇒ 不受影响。
- `src/simple_harness/execution/sqlite/context_use_migration.py`：`:22` 把 `legacy_v8_descriptor` **别名**成 `fresh_descriptor`，与 `schema.fresh_descriptor` 无关，不受影响。

### v10 迁移内容（DDL）

本片只建 4 张表；S2–S4 的新表一律**追加进同一个 v10 DDL** 并在同片内重算 descriptor（`program.md` §3.3）。

| 表 | 关键字段与约束 |
|---|---|
| `base_agent_bindings_v1` | `agent_id TEXT PRIMARY KEY`、`run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id)`、`owner_scope TEXT NOT NULL`、`api_mode TEXT NOT NULL CHECK(api_mode='base_agent_v1')`、`role TEXT NOT NULL CHECK(role IN ('root','child'))`、`config_json TEXT NOT NULL`、`config_hash TEXT NOT NULL CHECK(length(config_hash)=64)`、`control_generation INTEGER NOT NULL DEFAULT 0`、`created_at REAL NOT NULL` |
| `base_agent_turns_v1` | `turn_id TEXT PRIMARY KEY`、`agent_id TEXT NOT NULL REFERENCES base_agent_bindings_v1(agent_id)`、`input_id TEXT NOT NULL`、`input_hash TEXT NOT NULL CHECK(length(input_hash)=64)`、`input_json TEXT NOT NULL`、`continuation_id TEXT`、`seq INTEGER NOT NULL`、`phase TEXT NOT NULL CHECK(phase IN ('queued','running','result_pending','committed','failed'))`、`staged_result_hash TEXT`、`staged_result_json TEXT`、`provider_turn_ordinal_from INTEGER`、`provider_turn_ordinal_to INTEGER`、`lease_epoch INTEGER`、`created_at REAL NOT NULL`、`UNIQUE(agent_id,input_id)`、`UNIQUE(agent_id,seq)` |
| `base_agent_turn_results_v1` | `turn_id TEXT PRIMARY KEY REFERENCES base_agent_turns_v1(turn_id)`、`agent_id TEXT NOT NULL`、`result_hash TEXT NOT NULL CHECK(length(result_hash)=64)`、`result_json TEXT NOT NULL`、`commit_receipt_id TEXT NOT NULL UNIQUE`、`usage_refs_json TEXT`、`committed_at REAL NOT NULL`、`INDEX(agent_id)` |
| `base_agent_delegations_v1` | **裁决 #21，缩为最小映射**：`delegation_id TEXT PRIMARY KEY`、`parent_agent_id TEXT NOT NULL`、`parent_turn_id TEXT NOT NULL REFERENCES base_agent_turns_v1(turn_id)`、`ordinal INTEGER NOT NULL`、`child_agent_id TEXT NOT NULL UNIQUE`、`ticket_id TEXT NOT NULL UNIQUE`、`state TEXT NOT NULL CHECK(state IN ('launched','settled','failed'))`、`UNIQUE(parent_turn_id, ordinal)` |

**#21 去掉的两列与其推导路径**（不再冗余存储）：
- `child_run_id` → `base_agent_bindings_v1.run_id WHERE agent_id = child_agent_id`
- `effect_id` → 既有 effect 账本；UNKNOWN 结算时反向路径是 `EffectRecord.arguments["delegation_id"]`（`execution/effects.py:278` 确认 `EffectRecord` 携带 `arguments`）→ 本表 → `child_agent_id` → bindings → results。

### 必须保留的旧约束

- 不重写任何历史 migration checksum；`legacy_v7/v8/v9_descriptor().checksum` 三个值一位不变。
- 旧表、旧 `run_id` 字段、旧 ledger 不动；新表初始为空（BA-v1.0 §11.3）。
- 已有 v9 库仍可 `Database.open`；**本片不实现 v9→v10 就地升级器**（S5 的 BA37），新库直建 v10。
- `migrate_execution_to_v9` 的对外行为不变：仍把库升到 **9**，仍写 to_version=9 的回执。

### 验证栏（oracle）

新文件 `tests/execution/test_base_agent_schema_v10.py`：

- `test_fresh_open_is_v10`：`Database.open(tmp/"a.db").schema_version == 10`；`sqlite_master` 含 4 张新表。
- `test_v9_database_still_opens`：用 `legacy_v9_descriptor()` 手工建只含 v9 行的库，`Database.open` 不抛，`schema_version == 9`。
- `test_descriptor_checksums_are_stable`：`legacy_v7/v8/v9_descriptor().checksum` 与硬编码期望值相等（把改动前 main 上算出的三个值抄进测试）。
- `test_schema_all_exports_descriptor_helpers`（**#20**）：`set(schema.__all__) >= {"accepted_descriptor_rows","legacy_v7_descriptor","legacy_v8_descriptor","legacy_v9_descriptor","fresh_descriptor","SCHEMA_VERSION","Migration","migrations","initial_migration"}`。
- `test_delegation_uniques`：同 `(parent_turn_id, ordinal)` 二次插入抛 `sqlite3.IntegrityError`；同 `child_agent_id` 二次插入同样抛。

新文件 `tests/execution/test_short_context_migration_still_targets_v9.py`（**#8**）：

- `test_existing_v8_library_migrates_to_nine_after_v10`：造一个 v8 库 → `migrate_execution_to_v9(...)` → 返回/回执 `to_version == 9`，`sdk_schema_migrations` 最大 version == 9，`Database.open` 不抛。
- `test_existing_v9_library_validates_against_legacy_v9_catalog`：对一个已是 v9 的库调 `_validate` 不抛 `ExecutionSchemaIncompatible`。

基线回归命令（本任务第 0 步与收尾各跑一次）：

```
.venv/bin/python -m pytest -q -p no:cacheprovider \
  --ignore=tests/integration/runtime/test_context_use_admission.py \
  --ignore=tests/integration/runtime/test_context_use_durable.py \
  --ignore=tests/integration/runtime/test_context_use_public_memory.py
```

- **第 0 步算对**：`60 failed / 15 errors / 1843 passed / 2 skipped`，`FAILED|ERROR` 行集与 `../baseline-known-failures.txt` `diff` 为空。
- **收尾算对**：新增的红全部落在上面列出的 schema/迁移夹具清单里，逐条在 journal 记原因。

**覆盖 AC**：DG03（幂等/配额的持久化基座）、BA30（结果表）、AC5 的前置。

---

## T3 · 契约骨架：`agents/` 包 + `runtime/agent_turn.py`

### 新增模块

- **`src/simple_harness/runtime/agent_turn.py`** —— **裁决 #10**：`AgentTurnOutcome` 定义在 runtime 层，**纯 dataclass、零 `agents` 依赖**，kernel 只 import 它，永不 import `simple_harness.agents`。

  ```
  @dataclass(frozen=True, slots=True)
  class AgentTurnOutcome:
      agent_id: str
      turn_id: str
      input_id: str
      input_hash: str            # 64 hex
      result_hash: str           # 64 hex = sha256(canonical_json(result_json))
      result_json: FrozenJsonValue   # 纯 JSON 对象，不是 AgentTurnResult
      usage_refs: tuple[str, ...] = ()
      provider_turn_ordinal_from: int | None = None
      provider_turn_ordinal_to: int | None = None
  ```

  **分层规则（写进模块 docstring）**：`agents` 层单向依赖 `runtime`；`AgentTurnResult`（富类型）由 `agents/contracts.py` 从 `result_json` 解码，内核只搬运 JSON。
- `src/simple_harness/agents/__init__.py`（只做本包内 re-export，**不在顶层 import sqlite/httpx**）
- `src/simple_harness/agents/config.py` — `AgentLimits`、`AgentConfig`、`config_hash()`
- `src/simple_harness/agents/contracts.py` — `AgentId`、`AgentTurnId`、`AgentTurnState`、`AgentTurnReceipt`、`AgentTurnResult`、`AgentDelegationResult`、错误类型
- `src/simple_harness/agents/codec.py` — `run_id_for_agent(AgentId) -> RunId` / `agent_id_for_run(RunId) -> AgentId`（首版同一 opaque 文本，BA-v1.0 §9.1）

### 内容约束（BA-v1.0 §4.1）

- `AgentLimits(max_pending_inputs, max_model_calls_per_turn, max_tool_calls_per_turn, turn_deadline_seconds, lifetime_cost_limit_micros | None, max_delegations_per_turn, delegation_wait_seconds)`
  —— 最后两个是本片新增：委派配额（默认 1）与**委派条件等待上限**（默认 60.0，裁决 #1 的超时口径）。
- `AgentConfig(name, instructions, model_profile_ref, tool_names: tuple[str,...], limits: AgentLimits, short_memory_mode='hybrid')`。**本片不放 `ContextPolicy`**（S3 引入），不造空壳字段。
- 全部 `@dataclass(frozen=True, slots=True)`；数字**拒绝 bool、负数、NaN**；限制文本与数组大小；`config_hash = sha256(canonical_json(...))`。
- **配置里不出现 `user_memory` / `mission_id` / `task_id` 任何必填字段**（BA-v1.0 §4.1 末句）。
- `AgentTurnResult(turn_id, state, public_output: Message | None, artifact_refs, usage_refs, delegation_count: int, error)` —— **不是** `ConversationTurnOutput`；`delegation_count` 供 LLM 变异 V5 判定。
- `AgentTurnResult.to_json()/from_json()` 与 `AgentTurnOutcome.result_json` 一一对应，`result_hash` 由 `sha256(canonical_json(to_json()))` 得到。

### 必须保留的旧约束

- `simple_harness.contracts` 的 `RunId` 语义不变；`canonical_json` 复用现成实现。
- `runtime/agent_turn.py` 不得 import `simple_harness.agents`、不得 import sqlite。

### 验证栏（oracle）

新文件 `tests/agents/test_config_contracts.py`：

- `test_limits_reject_bool_negative_nan`：`AgentLimits(max_model_calls_per_turn=True)` / `-1` / `float("nan")` 各抛 `ValueError`/`TypeError`。
- `test_config_hash_is_stable_and_order_insensitive`：同内容不同构造顺序 → 同 hash；改一个字符 → 不同 hash。
- `test_config_rejects_user_memory_fields`：`dataclasses.fields(AgentConfig)` 名字集合与冻结常量相等。
- `test_agent_id_run_id_roundtrip`：`agent_id_for_run(run_id_for_agent(a)) == a`，非 `AgentId` 入参抛 `TypeError`。
- **`test_agent_turn_outcome_has_no_agents_dependency`（#10，决定性于分层）**：
  `importlib.import_module("simple_harness.runtime.agent_turn")` 后断言 `sys.modules` 中**没有** `simple_harness.agents`；并对 `runtime/agent_turn.py` 与 `runtime/kernel.py` 源码做 `ast` 扫描，断言没有任何 `import simple_harness.agents` / `from simple_harness.agents import`。
- `test_result_hash_roundtrip`：`AgentTurnResult.from_json(outcome.result_json)` 还原后再 `to_json()` 与原 `result_json` 相等，hash 不变。

**覆盖 AC**：BA01（身份基础）、AC5 的前置。

---

## T5 · AgentTurn 结果载体 + `_drive` 分流 + 同事务 ack + recover 优先 finalize（核心）

### 改哪些文件/符号

**`src/simple_harness/runtime/kernel.py::DriverResult`（`:199-244`）**

- 新增字段 `agent_turn_outcome: AgentTurnOutcome | None = None`（类型来自 `runtime/agent_turn.py`）。
- `__post_init__` 追加排他校验，**照抄 `:224-243` 四组既有排他写法的风格**：
  - `agent_turn_outcome is not None` ⇒ `state is RunState.WAITING`，且 `conversation_output is None`、`wait_blocker is None`、`workflow_spawn_control is None`、`workflow_terminal is None`、`workflow_retry_wake is None`、`authorization_wait is None`，否则 `raise ValueError("agent turn outcome requires an exclusive WAITING result")`。
  - **旧规则 `:218-222`（`only COMPLETED may carry conversation_output`）一字不改。**

**`src/simple_harness/runtime/kernel.py::Runtime._drive`（`:2718-2995`）**

插入点精确到行（已核对）：`:2849` 是 `if result.workflow_spawn_control is not None:`，`:2852` 是 `current = self._uow.read_run(run_id)`，`:2855` 是 `if result.state is RunState.WAITING:`。

- 在 `:2852-2854`（读到 `current`、`current is None` 判断之后）与 `:2855` **之间**插入：

  ```
  if result.agent_turn_outcome is not None:
      await self._commit_agent_turn(
          current, result.agent_turn_outcome, result.payload, continuation_claim
      )
      return
  ```

  放在 `current` 之后是因为 finalize 需要 `current.version` 做 CAS；放在 `:2855` 之前是因为 outcome 恒为 WAITING，不能被 WAITING 分支的三条既有子路径（authorization_wait / 无 claim / 有 claim）截胡。

- **新增 `Runtime._commit_agent_turn(current, outcome, payload, continuation_claim)`**，两步：

  1. **stage**：若 `uow.read_agent_turn(outcome.turn_id).phase != 'result_pending'`，调 `uow.stage_agent_turn_result(turn_id=..., result_hash=..., result_json=..., input_id=..., input_hash=..., lease=self._leases[run_id], now=...)` —— 把 Turn 置 `phase='result_pending'`，把结果冻结进 `staged_result_hash/staged_result_json`。**这一步是独立短事务。**
  2. **finalize**：`uow.commit_agent_turn_result_and_idle(...)` —— **同一个事务**里做四件事（**裁决 #4**）：
     - 写 `base_agent_turn_results_v1`（`commit_receipt_id` UNIQUE 保证幂等）
     - `base_agent_turns_v1.phase` → `'committed'`
     - **ack 本轮 continuation**：`continuation_claim` 非 None 时按 `commit_runtime_state_and_ack_continuation`（`kernel.py:2916-2933`）的构成传 `receipt_id = f"{run_id}:progress:{claim.continuation_id}:{claim.claim_epoch}"`、`event_id = f"{run_id}:agent_turn:{outcome.turn_id}:committed"`
     - `commit_runtime_state(state=RunState.WAITING, expected_version=current.version, payload={"base_agent_stage":"idle","turn_id":outcome.turn_id,"agent_id":outcome.agent_id,**payload})` —— **Run 不进终态**
  3. 提交成功后照既有范式 `asyncio.create_task(self._reschedule(run_id))`（与 `:2934` 同）。

  **`continuation_claim is None` 的两种合法情形**（不得抛）：
  - `:2840-2847` 已把「durable continuation 已 ACKED」的 claim 置 None（崩溃重入）；
  - `recover` 走的 finalize-only 路径。
    此时 finalize 跳过 ack 段，只写结果 + 提交 WAITING，并**不**触发 `_reschedule`（由 recover 自己 `_schedule`）。

- **`Runtime._finalize_pending_agent_turn(run_id)`**：只做上面的第 2 步（无 stage、无 driver）。

**`src/simple_harness/runtime/kernel.py::Runtime.recover`（`:1877-1966`）**

- 在 `prior_ready is None` 分支里、**`:1912` 的 `run.parent_run_id is not None and run.driver_kind == WORKFLOW_DRIVER_KIND` 判断之前**插入（**裁决 #4 的恢复面 + BA-v1.0 §8.3 末句**）：

  ```
  if run.driver_kind == BASE_AGENT_DRIVER_KIND and self._uow.read_pending_agent_turn(run.run_id) is not None:
      activated = await self._activate(run.run_id)
      await self._finalize_pending_agent_turn(run.run_id)
      self._schedule(activated.run_id)
      continue
  ```

  —— **先 finalize 再调度，不重新进 driver、不重新调 provider**。
- 新增模块级常量 `BASE_AGENT_DRIVER_KIND = "base_agent"`（放 `kernel.py`，与 `ROOT_PROFILE_KEY`（`:147`）、`WORKFLOW_DRIVER_KIND` 同处）。

**`RuntimeUnitOfWork` Protocol（`kernel.py:530-532` 起）**：追加 `stage_agent_turn_result`、`commit_agent_turn_result_and_idle`、`read_agent_turn`、`read_pending_agent_turn`、`read_agent_turn_result`。

**新增 `src/simple_harness/execution/sqlite/base_agent/turns.py`**：`open_turn` / `claim_turn` / `stage_result` / `finalize_result` / `read_turn` / `read_pending_turn` / `read_result` 的事务 helper，**接受外层 connection、不自开事务**（范式见 `runtime/context.py:102`，裁决依据 `investigation.md` D15）。

**`src/simple_harness/execution/sqlite/uow.py`**：加 5 个薄 facade（各 ≤10 行，照 `:1140` / `:1149` / `:1154` 的写法），委派给上面的 helper。

### 必须保留的旧约束

- `conversation_output` 只能 COMPLETED（`kernel.py:218-222`）。
- `_drive` preflight 四道闸的位置与顺序不变：policy fingerprint（`:2736-2738`）、tool catalog（`:2751-2775`）、CANCEL_REQUESTED（`:2782`）、`claim_continuation`（`:2789`）。
- `:2840-2847` 的「已 ACKED 则清 claim」逻辑不动。
- lease / fence / 受控 handoff 不变；已发生 effect 结果不可改写（`tools/executor.py:288-293`）。
- 旧 `_terminalize` 路径（含 `:3195` 的父子终态分支）**一行不改**；本片只是永不走到它。
- 单一 transaction owner（`kernel.py:1500-1504`）；事务内不 `await` provider/网络。

### 验证栏（oracle）

新文件 `tests/agents/test_turn_finalize.py`：

- `test_driver_result_rejects_both_carriers`：`DriverResult(RunState.WAITING, agent_turn_outcome=X, conversation_output=Y)` 抛 `ValueError`；`DriverResult(RunState.COMPLETED, agent_turn_outcome=X)` 抛 `ValueError`；`DriverResult(RunState.WAITING, agent_turn_outcome=X, wait_blocker=B)` 抛 `ValueError`。
- `test_committed_turn_leaves_run_non_terminal`：finalize 后 `uow.read_run(run_id).state is RunState.WAITING`，`base_agent_turn_results_v1` 恰好 1 行。
- **`test_finalize_acks_the_input_continuation_in_the_same_transaction`（#4，决定性）**：用假 driver 造一次带 `agent_turn_outcome` 的返回；断言 (a) `uow.read_continuation(cid).state is ContinuationState.ACKED`，(b) 该 continuation 的 progress receipt id == `f"{run_id}:progress:{cid}:{epoch}"`，(c) 用 `fault` 钩子让该事务在写完 result 行之后抛错时，**continuation 仍是 CLAIMED、result 行也不存在**（同事务全有或全无）。
- **`test_result_pending_then_kill_then_recover_commits_once`（BA30/AC5，决定性）**：假 driver 在 stage 之后、finalize 之前抛 `KeyboardInterrupt`；重开 `Runtime` 调 `recover()`；断言 (a) provider 调用计数**未增加**，(b) `base_agent_turn_results_v1` 仍恰好 1 行且 `result_hash` 与 stage 时相同，(c) Run 版本单调递增，(d) driver 的 `start` **未被再次调用**（用 spy 计数）。
- `test_finalize_is_idempotent_by_receipt_id`：连调两次 `commit_agent_turn_result_and_idle` → 第二次返回原回执，不新增行、不重复 ack。
- 回归：`tests/runtime/test_conversation_continuation.py`、`tests/integration/runtime/test_kernel_start.py`、`tests/conformance/test_future_consumer_memory.py` 全绿不变。

**覆盖 AC**：AC5（决定性）、AC4（前置）。

---

## T6 · `AgentExecutionDriver`：复用 ReAct 核心，最终响应不再关 Agent

### 新增模块

- `src/simple_harness/agents/execution.py` — `AgentExecutionDriver` + `build_agent_execution_driver(...)`
- `src/simple_harness/agents/completion.py` — `stage_outcome()` / `finalize_outcome()` 的策略入口（把 `ReActResult` → `AgentTurnOutcome` 的换算集中一处）

### 改哪些文件/符号

**`src/simple_harness/runtime/drivers/react.py`**（只做提取，语义逐字不变）

- 把 `:287-306` 的最终结果构造提取为模块级私有 helper `_legacy_conversation_result(result) -> DriverResult`（含 `:292` 的 `RunState.COMPLETED` 与 `:294-305` 的 `ConversationTurnOutput`），`ReActDriver.start`（`:98`）末尾改成 `return _legacy_conversation_result(result)`。
- 把 `:234-291` 的五个异常分支（`UnknownToolError` / `ProviderInvocationUnknownError` / `TerminationBudgetExceeded` / `ToolAuthorizationPending` / `ToolEffectUnknownError`）提取为 `_react_failure_result(exc) -> DriverResult`，两个 driver 共用。
- `__all__`（`:450`）**不变**（两个 helper 都是私有名）。

**`src/simple_harness/agents/execution.py::AgentExecutionDriver.start`**

1. **无输入即返回 WAITING，不调 provider（裁决 #6）**：
   进入任何 ReAct 逻辑之前先判断——若 `invocation.continuations` 里**没有** `kind == "base_agent_input"` 的项，**且** `uow.read_pending_agent_turn(run_id) is None`（无 `result_pending` Turn），则立即
   `return DriverResult(RunState.WAITING, {"base_agent_stage": "idle"})`，
   **不构造 `ReActLoop`、不 `load` Context、不调 provider**。
   （必要性已核实：`Runtime._start_run`（`kernel.py:2534`）创建后立刻 `_activate` + `_schedule`，而 `_drive` 的 `:2790-2791` 只在 `run.state is RunState.WAITING and continuation_claim is None` 时提前 return；新建 Run 的状态不是 WAITING ⇒ driver **一定**会被调用一次。）
2. **意外 continuation kind 返回 FAILED 结果，不抛（裁决 #18）**：
   若 claim 到的 continuation kind 不在 `{"base_agent_input"}` 内（例如 `conversation_user`、`child_terminal`），
   `return DriverResult(RunState.FAILED, {"raw_failures": [{"error_code": "base_agent_unexpected_continuation", "source_kind": "runtime", "retriable": False, "observed_kind": <kind>}]})`，
   **不 `raise UnitOfWorkConflict`**（抛异常会走 `_drive` 的 `except UnitOfWorkConflict` 分支被静默吞掉 —— 这正是初稿的缺陷）。
3. 正常路径：复用 `ReActLoop`（`react_loop.py:245`）与同样的 `_tools`（`react.py:377-401`）/ tool_exposure / budget fingerprint 校验；把 `base_agent_input` 的消息 append 进 Context 作为本轮输入。
4. 正常结束：
   `return DriverResult(RunState.WAITING, {"response_present": True, "finish_reason": ...}, agent_turn_outcome=AgentTurnOutcome(agent_id, turn_id, input_id, input_hash, result_hash, result_json, ...))`
   —— `conversation_output` 恒为 `None`。
5. 异常路径：调 `_react_failure_result(exc)`；其中 `TerminationBudgetExceeded` 的 `RunState.FAILED` 需要**改写为**「Turn 失败但 Agent 不死」——本片的处理是：把它转成 `DriverResult(RunState.WAITING, {...}, agent_turn_outcome=<state=failed 的 outcome>)`，让失败也走 T5 的 finalize（`phase='failed'` + 结果行记 error）。其余四个分支原样透传。
6. `policy_fingerprint`：由 `build_agent_execution_driver(limits=..., budget_policy=..., estimator=...)` 自行派生，照 `react.py:425-440` 的写法但 `"protocol": "base-agent-hard-policy-v1"`（与 legacy react 的 `"react-hard-policy-v1"` **必然不同**），由 start snapshot 的 `policy_fingerprint` 在 `kernel.py:2736-2738` 把关。

### 必须保留的旧约束

- **本片不改 `react_loop.py`**：工具 gate、UNKNOWN 分支、`PROJECT_EFFECT` 授权闸（`:211-214`）、必要 Context-use 校验全部不动。
- `TerminationState` 的 totals 永不重置（`termination.py:155`）；沿用 Run 级 `provider_turns_reserved_total`（`react_loop.py:364` 的 request_id 格式**不改**），只在 `base_agent_turns_v1` 记每个 Turn 的序号区间（`investigation.md` D6）。
- `react.termination.v1` 的初始锚不被覆盖（`react_checkpoint.py:67-69`）；本片仍用同一 namespace，checkpoint payload 里带 `turn_id`；按 `(agent_id,turn_id)` 分键留 S2（D7）。
- legacy ReAct 的 COMPLETED 语义逐字不变。

### 验证栏（oracle）

新文件 `tests/agents/test_agent_driver.py`：

- **`test_create_without_input_returns_waiting_without_provider`（#6，决定性）**：起一个 base_agent Run 但不投任何输入；断言 (a) `DriverResult.state is RunState.WAITING`，(b) provider spy 调用计数 == 0，(c) `agent_turn_outcome is None`，(d) `base_agent_turns_v1` 0 行。
- **`test_unexpected_continuation_kind_returns_failed_result_not_raise`（#18，决定性）**：手工 `enqueue_continuation(payload={"kind":"child_terminal",...})` 到一个 base_agent Run；断言 driver **返回** `DriverResult(RunState.FAILED, ...)` 且 `payload["raw_failures"][0]["error_code"] == "base_agent_unexpected_continuation"`，并断言 `pytest.raises(UnitOfWorkConflict)` **不成立**（用 `try/except` 显式记录）。
- `test_agent_driver_never_emits_conversation_output`：任意成功轮次的 `DriverResult.conversation_output is None`。
- `test_agent_policy_fingerprint_differs_from_react`：`build_agent_execution_driver(...).policy_fingerprint != build_react_driver(...).policy_fingerprint`。
- 回归（legacy 提取后语义不变）：`tests/runtime/test_react_conversation_memory.py`、`tests/conformance/test_future_consumer_memory.py`、`tests/integration/runtime/test_kernel_start.py`、`tests/integration/runtime/test_react_sqlite_runtime.py` 全绿不变。

**覆盖 AC**：AC4（前置）、非功能「创建不驱动模型」。

---

## T6.5 · 【价值验证里程碑】内核级 spike：不死的执行身份

> **裁决 #13**：把价值验证从「第 8 个任务之后」提前到这里。本任务**不依赖 `agents` 包的运行时装配、不依赖 `agent.delegate`、不依赖 API 围栏**，只用裸 `build_runtime`。

### 新增文件

- `tests/agents/test_base_agent_kernel_spike.py`（**唯一交付物；无 `src/` 改动**）

### 装配（全部用既有公开入口）

```
Database.open(tmp/"execution.db") → SqliteExecutionUnitOfWork
profiles = {
    "agent.general": RuntimeProfile("agent.general", "base_agent"),   # root，见下方说明
    "agent.base":    RuntimeProfile("agent.base",    "base_agent"),   # 子 Agent 的 profile_key（T8 用）
}
drivers  = {"base_agent": build_agent_execution_driver(limits=..., budget_policy=..., estimator=None)}
ports    = RuntimePorts(..., agent_memory=None, conversation_memory_enabled=False,
                        context_staging=None, context_preparation_mode=None,
                        memory_dispatcher=None, context_provider=None)
runtime  = build_runtime(uow, profiles, drivers, ports)
```

> **关于 `RuntimeProfile("agent.base","base_agent")` 的落地口径（实测约束，必须照此写）**：
> `build_runtime`（`kernel.py:3459-3502`）**硬性要求** root profile 的 key 就是 `ROOT_PROFILE_KEY = "agent.general"`（`:147`、`:3473-3479`，`root_profile_key` 传别的值直接 `ValueError("root_profile_key is fixed to agent.general")`）。
> 因此 **root Agent 的 profile 条目必须键为 `"agent.general"`，但其 `driver_kind` 改成 `"base_agent"`**；`RuntimeProfile("agent.base","base_agent")` 作为**子 Agent 的 profile_key** 一并注册（子 Run 由 `ChildCoordinator.launch` 按 `launch_request["profile_key"]/["driver_kind"]` 建立，`child_coordinator.py:23-34` + `uow.py:4615-4619`，不查 `self._profiles`）。
> 裁决 #16 要求的「未注册 `base_agent` driver 时 `build_runtime` 拒绝，绝不落回 react」由 `kernel.py:3490-3493` 的 `raise ValueError("agent.general driver is not registered")` 天然成立。

Run 的建立与输入投递在本 spike 里走**内部路径**（公开入口是 T4 的事）：
- 建 Run：`await runtime._start_run(RunStart(..., input={"capability_snapshot": {"tools": []}, "base_agent_binding": {...}}))`
- 投输入：`uow.enqueue_continuation(continuation_id=..., run_id=..., payload={"kind":"base_agent_input", ...}, now=...)` + `await runtime._wake_continuation(run_id)`

### 三条里程碑断言

1. **`test_two_results_on_one_run_never_terminal`**：同一个 Run 连投两条 `base_agent_input`；断言 (a) 两次都产生 `base_agent_turn_results_v1` 行、`seq` 为 1 和 2，(b) 全程 `uow.read_run(run_id).state` **从未**进入 `COMPLETED/FAILED/CANCELLED`（用 run_events 全量回放校验，不只看末态），(c) 第二次 provider 请求的 messages 里包含第一次的 assistant 回答。
2. **`test_input_continuation_is_acked_and_rescheduled`（#4+#5）**：每轮结束后 `uow.read_continuation(cid).state is ContinuationState.ACKED`；且 finalize 后 Run 被重新调度（spy `Runtime._reschedule` 至少被调用一次），第二条输入无需外部再次唤醒即被消费。
3. **`test_stage_then_kill_then_recover_commits_once`（BA30）**：在 stage 之后 finalize 之前中断；新 `Runtime` `recover()` 后 provider 调用计数不变、结果行仍 1 行且 hash 不变、driver `start` 未被再次调用。

### 最小验证动作（一条命令）

```
.venv/bin/python -m pytest -q tests/agents/test_base_agent_kernel_spike.py
```

**算对**：三条全绿。**这是本片"主要矛盾的第一面已被解决"的判定点**；不绿则不得进入 T4 及之后。

**覆盖 AC**：AC4（决定性，主 Agent 面）、AC5（决定性）、非功能「创建不驱动模型」「提交后重新调度」。

---

## T4 · API 围栏与两个公开入口（**不动 start_snapshot.py、不动 StartModeDriverRouter**）

> **裁决 #2 / #3 / #16**：初稿的「新 start_mode + schema_version 8 + router 分支」三项**全部删除**。本任务缩水为：一个 namespace + 两个 kernel 公开入口。

### 改哪些文件/符号

**`src/simple_harness/execution/command_ingress.py`**

- 仿 `reserve_host_control_run`（`:186-195`）新增：
  ```
  def reserve_base_agent_run(self, *, run_id: str, intent_hash: str, now: float) -> None:
      self.reserve_legacy_run(namespace="base-agent/v1", projection_key_id="base-agent-v1",
                              run_id=run_id, intent_hash=intent_hash, now=now)
  ```
- 新增对称准入：
  ```
  def require_base_agent_run(self, run_id: str) -> None:
      row = ... SELECT namespace, api_mode FROM conversation_run_modes WHERE run_id=?
      if row is None or tuple(row) != ("base-agent/v1", RunApiMode.LEGACY.value):
          raise CommandError(CommandErrorCode.RUN_MODE_CONFLICT)
  ```
  —— **legacy 与 unmanaged（无行）都拒绝**，与 `require_legacy_or_unmanaged`（`:197-202`）互为镜像。
- `require_legacy_or_unmanaged`（`:197-202`）**一行不改**（它天然拒绝 `base-agent/v1`，BA06 的一半白送）。
- `RunApiMode`（`runtime/commands.py:34-36`）**不新增枚举值**（`investigation.md` D8：新增会打 `tests/unit/contracts/command-public-api.json` 快照）。

**`src/simple_harness/execution/sqlite/uow.py`**：仿 `:1149` / `:1154` 新增两个薄 facade `reserve_base_agent_run_mode(...)` / `require_base_agent_run_mode(...)`。

**`src/simple_harness/runtime/kernel.py`**

- `RuntimeUnitOfWork` Protocol（`:530` 起）追加这两个方法。
- `Runtime._reserve_base_agent_start(start: RunStart)`：照 `_reserve_legacy_start`（`:1575-1600`）的 intent_hash 构成，调 `reserve_base_agent_run_mode`。
- **公开入口 1 · `async def start_base_agent_run(self, start: RunStart) -> RunRecord`**：
  `self._require_started()` → `self._reserve_base_agent_start(start)` → `return await self._start_run(start)`。
  与 `RunClient.start_conversation` 的「先 `reserve_legacy_run_mode`（`:911-933`）再 `_start_run`（`:949-950`）」逐条同构。
  *（初稿裁决只点名了 signal 入口；创建路径同样需要一个不经过 legacy namespace 的公开入口，否则 `AgentRuntime.create` 只能调私有 `_start_run`。两个入口成对出现，是同一件围栏事，故合并在本任务。）*
- **公开入口 2 · `async def signal_base_agent_input(self, run_id: RunId, *, continuation_id, turn_id, input_id, input_hash, message, seq) -> AgentTurnReceiptRow`**（**裁决 #5**）：
  1. `self._require_started()`
  2. **准入**：`self._uow.require_base_agent_run_mode(_run_id(run_id))` —— 对 legacy / unmanaged run 抛 `CommandError(RUN_MODE_CONFLICT)`
  3. 读 run；终态则 `raise UnitOfWorkConflict("terminal Run rejects new continuations")`（与 `:1266-1267` 同措辞；本片 Run 永不终态，这条是防御）
  4. **一个短事务**：`self._uow.submit_agent_input(...)` —— 同事务写 `base_agent_turns_v1(phase='queued')` **与** `enqueue_continuation(payload={"kind":"base_agent_input", "turn_id":..., "input_id":..., "input_hash":..., "message":{...}})`（helper 接受外层 connection，事务内不 await）
  5. `asyncio.create_task(self._wake_continuation(_run_id(run_id)))`（`:2647-2653`，内部会 `_activate` + `_schedule`）
  6. 返回受理回执
- **`RunClient.signal`（`:1209-1228`）加一行防伪**：`:1219` 的 `if payload.get("kind") == "conversation_user"` 扩为 `if payload.get("kind") in {"conversation_user", "base_agent_input"}`，措辞 `raise ValueError("base_agent_input continuations require signal_base_agent_input")`。（该路径本已被 `_require_legacy_mode` 挡住 base-agent run；这行防的是"往 legacy run 里塞伪造 kind"。）
- 驱动契约标签（`:2803-2809`）：`type(driver) is AgentExecutionDriver` → `"sdk.base_agent.v1"`（与 `sdk.react.v2` / `sdk.workflow.v2` 并列）。

### 必须保留的旧约束（本任务的核心是"什么都不动"）

- **`src/simple_harness/runtime/start_snapshot.py` 零改动**：`:232` / `:324` 的 `start_mode ∈ {"ordinary","host_control"}`、`:351` 的 `schema_version 6/7`、`:407` 的 `from_json` 接受集合、`:449-452` 的 v7 拒绝 host_control、`:535-571` 的 `bind_start_snapshot` —— **一个字符都不改**。Agent 绑定走 `start.input`（下节）。
- **`src/simple_harness/runtime/drivers/start_mode.py` 零改动**：`StartModeDriverRouter`（`:16-35`）不加字段、不加分支。base_agent 根本不经过它。
- 旧幂等与 namespace identity（`command_ingress.py:137-183`）不变。
- v6/v7 快照字节完全不变。

### Agent 绑定放在 `start.input`

`StartSnapshot.input` 是自由 JSON 对象（`start_snapshot.py:346-348` 的 `to_json` 只要求它是 dict），与 `capability_snapshot` **并列**：

```json
{
  "capability_snapshot": {"tools": ["agent.delegate", "..."]},
  "base_agent_binding": {
    "agent_id": "<opaque>",
    "config_hash": "<64 hex>",
    "api_mode": "base_agent_v1",
    "role": "root",
    "owner_scope": "<opaque>"
  },
  "messages": []
}
```

- `AgentExecutionDriver` 从 `invocation.start.input["base_agent_binding"]` 读取 agent 身份；缺失或 `api_mode != "base_agent_v1"` → `DriverResult(FAILED, error_code="base_agent_binding_missing")`（不抛）。
- 子 Agent 的快照同构，`role="child"`，`capability_snapshot.tools` 不含 `"agent.delegate"`。

### 验证栏（oracle）

新文件 `tests/agents/test_api_mode_fence.py`：

- `test_legacy_start_conversation_rejects_base_agent_run`：先 `reserve_base_agent_run_mode(run_id="a1")`，再 `RunClient.start_conversation(..., run_id=RunId("a1"))` → `CommandError(RUN_MODE_CONFLICT)`。
- `test_signal_conversation_rejects_base_agent_run`：同上，`signal_conversation` 抛 `RUN_MODE_CONFLICT`。
- **`test_signal_base_agent_input_rejects_legacy_run`（#5 的对称面）**：对一个 `reserve_legacy_run_mode` 过的 run 调 `Runtime.signal_base_agent_input` → `CommandError(RUN_MODE_CONFLICT)`。
- **`test_signal_base_agent_input_rejects_unmanaged_run`**：对一个 `conversation_run_modes` 里没有行的 run 调用 → 同样 `RUN_MODE_CONFLICT`。
- `test_run_client_signal_refuses_forged_base_agent_input`：`RunClient.signal(payload={"kind":"base_agent_input"})` → `ValueError`。
- `test_ordinary_run_unaffected`：普通 `start_conversation` + `signal_conversation` 仍成功。
- **`test_build_runtime_rejects_unregistered_base_agent_driver`（#16，AC7 第二子条款）**：
  `build_runtime(uow, {"agent.general": RuntimeProfile("agent.general","base_agent")}, {"react": react_driver}, ports)` → `ValueError`（`kernel.py:3490-3493`），并断言返回值**不是**一个落回 react 的 Runtime。
- `test_v7_snapshot_bytes_unchanged`（回归，证明 #3 的"零改动"）：对固定 ordinary `RunStart`，`bind_start_snapshot(...).to_json()` 的 `canonical_json` 与硬编码期望串逐字节相等，且 `["schema_version"] == 7`。
- `test_start_snapshot_module_untouched`：对 `src/simple_harness/runtime/start_snapshot.py` 与 `src/simple_harness/runtime/drivers/start_mode.py` 算 sha256，与基线值相等（**本片的硬性零改动闸**）。
- 回归：`tests/integration/runtime/test_kernel_start.py`、`tests/execution/test_command_ingress.py` 相对基线无新红。

**覆盖 AC**：AC7。

---

## T7 · `build_agent_runtime` / `BaseAgent` / `AgentRuntime`

### 新增模块

- `src/simple_harness/agents/base.py` — `BaseAgent`（轻量 handle）
- `src/simple_harness/agents/runtime.py` — `AgentRuntime` + `build_agent_runtime(config, ports)`
- `src/simple_harness/agents/ports.py` — `AgentRuntimePorts`（provider / tool_executor / authorization / database_path / tool_names / delegate 配额与子配置模板等）
- `src/simple_harness/execution/sqlite/base_agent/commands.py` — `create_agent` / `submit_agent_input` 的事务 helper（接受外层 connection）

### 本片实现的 API 表面（BA-v1.0 §4.2/§4.3 的子集）

- `AgentRuntime.create(config, *, creation_key) -> BaseAgent`
- `AgentRuntime.create_many(configs, *, batch_key) -> tuple[BaseAgent, ...]` —— **最小实现**：顺序调 `create`，`creation_key = f"{batch_key}:{i}"`。完整批次幂等/冲突/整批回滚是 S2 的 BA02–BA04。
- `AgentRuntime.open(agent_id) -> BaseAgent`（只打开已存在的；owner 校验留 S2 的 BA05）
- `AgentRuntime.shutdown()`
- `BaseAgent.submit(input, *, input_id) -> AgentTurnReceipt` / `wait_turn(turn_id, *, timeout=None)` / `ask(text, *, input_id, timeout=None)` / `get_result(turn_id)` / `status()`
- **本片不导出** `cancel_turn` / `close` / `memory.*`（S2/S4）。

### `create` 与 `submit` 的路径（**裁决 #5**）

**`AgentRuntime.create`**：

1. 算 `agent_id` / `config_hash`；`run_id = run_id_for_agent(agent_id)`
2. 组 `RunStart(..., conversation=None, input={"capability_snapshot": {...}, "base_agent_binding": {...}})`
   —— `conversation=None` 合法：`kernel.py:2585-2587` 在无 conversation 时用 `user_id="harness-system"`
3. `await runtime.start_base_agent_run(start)`（T4 的公开入口，内部先 `reserve_base_agent_run_mode`）
4. 同一步之后写 `base_agent_bindings_v1` 行（`uow.create_agent_binding`，幂等 by `creation_key`）
5. **不投任何输入** ⇒ 由 T6 的 #6 保证 driver 不调 provider

**`BaseAgent.submit`**：

1. 算 `input_hash`、`seq`、`turn_id`、`continuation_id = f"{agent_id}:input:{input_id}"`
2. `await runtime.signal_base_agent_input(run_id, continuation_id=..., turn_id=..., input_id=..., input_hash=..., message=..., seq=...)`
   —— 该入口内部：准入（namespace）→ **一个短事务写 queued Turn + enqueue `base_agent_input`** → `_wake_continuation`（`_activate` + `_schedule`）
3. 返回 `AgentTurnReceipt(turn_id, input_id, seq, state=AgentTurnState.QUEUED)`
4. `wait_turn` = 条件等待 `base_agent_turn_results_v1` 行出现（带 `timeout`），超时**不取消**底层 Turn（BA09 的方向，本片只保证不取消，完整语义 S2）

### `build_agent_runtime` 的装配（照 `consumer_adapter.py:336-497`，**不是** production）

```
Database.open(ports.database_path) → SqliteExecutionUnitOfWork          （同 consumer_adapter:385-386）
ToolRegistry（含 agent.delegate 占位实例，晚绑 runtime）→ seal()
EffectExecutor / ProviderInvocationCoordinator / SqliteContextPort
RuntimePorts(..., agent_memory=None, conversation_memory_enabled=False,
             context_staging=None, context_preparation_mode=None,
             memory_dispatcher=None, context_provider=None)
profiles = {"agent.general": RuntimeProfile("agent.general", "base_agent"),
            "agent.base":    RuntimeProfile("agent.base",    "base_agent")}
drivers  = {"base_agent": build_agent_execution_driver(...)}
build_runtime(uow, profiles, drivers, ports) → Runtime
delegate_tool.bind(runtime) → AgentRuntime(runtime, ...)
```

- **不注册 `StartModeDriverRouter`**（裁决 #16：本片不碰它）。
- **显式不注入** `agent_memory`、不启用 `conversation_memory`、不创建 memory outbox 消费器（BA-v1.0 §9.4）。
- Agent 层任何模块**不得** `import simple_harness_memory`。

### 改哪些文件/符号

- `src/simple_harness/__init__.py`：`_RUNTIME_EXPORTS`（`:41-357`）追加 `BaseAgent / AgentRuntime / AgentConfig / AgentLimits / AgentTurnReceipt / AgentTurnResult / AgentTurnState / build_agent_runtime`；`__getattr__`（`:360-388`）加一个分支**从 `.agents` 惰性解析**（不要混进 `from . import runtime` 的路径）；`__all__`（`:395+`）同步。
- `src/simple_harness/runtime/__init__.py`：导出 `AgentTurnOutcome`（`DriverResult` 的字段类型必须可从 runtime 公共面拿到）。

### 必须保留的旧约束

- import 纯度：新分支**惰性 import**，根包 import 时不开数据库、不建连接、不起线程（`tests/artifact/test_import_purity.py`）。
- `__all__[0] == "__version__"`。
- `build_production_runtime` 的旧合同不变（`production.py:86/120-134` 仍强制 memory）；不传假 `MemoryManager` 绕检查。
- 事务纪律：`submit` 的事务里不 await provider/网络。

### 验证栏（oracle）

新文件 `tests/agents/test_build_agent_runtime.py`：

- **`test_no_memory_entrypoint_is_called`（BA38/AC8，必须）**：注入 spy，其 `recall_for_turn / release_recall / record_committed_turn` 一律 `raise AssertionError`；跑完一次完整 `ask` 后断言 spy 从未被调用，且 `runtime._ports.agent_memory is None`、`conversation_memory_enabled is False`、`memory_dispatcher is None`、`context_staging is None`；另断言 `sys.modules` 里没有 `simple_harness_memory`。
- **`test_create_returns_independent_agents`（BA01/AC6，必须）**：`create_many([cfg]*3, batch_key="b1")` → 3 个不同 `agent_id`、3 个不同 `run_id`；给 agent[0] 提交输入后，agent[1]/agent[2] 的 `SqliteContextPort.load(...)` 仍为 `revision == 0`。
- **`test_create_does_not_call_provider`（#6，非功能"创建不驱动模型"）**：创建 3 个 Agent 后 provider 调用计数 == 0，且 `base_agent_turns_v1` 0 行。
- **`test_submit_reschedules_the_run`（#5，非功能"提交后重新调度"）**：spy `Runtime._schedule`；`submit` 返回后**不做任何额外驱动**，`await agent.wait_turn(receipt.turn_id, timeout=5)` 成功返回；断言 `_schedule` 至少被调用一次、`_wake_continuation` 被调用一次。
- `test_import_purity_still_holds`：`tests/artifact/test_import_purity.py` 必须仍是既有状态（本片不得让它变红）。

**覆盖 AC**：AC6、AC8、AC4（主 Agent 两轮的驱动面）。

---

## T8 · `agent.delegate` 工具（单层 · 有配额 · 不递归 · 结果直读子结果行）

### 新增模块

- `src/simple_harness/agents/tools/__init__.py`
- `src/simple_harness/agents/tools/delegate.py` — `AgentDelegateTool`
- `src/simple_harness/execution/sqlite/base_agent/delegations.py` — 事务 helper（接受外层 connection）

### 输入 schema（closed，`additionalProperties: false`）

```json
{"type":"object",
 "additionalProperties": false,
 "properties":{
   "objective":{"type":"string","maxLength":8000},
   "role_hint":{"type":"string","enum":["worker"]},
   "delegation_id":{"type":"string","maxLength":128}},
 "required":["objective","delegation_id"]}
```

### 执行流程（全部走既有工具网关 / 授权 / effect 账本）

1. 从 `ToolContext.run_id` 反查 `base_agent_bindings_v1` 得 parent `agent_id`，从当前 checkpoint 得 `parent_turn_id`。
2. **配额闸**：`SELECT COUNT(*) FROM base_agent_delegations_v1 WHERE parent_turn_id=?` ≥ `AgentLimits.max_delegations_per_turn`（默认 1）→ 返回 `ToolResult(outcome=REJECTED, error_code="agent_delegation_quota_exceeded")`，**不抛异常**（模型要能看见并改策略）。
3. **幂等闸**：同 `delegation_id` 已存在 → 直接返回原 `child_agent_id`（含已 settled 时直接回放结果），不造第二个子 Agent、不第二次 launch。
4. 组装子 Agent 的 `AgentConfig`：
   - `tool_names = parent.tool_names - {"agent.delegate"}` ⇒ 子 `capability_snapshot.tools` **一定不含** delegate（结构性禁递归，`react.py:377-401` 决定每 Run 的工具集）
   - **`instructions` 来自委派配置模板**（`AgentRuntimePorts.child_instructions_template`，构造 `AgentDelegateTool` 时注入），**主 Agent 看不到、不能改**（**裁决 #14** 的 nonce 口径基础）
5. **子 Run 围栏 + 委派登记（同一事务，且早于子 Run 存在）** —— **裁决 #11**：
   `uow.reserve_child_base_agent_delegation(delegation_row=..., child_run_id=..., intent_hash=..., now=...)`：**一个事务**里写
   - `conversation_run_modes(run_id=child_run_id, namespace="base-agent/v1", api_mode=legacy, intent_hash=...)`
   - `base_agent_delegations_v1(..., state='launched')`
   - `base_agent_bindings_v1(child_agent_id, run_id=child_run_id, role='child', ...)`

   > **实现口径说明**：把模式行塞进 `claim_profile_launch_and_commit_child`（`uow.py:4585-4790`，200 行、自开事务）内部会重写该方法，风险不成比例。改为**先围栏后建 Run**，与 `RunClient.start_conversation` 的 `reserve_legacy_run_mode`（`kernel.py:911-933`）先于 `_start_run`（`:949-950`）是同一范式；`reserve_legacy_run` 本身不要求 `runs` 行已存在（`command_ingress.py:151-176`）。**保证不变**：子 Run 从存在的第一刻起就已被 `base-agent/v1` 围栏，不存在未围栏窗口。
6. `uow.issue_profile_launch_ticket(ProfileLaunchTicket(ticket_id, parent_run_id, profile_key="agent.base", catalog_generation, child_launch_fingerprint(launch)), now=...)`（`uow.py:4537-4583`；字段顺序见 `execution/contracts/children.py:72-79`）。
7. `await runtime.children.launch(ChildLaunchRequest(ProfileLaunchTicketRef(ticket_id, catalog_generation), command_id, child_run_id, request_id, AttachmentPolicy.DETACHED, launch_payload, child_start_snapshot))`（`child_coordinator.py:17-41`、`child_runs.py:39-59`）。
   - `launch_payload = {"profile_key": "agent.base", "driver_kind": "base_agent", "catalog_generation": <gen>, ...}`（`uow.py:4615-4619` 会逐项校验）
   - `child_start_snapshot` 是**普通 v7 快照**（`start_mode="ordinary"`，`schema_version=7`），`driver_kind="base_agent"`，`input` 带 `base_agent_binding{role:"child"}` 与不含 delegate 的 `capability_snapshot`
   - **`AttachmentPolicy.DETACHED` 的理由已变**：子 Agent 永不终态 ⇒ `kernel.py:3195-3240` 永不触发，本来就不会有 `child_terminal` continuation；DETACHED 是**纵深防御**——即使子 Run 因内核故障被 `_terminalize`，也只写回执、不给父 Run 塞 continuation（父 driver 会按 #18 返回 FAILED 结果）
8. **投递子 Agent 的唯一一条输入（一次委派 = 子一个 Turn，裁决 #15）**：
   `await runtime.signal_base_agent_input(child_run_id, ..., message=<objective>)` —— 子 Agent 在这**一个 AgentTurn 内**可以做多次模型/工具循环，但只产出一个结果行。
9. **条件等待子结果行（带超时，裁决 #1 + #17）**：
   轮询 `uow.read_agent_turn_result(child_turn_id)`（异步 sleep 退避，**不在事务内 await**，**不用 `runtime.wait_idle`** —— `wait_idle` 在第一次 drive 后就返回，不是完成信号）；
   - 上限 `AgentLimits.delegation_wait_seconds`
   - 超时 → 返回 `ToolResult(outcome=FAILED, error_code="agent_delegation_timeout", value={"child_agent_id":..., "delegation_id":..., "status":"pending"}, retryable=False)`，**不抛异常、不重复 launch**，`base_agent_delegations_v1.state` 保持 `'launched'`
10. 拿到结果行 → `state='settled'`；返回
    `ToolResult(outcome=SUCCEEDED, value={"child_agent_id":..., "delegation_id":..., "status":"settled", "result_hash":..., "result":{...}})`。
    结果体积超过固定上限时，`value["result"]` 只带摘要 + `result_ref`（原文留在 `base_agent_turn_results_v1` 可回读，**截断的是引用不是事实**，见变异 V4）。

### effect 分类与 UNKNOWN 结算

- **`ToolEffectClass.NON_PROJECT_EFFECT`**（`tools/runtime_catalog.py:65-69`），`route_requirement=FORBIDDEN`、`task_scope_requirement=FORBIDDEN`。
  **理由**：`PROJECT_EFFECT` 会在 `react_loop.py:211-214` 要求 `TaskExecutionEnvelope`，本阶段没有真实受控实现，BA-v1.0 §9.4 明令不得用无条件 ALLOW 补缺口。
- **UNKNOWN 恢复（DG04/AC9，裁决 #1）**：`ToolReconciliationPort.observe(effect)`（`tools/reconciliation.py:40-43`）的证据源是**子 Agent 的 `base_agent_turn_results_v1` 行**，路径为
  `EffectRecord.arguments["delegation_id"]`（`execution/effects.py:278`）→ `base_agent_delegations_v1` → `child_agent_id` → `base_agent_bindings_v1.run_id` → 该子 Agent 的结果行。
  有结果行 → `ReconciliationObservation(COMPLETED, evidence_ref=<result commit_receipt_id>, result=<ToolResult>)`；无 → `STILL_UNKNOWN`。**永不重新 launch。**
  **彻底不读 `child_terminal_receipts`**。

### 改哪些文件/符号

- `src/simple_harness/tools/contracts.py::ToolContext`（`:107-144`）：新增 `agent_delegate_context: AgentDelegateToolContext | None = None`，校验方式照 `workflow_spawn_context`（`:126-130`：惰性 import + isinstance）。
- `src/simple_harness/tools/__init__.py`：`__all__` 追加新符号（同步 `tests/conformance/tool-public-api.json`，见 T10）。
- `src/simple_harness/execution/sqlite/uow.py`：`reserve_child_base_agent_delegation`、`read_delegation`、`settle_delegation` 三个薄 facade。

### 必须保留的旧约束

- 已发生 effect 的真实结果不可篡改（`tools/executor.py:288-293`）。
- 父 Run 终态时 `issue_profile_launch_ticket` 拒发（`uow.py:4560-4561`）—— 本片父 Run 永不终态，正好相容。
- 工具 schema 闭合校验（`tools/contracts.py:65-105`）；`ToolResult` 的 outcome/error_code 约束（`:167-172`：REJECTED/FAILED 必须带 error_code）。
- `ToolRegistry.seal()`（`registry.py:88-113`）之后禁止再注册。

### 验证栏（oracle）

新文件 `tests/agents/test_delegate_tool.py`：

- **`test_delegate_creates_child_agent_and_returns_child_result_row`（DG01+DG02/AC1+AC2，决定性）**：断言 (a) `base_agent_bindings_v1` 多一行 `role='child'`，(b) 子 `run_id` 的 `parent_run_id == 父 run_id`，(c) 工具返回值 `value["result_hash"]` **等于**子 Agent `base_agent_turn_results_v1` 行的 `result_hash`，(d) 父 Context 里出现该 tool 消息，(e) **`child_terminal_receipts` 表 0 行**（证明未走旧通道）。
- **`test_child_run_is_mode_fenced_before_it_exists`（#11/AC7 child 面，决定性）**：断言子 `run_id` 在 `conversation_run_modes` 的 namespace 为 `base-agent/v1`；且对子 run 调 `RunClient.signal_conversation` 抛 `CommandError(RUN_MODE_CONFLICT)`；再断言"围栏行的 `created_at` ≤ `runs` 行的创建事件时间"。
- **`test_child_catalog_excludes_delegate`（DG03/AC3a，决定性）**：读子 start snapshot 的 `input["capability_snapshot"]["tools"]`，断言不含 `"agent.delegate"`。
- **`test_quota_rejects_second_delegation_in_same_turn`（DG03/AC3b，决定性）**：`max_delegations_per_turn=1` 时第二次调用返回 `ToolOutcome.REJECTED` + `error_code="agent_delegation_quota_exceeded"`，`base_agent_delegations_v1` 仍 1 行。
- **`test_same_delegation_id_is_idempotent`（DG03/AC3c）**：同 `delegation_id` 二次调用返回同一 `child_agent_id`，表仍 1 行，`claim_profile_launch_and_commit_child` 只发生一次。
- **`test_one_delegation_is_exactly_one_child_turn`（#15）**：一次委派后 `base_agent_turns_v1 WHERE agent_id=child` 恰好 **1** 行，`seq == 1`。
- **`test_delegate_unknown_reconciles_from_child_result_row`（DG04/AC9）**：注入让 effect 停在 UNKNOWN，重开 runtime 后 `observe` 读**子结果行**返回 `COMPLETED`，`evidence_ref` 指向该行的 `commit_receipt_id`；断言**没有**第二次 `claim_profile_launch_and_commit_child`，且断言实现里 `grep` 不到 `child_terminal`。
- **`test_delegate_timeout_returns_visible_result_not_raise`（#1 的超时面）**：把 `delegation_wait_seconds` 设成 0.05 并让子 Agent 的 provider 挂住；断言工具**返回** `ToolOutcome.FAILED` + `error_code="agent_delegation_timeout"`，父 AgentTurn 仍正常 committed，`base_agent_delegations_v1.state == 'launched'`，**没有**第二次 launch。
- `test_delegate_is_non_project_effect`：注册的 `ToolExecutionPolicy.effect_class is ToolEffectClass.NON_PROJECT_EFFECT`。
- LLM 变异（对应 acceptance V1–V5）：`test_unknown_delegation_id_is_rejected_not_created`、`test_malformed_arguments_never_create_child`、`test_oversize_objective_is_rejected_before_launch`、`test_oversize_child_result_returns_ref_not_truncated_fact`、`test_no_delegation_still_commits_a_valid_turn`。

**覆盖 AC**：AC1、AC2、AC3（决定性）、AC7（child 面）、AC9。

---

## T9 · mock 确定性端到端（完整价值链验收）

### 新增文件

`tests/agents/test_delegation_e2e_mock.py`

### 脚本化 provider —— **4 次调用**（裁决 #15：子 Agent 只跑一个 Turn）

参照 `tests/conformance/future_consumer_fixture.py:140-232` 的 `DeterministicProvider`。

| 调用序 | 归属 | 响应 |
|---|---|---|
| 1 | 主 Agent turn-1 | tool_call `agent.delegate{objective:"...", delegation_id:"d-1"}` |
| 2 | 子 Agent turn-1（唯一一轮） | 纯文本 `"结论：Y。验证码 <NONCE>"` —— NONCE 取自**子 Agent 的 instructions**（委派模板），主 Agent 侧不可见 |
| 3 | 主 Agent turn-1（工具结果回来后） | 纯文本 `"综合子 Agent 的结论：Y。验证码 <NONCE>"`（NONCE 只能来自工具返回值） |
| 4 | 重启后 · 主 Agent turn-2 | 纯文本 `"上次的结论是 Y"` |

**NONCE 口径（裁决 #14，mock 与 real_provider 共用）**：
- NONCE 由测试用 `secrets.token_hex(8)` 随机生成，**只写进**委派模板的 `child_instructions`；
- 断言前先校验 NONCE **不出现在**主 Agent 的 `instructions`、主 Agent 收到的任何输入文本、主 Agent 的工具描述/schema 中（`assert nonce not in canonical_json(...)`）；
- **AC2 判定 = 主 Agent 最终 `public_output` 含 NONCE**。这使"结果确实经由子 Agent 回传"成为可判定事实，而不是靠人读文本。

### 测试步骤与断言

1. `build_agent_runtime` → `AgentRuntime.create(main_config, creation_key="m-1")`；断言此刻 provider 调用计数 == 0（**创建不驱动模型**）。
2. `await main.ask("<复杂任务>", input_id="req-1")` → `AgentTurnResult`；断言 `NONCE in public_output.content`（**AC2**），且 `delegation_count == 1`。
3. 断言子 Agent 恰好 **1 个 AgentTurn**（`base_agent_turns_v1 WHERE agent_id=child` 只有 `seq==1`），且子 `run_id` 全程未进终态（**#15**）。
4. 断言 `base_agent_delegations_v1` 恰好 1 行、`state='settled'`；`child_terminal_receipts` 0 行（**#1**）。
5. `await runtime.shutdown()`；同进程内**新开** `Runtime`（同一 db 文件）→ `AgentRuntime.open(main_agent_id)`。
6. `main.get_result(turn_1_id)` 读到与步骤 2 完全相同的 `result_hash`，provider 调用计数**没有增加**（**AC5 的可读面**）。
7. `await main.ask("刚才的结论是什么？", input_id="req-2")` 成功返回，`seq == 2`，主 Run 全程未终态（**AC4**）。
8. 断言全程 `sys.modules` 无 `simple_harness_memory`（**AC8**）。
9. 断言 provider 总调用次数恰好 **4**（脚本用尽即证明没有多余模型调用）。

### 最小验证动作（一条命令）

```
.venv/bin/python -m pytest -q tests/agents/test_delegation_e2e_mock.py
```

**算对**：该文件全部通过，且固定回归命令的红集与 `../baseline-known-failures.txt` 的差异只出现在 T2/T10 显式声明的 schema/快照夹具上。

**覆盖 AC**：AC1、AC2、AC3、AC4、AC5、AC8（一次性贯通）。

---

## T10 · 真实 provider 演示 + 公共导出与快照合法更新

### 新增文件

- `examples/base_agent_delegation.py` — 真实模型演示脚本
- `tests/agents/test_delegation_e2e_real_provider.py` — 标记 `@pytest.mark.real_provider`
- **`tests/conftest.py`**（当前仓库根与 `tests/` 下都没有 conftest，本片新建）
- `tests/unit/contracts/public-api-0.7.10.json` — 冻结改动前的导出集

### `--run-real-provider` 开关（裁决 #19，**不改 addopts**）

`pyproject.toml`：`[tool.pytest.ini_options]` 的 `addopts`（`:82` `"-ra --strict-config --strict-markers"`）**一字不改**；`markers`（`:84-89`）**只追加一行** `"real_provider: end-to-end test that calls a real LLM provider"`（`--strict-markers` 要求注册）。

`tests/conftest.py`：

```
def pytest_addoption(parser):
    parser.addoption("--run-real-provider", action="store_true", default=False,
                     help="run tests marked real_provider against a real LLM endpoint")

def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-real-provider"):
        return
    skip = pytest.mark.skip(reason="needs --run-real-provider")
    for item in items:
        if "real_provider" in item.keywords:
            item.add_marker(skip)
```

- 默认（不带开关）：`real_provider` 用例 **skip**，不影响回归红集口径。
- 带开关且配置缺失：`pytest.skip` 而不是 fail。

### 真实 provider 接法

- `OpenAICompatibleProvider(client=httpx.AsyncClient(), base_url=<BASEURL>, model=<model>, secret=Secret(<APIKEY>))`（`providers/openai_compatible.py:56-113`）
- 配置来源优先级：环境变量 `SH_BASEURL` / `SH_APIKEY` / `SH_MODEL` → Host 仓库根 `.env` 的 `BASEURL` / `APIKEY` → 平台 user-data 的 `llm_runtime.json`（`backend/main.py:250`）。任一缺失即 `pytest.skip`。
- **key 绝不打印、绝不写进任何文件或断言消息**；`SecretRedactor`（`providers/redaction.py`）已在 provider 内生效。
- NONCE 口径与 T9 完全一致（同一 helper）。

### 公共 API 快照的合法更新（裁决 #7）

`tests/unit/contracts/test_public_api.py` —— **四处硬编码版本修正**（已逐行核对）：

| 行 | 现状 | 改成 |
|---|---|---|
| `:17` | `assert simple_harness.__version__ == snapshot["version"] == "0.7.8"` | `assert simple_harness.__version__ == snapshot["version"]` |
| `:41` | `assert previous["version"] == "0.7.3" and current["version"] == "0.7.8"` | `assert previous["version"] == "0.7.3" and current["version"] == simple_harness.__version__` |
| `:53` | `assert old["version"] == "0.7.4" and current["version"] == "0.7.8"` | `assert old["version"] == "0.7.4" and current["version"] == simple_harness.__version__` |
| `:64` | `assert old["version"] == "0.7.7" and current["version"] == "0.7.8"` | `assert old["version"] == "0.7.7" and current["version"] == simple_harness.__version__` |

- `:31-33` 的 `test_route_recovery_candidate_keeps_existing_public_exports`（比对两份**冻结**文件 0.7.1/0.7.2）**不改**。
- **这会顺手修掉一条既有红 `test_public_api_matches_frozen_snapshot`（`assert '0.7.10' == '0.7.8'`）以及另外三条同因红**；必须在 journal 里显式记为"触碰既有红"（`baseline.md` 的回归门口径）。
- 新增 `test_base_agent_successor_preserves_h0710_exports`：照 `:59-67` 的范式断言 `set(old[module]) <= set(current[module])`，`old = public-api-0.7.10.json`。
- `tests/unit/contracts/public-api.json`：更新 `simple_harness` / `simple_harness.runtime` 符号表 + `version` → 当前 `simple_harness.__version__`。
- `tests/conformance/tool-public-api.json`：追加 `agent.delegate` 相关新符号（`sorted(tools.__all__)` 必须完全相等，`test_tool_public_api.py:12-19`）。

### 必须保留的旧约束

- `PROTOCOL_VERSION == "1.0.0"` 不动（`tests/conformance/test_protocol_version.py:29`）。
- `tests/artifact/test_import_purity.py` 不得由本片变红。
- 只增不减：不删除任何既有公共符号。
- `addopts` 不变（#19）。

### 验证栏（oracle）

- `.venv/bin/python -m pytest -q tests/unit/contracts/test_public_api.py` 全绿（含新增 successor 测试）。
- `.venv/bin/python -m pytest -q tests/conformance/test_tool_public_api.py` 全绿。
- `.venv/bin/python -m pytest -q tests/agents/test_delegation_e2e_real_provider.py` **不带**开关时输出 `1 skipped`（证明默认口径不变）。
- `.venv/bin/python -m pytest -q --run-real-provider tests/agents/test_delegation_e2e_real_provider.py` 在配置就绪时通过（断言主 Agent 最终输出含 NONCE）；缺配置时 skip 而不是 fail。
- `examples/base_agent_delegation.py` 手工跑一次，输出里 grep 不到 key 的任何前缀（脚本自带一条 assert）。
- **报告分开**：mock 结果与 real_provider 结果分两份记录，不互相冒充（BA40 口径）。

**覆盖 AC**：AC8（exact 导出面）、AC10；AC1–AC3 的真实模型复现（作为证据，不作为 MUST 判定）。

---

## 附 A：任务 → AC 覆盖矩阵

| 任务 | AC1 DG01 | AC2 DG02 | AC3 DG03 | AC4 BA07 | AC5 BA30 | AC6 BA01 | AC7 BA06 | AC8 BA38 | AC9 DG04 | AC10 |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| T2 基线+schema v10 | | | ● | | ● | | | | | |
| T3 契约骨架 | | | | | ● | ● | | | | |
| T5 结果载体/finalize | | | | ● | **✔** | | | | | |
| T6 Agent driver | | | | ● | | | | | | |
| **T6.5 里程碑 spike** | | | | **✔** | **✔** | | | | | |
| T4 围栏与入口 | | | | | | | **✔** | | | |
| T7 装配 | | | | ● | | **✔** | | **✔** | | |
| T8 delegate 工具 | **✔** | **✔** | **✔** | | | | ✔(child) | | **✔** | |
| T9 mock 端到端 | ✔ | ✔ | ✔ | ✔ | ✔ | | | ✔ | | |
| T10 导出/真实 provider | ○ | ○ | ○ | | | | | ✔ | | **✔** |

`✔` = 该任务负责让此 AC 成立；`●` = 提供必要前置；`○` = 提供真实模型证据。

---

## 附 B：挑战裁决落点（21 条逐条）

> 与 `challenge-round-1.md` 的裁决表一一对应；每行给出"改在哪个任务 / 哪一段"。

| # | finding | 落点（任务 · 段落） |
|---|---|---|
| 1 | child-terminal-receipt-never-written | **T8 · 执行流程 9-10 + effect/UNKNOWN 结算段**（条件等待子 `base_agent_turn_results_v1` 行、超时返回 `agent_delegation_timeout`、`observe` 证据源改子结果行）；**T9 步骤 4**（断言 `child_terminal_receipts` 0 行）；acceptance AC2/AC9 |
| 2 | router-fingerprint-gate-fails-base-agent | **T4 · 标题与"必须保留的旧约束"**（`start_mode.py` 零改动）；**T6.5 · 装配段**（`driver_kind="base_agent"`，`kernel.py:2735-2738` 天然成立）；**T7 · build_agent_runtime 装配**（不注册 Router） |
| 3 | start-snapshot-v8-version-gates-incomplete | **T4 · "必须保留的旧约束" + "Agent 绑定放在 `start.input`"**（`start_snapshot.py` 零改动，`test_start_snapshot_module_untouched` 做硬闸，`test_v7_snapshot_bytes_unchanged` 保留） |
| 4 | continuation-ack-missing-in-agent-turn-commit | **T5 · `_commit_agent_turn` 第 2 步**（同事务 ack，receipt_id = `{run_id}:progress:{cid}:{epoch}`）+ **第 3 步 `_reschedule`**；oracle `test_finalize_acks_the_input_continuation_in_the_same_transaction` |
| 5 | no-input-delivery-path | **T4 · 公开入口 2 `Runtime.signal_base_agent_input`**（准入 + 同短事务写 queued Turn 与 continuation + `_wake_continuation`）；**T7 · `BaseAgent.submit`**；oracle `test_submit_reschedules_the_run` |
| 6 | create-drives-driver-immediately | **T6 · `AgentExecutionDriver.start` 第 1 条**（无输入即 WAITING，不进 ReActLoop、不调 provider）；oracle `test_create_without_input_returns_waiting_without_provider`、T7 的 `test_create_does_not_call_provider` |
| 7 | public-api-version-pin-breaks-three-green-tests | **T10 · 公共 API 快照的合法更新表**（`:17/:41/:53/:64` 四处改为与 `__version__` 比对，journal 记"触碰既有红"） |
| 8 | v10-breaks-v9-migration-validator | **T2 · `short_context_migration.py` 改动表**（`:20/:83/:114/:191/:194` 五处改用 `legacy_v9_descriptor`）；oracle `tests/execution/test_short_context_migration_still_targets_v9.py` |
| 9 | result-freeze-window-outside-oracle | **acceptance · AC5 措辞收窄为"stage 之后任意点"** + **非功能表新增"残留冻结窗口"行** + **journal §6 遗留**（归 S5 BA31） |
| 10 | agent-turn-outcome-layering-cycle | **T3 · 新增 `src/simple_harness/runtime/agent_turn.py`**（纯 dataclass、只搬 JSON、零 agents 依赖）；oracle `test_agent_turn_outcome_has_no_agents_dependency` |
| 11 | child-run-not-mode-fenced | **T8 · 执行流程第 5 步**（围栏行 + 委派行 + 子 binding 同事务，且早于子 Run 存在）；oracle `test_child_run_is_mode_fenced_before_it_exists`；acceptance AC7 覆盖 child |
| 12 | primary-contradiction-one-sided | **acceptance · 「主要矛盾」与「矛盾的主要方面」改写**；**program.md §0 与 S1 段改写**；**plan §0 的"v2 三条结构性改变"第 1 条**；T8 升为核心任务 |
| 13 | value-milestone-behind-eight-tasks | **新任务 T6.5（价值验证里程碑）** 插在 T6 之后、T4 之前；plan §0 的执行顺序图与"里程碑 = T6.5"声明 |
| 14 | ac2-not-decidable | **T9 · NONCE 口径段**（模板注入、不可见性前置校验、AC2 = 主 `public_output` 含 NONCE）；**T8 步骤 4**（子 instructions 来自委派模板）；**T10** 共用同一 helper；acceptance AC2 |
| 15 | child-two-turns-has-no-driver | **T8 · 执行流程第 8 步**（一次委派 = 子一个 AgentTurn）；oracle `test_one_delegation_is_exactly_one_child_turn`；**T9 的 4 次调用表**；acceptance AC4 只对主 Agent 要求两轮 |
| 16 | simpler-driver-kind-instead-of-start-mode | **T6.5 · 装配段的 profile/driver 口径说明**；**T4 · oracle `test_build_runtime_rejects_unregistered_base_agent_driver`**；acceptance AC7 第二子条款 |
| 17 | wait-idle-returns-on-first-drive | **T8 · 执行流程第 9 步**（显式写明"不用 `runtime.wait_idle` 作完成信号"） |
| 18 | uow-conflict-is-swallowed-not-raised | **T6 · `AgentExecutionDriver.start` 第 2 条**（返回 `DriverResult(FAILED, "base_agent_unexpected_continuation")`，不抛）；oracle `test_unexpected_continuation_kind_returns_failed_result_not_raise` |
| 19 | addopts-marker-override | **T10 · `--run-real-provider` 开关段**（addopts 不动，只注册 marker，conftest 加开关 + skip 钩子）；oracle "不带开关时 `1 skipped`" |
| 20 | schema-all-inconsistent | **T2 · `schema.py` 改动表 `:86-92` 行**（补 `accepted_descriptor_rows` + 三个 legacy descriptor）；oracle `test_schema_all_exports_descriptor_helpers` |
| 21 | four-tables-may-be-two | **T2 · v10 DDL 表格的 `base_agent_delegations_v1` 行 + 其下"去掉的两列与推导路径"说明**（仍 4 张表，delegations 缩为最小映射） |

**齐全性**：21/21 行均有具体落点（任务 + 段落 + 多数含 oracle 测试名）。

---

## 附 C：行号勘误（相对初稿 / `investigation.md`）

本 v2 在 `fd12e7dd` 上重新核对了全部锚点，以下几处与初稿/调查稿不一致，**以本表为准**：

| 位置 | 初稿/调查稿 | 实测 |
|---|---|---|
| `_drive` 结果分流 | 「插在 `:2852` 的 `if result.state is RunState.WAITING` 之前」 | `:2852` 是 `current = self._uow.read_run(run_id)`；WAITING 判断在 `:2855`。插入点是 `:2854` 与 `:2855` 之间 |
| `_drive` workflow_spawn_control 分支 | `:2845` | `:2849` |
| react 异常分支 | `react.py:233-285` | `react.py:234-291` |
| react `build_react_driver` 指纹 | `react.py:428-441` | `react.py:425-440`（`policy_fingerprint =` 在 `:438`） |
| `recover` 的 workflow-spawn 判断 | `:1911-1913` | `:1910-1914`（`run.parent_run_id is not None` 在 `:1912`） |
| `command_ingress.reserve_legacy_run` | `:137-183` | `:137-184`（`RUN_MODE_CONFLICT` 在 `:184`） |
| 工具执行策略类型名 | `EffectPolicy` | `ToolExecutionPolicy`（`tools/runtime_catalog.py:83-105`），由 `ExecutableToolRecord.execution_policy`（`:296-305`）派生 |
| `PROJECT_EFFECT` 授权闸 | `react_loop.py:199-215` | `react_loop.py:211-214` |
| `schema.py::__all__` | `:86-92` | `:86-92`（**当前只有 5 个名字**，缺三个 legacy descriptor 与 `accepted_descriptor_rows`） |
| `build_runtime` root profile | 「root profile 固定 `agent.general`」（未展开后果） | `kernel.py:3473-3479` **硬拒**非 `agent.general` 的 root key ⇒ root 条目必须键为 `"agent.general"`，只能改它的 `driver_kind`；`"agent.base"` 只能作为**子 Agent 的 profile_key** |

---

## 附 D：本片发现、但**本片不改**的问题（记入 journal §6 遗留）

1. **v10 之后旧 v9 库的只读审计通道会拒绝**：`execution/sqlite/database.py:145` 的只读审计 reader 断言 `reader.schema_version != SCHEMA_VERSION` 即 `RunAuditUnavailable`。v9 库仍能 `Database.open`（`accepted_descriptor_rows()` 接受），但 `audit_reader` 会拒。这是既有设计（审计只服务当前版本），v10 只是把界线从 9 挪到 10。归 S5 的 BA37 一并处置。
2. **`execution/sqlite/migrations/execution_v5_to_v6.py:156` 用 `fresh_descriptor()` 盖戳**：v5→v6 迁移完成后把 `sdk_schema_migrations` 单行改写成"当前 fresh 版本"，v10 之后会盖成 `(10,"0010_fresh",…)`，而库里并没有 v10 的表。这是**先于本片就存在**的错位（今天已经会盖成 9），本片不改、也不扩大；`tests/execution/test_execution_v5_to_v6_catalog_migration.py` 只走 `accepted_descriptor_rows()` 校验，因而不会变红。归 S5 BA37。
3. **`_drive` 的 `agent_turn_outcome` 分支与 `wait_blocker` 的组合被排他校验挡死**：意味着 UNKNOWN provider/工具的那一轮**不能**同时交结果——这是刻意的（不确定的动作不得被冻结成已完成结果），但也意味着 UNKNOWN 恢复期间该 AgentTurn 停在 `running`，直到 blocker 解除。完整语义是 S2 的 BA11。

---

## 附 E：closure 复核后的 v2.1 修订（**覆盖前文对应条款**，执行以本节为准）

> closure 复核（独立 Opus，只读）结论：21 条中 19 闭合、2 部分闭合；新发现 5 条必改（1 P0 + 4 P1）与 6 条 P2。裁决人：主编排者。原始 findings 见 `challenge-round-2-closure.md`。

| # | finding | 裁决 | 覆盖哪里 |
|---|---|---|---|
| E1 | child-binding-fk-precedes-run-row（P0） | **子 `base_agent_bindings_v1` 行改到 `claim_profile_launch_and_commit_child` 成功之后写**（`run_id` 外键指向已存在的子 runs 行）。预围栏事务只写：`conversation_run_modes` 行 + `base_agent_delegations_v1` 行（`state='reserved'`）。launch 成功后第二个短事务：写子 binding 行 + 委派行 `state='launched'`。`state` 枚举改为 `('reserved','launched','settled','failed')`。 | T2 DDL（delegations.state）、T8 步骤 5/7 |
| E2 | base-agent-fence-cannot-reuse-self-transaction-facade（P1） | 预围栏 helper `reserve_child_base_agent_delegation` **内联**三件事，不复用自开事务的 facade：(a) `CommandIngress._bind_namespace(connection, "base-agent/v1", "base-agent-v1", now)`（`command_ingress.py:558-574`，`conversation_run_modes.namespace` 是外键）；(b) `INSERT INTO conversation_run_modes(...)`（照 `:153-176` 的 run 不存在分支）；(c) `context_use_requirements.bind(connection, child_run_id, scope, "legacy_admission", child_run_id, intent_hash)`。helper 接受外层 connection。T4 的 `reserve_base_agent_run` facade 只给 root 用。 | T8 步骤 5、T4 |
| E3 | base-agent-failed-driver-result-terminalizes-run（P1） | **两分**：(a) `base_agent_unexpected_continuation`（claim 到非 `base_agent_input` 的 continuation）→ 返回 `DriverResult(RunState.WAITING, {"raw_failures":[{"error_code":"base_agent_unexpected_continuation", ...}]})` **不带 outcome**：`_drive` 现有 WAITING+claim 分支会 ack 该 continuation 并让 Run 留在 WAITING，Agent 不死、无 Turn 行（该 continuation 本来就不是 Turn）。(b) `base_agent_binding_missing`（start.input 缺绑定或 api_mode 不符）属**内核完整性故障**，按 BA-v1.0 §1.3 允许 `FAILED`：保留 `DriverResult(FAILED)`。oracle：`test_unexpected_continuation_kind_keeps_run_waiting_and_acks`（Run 仍 WAITING、continuation ACKED、`base_agent_turns_v1` 0 行、`child_terminal_receipts` 0 行）。T6 第 2 条与 T4 "Agent 绑定"段按此改。 | T6、T4 |
| E4 | delegation-row-precedes-launch-without-resume（P1） | T8 幂等闸改为**续做**：同 `delegation_id` 命中时按状态分流——`reserved`（无子 runs 行）→ 用同一 `ticket_id`/`child_run_id` 续做 launch（`claim_profile_launch_and_commit_child` 对 issued ticket 可重入、CLAIMED 且身份一致时幂等，`uow.py:4620-4649`）；`launched`（有子 runs 行、无结果行）→ 若子 Turn 未投递则投递，然后条件等待；`settled` → 回放结果。**只有 `settled` 才"直接返回"**。oracle：`test_reserved_delegation_is_resumed_not_poisoned`（在预围栏事务后、launch 前注入崩溃，重试同 `delegation_id` 后子 Agent 被创建且只 launch 一次）。 | T8 步骤 3 |
| E5 | child-start-snapshot-preflight-fields-unspecified（P1） | 子快照**不手工拼**：用 `bind_start_snapshot(RunStart(...), profile_key="agent.base", driver_kind="base_agent", policy_fingerprint=driver.policy_fingerprint)`，`RunStart` 的 `turn_id` 非空、`tool_catalog_generation = ports.tool_catalog.current_generation()`、`tool_catalog_fingerprint` 按 `_start_run` 同源；与 `_start_run`（`kernel.py:2574-2590`）逐字段同构。oracle：`test_child_snapshot_passes_kernel_preflight`（子 Run 首次 `_drive` 不因 `runtime_policy_mismatch`/`ToolCatalogStale` 终态）。 | T8 步骤 7 |
| E6 | queued-turn-lost-across-restart（P1） | **补恢复路径而不是只声明**：`AgentRuntime` 在 `build_agent_runtime` 完成 `runtime.__aenter__()` 后调用 `recover_pending_turns()`：查 `base_agent_turns_v1 WHERE phase IN ('queued','running','result_pending')`，对每个 run 调 `runtime._wake_continuation(run_id)`（WAITING+pending continuation 会被 `_drive` 重新认领；`result_pending` 由 T5 的 recover 分支先 finalize）。oracle：`test_queued_turn_survives_restart`（submit 后不驱动即 shutdown；新 runtime 后 `wait_turn` 拿到结果，provider 恰好调用 1 次）。T9 步骤 5–7 顺带覆盖。 | T7、acceptance 非功能表 |
| E7 | public-api-anchor-drift（P2） | T2 第 0 步核对时以实测为准：`test_public_api.py` :17/:42/:52/:62；`react.py __all__` :453；`react_loop.py` call_ordinal :735；`effects.py` :277。 | T2 步骤 0 |
| E8 | still-unknown-needs-evidence-ref（P2） | `STILL_UNKNOWN` 的 `evidence_ref = f"base_agent_delegation:{delegation_id}:pending"`（非空字符串）。 | T8 UNKNOWN 结算 |
| E9 | delegation-wait-exceeds-default-lease-ttl（P2） | `delegation_wait_seconds` 默认 **20.0**（< `lease_ttl_seconds` 30.0）；工具等待循环每次 sleep ≤ 0.2 s；条件等待期间租约由 kernel 心跳续期（`kernel.py:2666-2700`）。journal 记一句"长于租约的委派等待属 S2/S5"。 | T3 AgentLimits、T8 步骤 9 |
| E10 | orphan-context-use-requirement-is-immutable（P2） | 接受为已知遗留：预围栏后 launch 前崩溃留下的 `run_context_use_requirements` 孤儿行不可删；E4 的续做会复用同一 `child_run_id`，因此正常重试不产生新孤儿。记 journal §6。 | journal |
| E11 | base-agent-runtime-hijacks-every-root-run（P2） | AC7(d) `test_ordinary_run_unaffected` 建在**独立的** react Runtime（`build_consumer_runtime` 或既有 fixture）上，并在测试 docstring 注明原因。 | T4 oracle |
| E12 | recover-branch-skips-resolved-transition（P2） | T5 的 recover 新分支不用 `continue`：finalize 后落到既有的 `self._schedule(activated.run_id)` + `recovery.resolved` 发射（把分支写成设置 `activated` 后走公共收尾）。 | T5 |
