# 代码级调查：BA-v1.0 §9.3 修改表在当前 main 的真实位置

- 调查日期：2026-09-10
- 仓库：`/Users/taiwan/PROJECTS/SimplaHarness/simple-harness-sdk`
- main：`fd12e7dd7122786865ba61c19c48855a8eadcd8c`，`src/simple_harness/version.py::__version__ = "0.7.10"`
- 规范：Host 仓库 `plans/taskSys2/base-agent-phase1-plan.zh-CN.md`（BA-v1.0）
- 本文所有行号均来自上述 commit 的工作树；引用格式 `文件:行号`。
- 未修改任何 `src/` 或 `tests/` 文件；只跑了只读的 pytest 收集与执行。

---

## 0. 结论摘要（先看这一节）

1. **主要障碍不是"没有 Agent 类"，而是执行内核的单轮终态约束。** ReAct 的最终响应被硬编码映射为 `RunState.COMPLETED`（`src/simple_harness/runtime/drivers/react.py:292`），而 `DriverResult` 规定只有 COMPLETED 才能携带 `conversation_output`（`src/simple_harness/runtime/kernel.py:218-222`），同时 `RunClient.signal_conversation` 明确拒绝向终态 Run 追加输入（`src/simple_harness/runtime/kernel.py:1267`）。三者叠加 ⇒ **当前 SDK 无法在同一个执行身份上做"回答完 → 再接一条输入"**。现网的多轮 conformance 测试之所以能跑，是因为 fixture 把 provider 堵死让 Run 停在非终态（`tests/conformance/test_future_consumer_memory.py:355` 用 `block_provider=True`，`tests/conformance/future_consumer_fixture.py:215-232` 在 Run 未终态时排队 continuation）。
2. **"主 Agent 委派子 Agent"的底座已经存在，而且是 driver 无关的**：`Runtime.children`（`ChildCoordinator`，`src/simple_harness/runtime/child_coordinator.py:13-41`）→ `uow.claim_profile_launch_and_commit_child`（`src/simple_harness/execution/sqlite/uow.py:4585`）在一个事务里消费 ticket + 建子 Run + 写子 start snapshot，然后激活并调度。`tests/integration/runtime/test_runtime_h12.py:156-190` 已经证明可以用 `driver_kind="react"` 启动子 Run。
3. **`workflow_spawn` 不能复用**：`_CanonicalWorkflowSpawnRuntimeCoordinator.continue_ready` 在 `src/simple_harness/runtime/kernel.py:510` 把子 Run 的 `driver_kind` 硬编码为 `WORKFLOW_DRIVER_KIND`，且整条挂起/恢复协议绑死在 `workflow_spawn_child_wait_receipts` 表与 checkpoint `phase="child_wait"` 上（`src/simple_harness/execution/sqlite/uow.py:2376-2560`）。→ **结论：新写一个最小 `agent.delegate` 工具，复用 ChildCoordinator + 子终态回执，不复用 spawn 协议。**
4. **`build_agent_runtime` 应该建在 `build_runtime` / `build_consumer_runtime` 之上**，不能建在 `build_production_runtime` 之上：后者 `memory` 是必填字段并在 `src/simple_harness/runtime/production.py:120-131` 强制校验。
5. **基线不是全绿**：忽略 3 个 import `simple_harness_memory` 的文件后仍有 **60 failed / 15 errors / 1843 passed**（与 `plans/2026-09-10-base-agent-phase1/baseline-known-failures.txt` 的 75 条逐条一致）。schema v9→v10 会再动其中的迁移/schema 夹具，必须在计划里显式说明。

---

## 1. `runtime/kernel.py`

| 符号 | 位置 | 事实 |
|---|---|---|
| `RuntimeProfile` | `runtime/kernel.py:160-170` | `(profile_key, driver_kind)`，两者都必须非空字符串 |
| `DriverInvocation` | `runtime/kernel.py:172-197` | 携带 `run / start / execution_lease / run_fence / services / continuations / workflow_*`；新模式要塞 AgentTurn binding 只能加字段或走 `start` 快照 |
| `DriverResult` | `runtime/kernel.py:199-244` | 状态只允许 `WAITING/COMPLETED/FAILED`（`:213-214`） |
| **conversation_output 只能绑 COMPLETED** | **`runtime/kernel.py:218-222`** | `if self.conversation_output is not None: ... if state is not RunState.COMPLETED: raise ValueError("only COMPLETED may carry conversation_output")` |
| 互斥载体既有先例 | `runtime/kernel.py:224-243` | `workflow_spawn_control` / `workflow_terminal` / `workflow_retry_wake` / `authorization_wait` 各自都有"独占 WAITING"或"独占终态"的排他校验；新增 `agent_turn_outcome` 应照抄这套写法 |
| `RuntimeDriver` Protocol | `runtime/kernel.py:246-254` | `async def start(invocation, *, context, cancel) -> DriverResult` |
| `RuntimeServices` | `runtime/kernel.py:350-368` | 驱动可见的全部服务（provider/tools/context/react_checkpoint/tool_catalog/workflow_spawn/run_context_authority/...） |
| `RuntimePorts` | `runtime/kernel.py:636-720` | `agent_memory`、`context_provider`、`memory_dispatcher`、`context_staging` **全部可选**（`:658-661`）；`conversation_memory_enabled` 默认 `False`（`:655`）。校验只在启用 memory 时才要求 staging/preparation mode（`:700-706`） |
| `Runtime.__init__` | `runtime/kernel.py:1465-1573` | 注入点：`self._services`（`:1529`）、`self._drivers`（`:1524`）、`self.client = RunClient(self)`（`:1572`）、`self.children = ChildCoordinator(self)`（`:1573`）。Agent lifecycle service 最自然的位置是这里加一个 `self.agents = ...`，并在 `_start_once` 注册 pump |
| `Runtime.start` / `_start_once` | `runtime/kernel.py:1815-1876` | 启动顺序：`_start_hooks` → recover → pumps |
| `Runtime.recover` | `runtime/kernel.py:1877-1966` | 恢复分流：先读 `read_spawn_ready_activation`（`:1904`），再判断"workflow spawn 子 Run"（`:1911-1913`），否则 `_activate`。**新模式的分流点就在 `:1911` 这个 if 之前/之后加一个 `base_agent_v1` 分支** |
| `Runtime._drive` | `runtime/kernel.py:2718-2995` | preflight（policy_fingerprint `:2737`、tool catalog `:2751`、cancel `:2782`、claim_continuation `:2789`）→ StartModeDriverRouter.select（`:2802`）→ driver.start（`:2820`）→ 结果分流：`workflow_spawn_control`（`:2845`）→ WAITING 分支（`:2852-2925`）→ **else `self._terminalize(..., conversation_output=result.conversation_output)`（`:2936-2943`）**。新的 AgentTurnResult 分支必须插在 `:2852` 的 `if result.state is RunState.WAITING` 之前 |
| `Runtime._terminalize` | `runtime/kernel.py:3089-3294` | `:3195` `if run.parent_run_id is not None:` → 读 `read_child_terminal_result_for_run`（`:3198`）→ DETACHED 走 `commit_detached_child_terminal`（`:3217`），否则 `finalize_child_and_enqueue_parent_signal`（`:3229`）。**这段与 driver_kind 无关，ReAct 子 Run 同样生效** |
| `_drain_child_signals_once` | `runtime/kernel.py:2447-2464` | 从 `ChildSignalRuntime.reconcile_all` 拿到 ack 结果后激活/调度父 Run；调用点在 `_drain_resolved_waits_once`（`:2417`），由 `_start_once`（`:1842`）、`reconcile`（`:1973`）、`_wake_drain`（`:2063`）驱动 |
| `RunClient.start_conversation` | `runtime/kernel.py:872-990` | 预留 legacy run mode（`:911-932`）；无 memory 时直接 `_start_run`（`:949-950`） |
| **`RunClient.signal_conversation`** | **`runtime/kernel.py:1229-1307`** | `:1244` 要求 legacy mode；**`:1266-1267` `if run.state in {COMPLETED, FAILED, CANCELLED}: raise UnitOfWorkConflict("terminal Run rejects new continuations")`**；`:1299-1305` 入队 payload `{"kind": "conversation_user", ...}` |
| `RunClient.signal` | `runtime/kernel.py:1209-1228` | `:1219` 拒绝手工伪造 `conversation_user` continuation |
| `build_runtime` | `runtime/kernel.py:3459-3503` | root profile 固定 `agent.general`；`workflow` 是保留 driver key（`:3480`） |

### 1.1 启动/关闭 pump 清单

`runtime/kernel.py:1547-1560` 定义了 `_wake_drain_task / _command_pump_task / _delivery_pump_task / _memory_pump_task`；`_stop_background_tasks` 在 `runtime/kernel.py:2039-2058`。新增 Agent 队列 pump 应与这些同生命周期。

---

## 2. `runtime/drivers/react.py` 与 `react_loop.py`

### 2.1 最终响应 → COMPLETED（必须改的那一处）

```
src/simple_harness/runtime/drivers/react.py:286-306
        response_message = result.response.message
        return DriverResult(
            RunState.COMPLETED,                      # ← :292
            {"response_present": True, "finish_reason": ...},
            conversation_output=(ConversationTurnOutput(...)),   # ← :297-306
        )
```

这段就是 §9.3 要求"提取为内部 helper，新 adapter 返回本轮结果而不关 Agent"的目标。前面的异常分支（`react.py:233-285`）分别产出 `WAITING + raw_failures` / `wait_blocker` / `authorization_wait` / `FAILED`，这些在新模式下应原样保留。

### 2.2 continuation 处理只认 `conversation_user`

`react.py:150-155`：
```
for continuation in invocation.continuations:
    continuation_payload = thaw_json(continuation.payload)
    if not isinstance(continuation_payload, dict) or (
        continuation_payload.get("kind") != "conversation_user"
    ):
        continue
```
**⇒ `child_terminal` 类型的 continuation（`runtime/child_signal_runtime.py:77`）会被 ReAct 静默跳过，然后在 `_drive` 里被 ack 掉（`kernel.py:2903-2923`），子 Agent 的结果就丢了。** 这是 Slice 1 必须补的缺口（要么改用 DETACHED policy 不产生 signal，要么在新 driver 里显式消费）。

### 2.3 checkpoint / identity / Context port 注入点

| 内容 | 位置 |
|---|---|
| checkpoint 载入 | `react_loop.py:273-277` `DurableReactCheckpoint(services.react_checkpoint).load_or_create(value.run_id, execution_lease, ...)` |
| Context authority 插槽 | `react_loop.py:281-317`（`context_use_required` / `context_use_authority_scope` / `active_turn_id` / `active_continuation_id`） |
| Context 首次装载 | `react_loop.py:318-327` `services.context.load` / `append(..., f"{run_id}:context:initial", initial_messages)` |
| **provider request_id 构成** | **`react_loop.py:363-365` `f"{run_id}:provider-turn:{state.provider_turns_reserved_total}"`** |
| Host Context snapshot 授权 | `react_loop.py:390-420` `services.run_context_authority.prepare_snapshot(RunContextAuthorityRequest(...))` |
| 响应冻结（可挂 RESULT_PENDING 的位置） | `react_loop.py:561-575`（`phase="response_reserved"` + `provider_response_snapshot/digest` CAS） |
| **最终返回点** | **`react_loop.py:717-732`**：把 `phase` 复位为 `"ready"`、清空 request/response 快照，然后 `return ReActResult(response, state)` |
| 工具 gate / effect 身份 | `react_loop.py:734-760` `_internal_effect_identity(run_id, provider_turns_reserved_total, raw_call_id, call_ordinal)` |

**RESULT_PENDING 应挂在哪：** `react_loop.py:717` 之前（把"最终响应已冻结"的事实固化成 AgentTurn 的 RESULT_PENDING 行），或在新 driver 里包住 `react_loop.py:732` 的返回值。前者更贴规范 §8.3（"冻结结果、hash、checkpoint、输入绑定"在同一 checkpoint CAS 里），后者改动面小。计划取后者 + 一次独立的 stage 事务。

### 2.4 `build_react_driver` 与工具集

`react.py:404-450`：`policy_fingerprint` 由 limits + provider binding 派生（`:428-441`），会与 start snapshot 的 `policy_fingerprint` 在 `kernel.py:2737` 比对。新 Agent driver 若换 limits，必须自带自己的 fingerprint。

`react.py:377-401` `_tools(...)`：**每个 Run 的工具集来自 `start.input["capability_snapshot"]["tools"]`**，并在有 catalog fingerprint 时对冻结快照做子集校验（`:394-399`）。
**⇒ 子 Agent 的 `capability_snapshot.tools` 不含 `agent.delegate` 即可从结构上禁止递归委派，无需运行时特判。**

---

## 3. `runtime/context.py`：全量快照重写

- `ContextPort` Protocol：`runtime/context.py:41-52`（`load` / `append(run_id, lease, expected_revision, append_id, entries)`）
- `SqliteContextPort.load`：`runtime/context.py:67-77`，读 `workflow_checkpoints` 里 `namespace='react.context.v1'`（常量在 `:25`）的最新版本
- **全量重写证据**：`runtime/context.py:170-174`
  ```
  next_payload = {
      "revision": next_revision,
      "messages": [*messages, *append_payload],     # ← :172 整份 messages 重写
      "append_receipts": {**receipts, append_id: append_hash},   # ← :173 receipts 也整份重写
  }
  ```
  每次 append 都插一行新的完整 checkpoint（`:177-194`）。⇒ 增长是 O(n²)，这正是 BA19 要解决的问题（Slice 3）。
- 幂等：`append_id` → hash 回执（`:155-162`）；CAS：`revision != expected_revision` 冲突（`:163-165`）；租约校验：`:128-141`。

---

## 4. `execution/context_authority.py`

| 符号 | 位置 |
|---|---|
| `ContextRouteState` / `ContextRouteOrigin` / `ContextRouteReceipt` | `:48` / `:54` / `:60-267` |
| `RunContextAuthorityRequest` | `:270-312`（含 `turn_id` / `continuation_id` / `provider_request_id` / `provider_turn_ordinal` / `prior_context_revision`） |
| `RunContextSnapshot` | `:314-435`（`request_payload()` `:381`、`payload_hash` `:411`、`receipt_json()` `:414`） |
| `RunContextAuthorityPort` | `:437-441` `def prepare_snapshot(request) -> RunContextSnapshot` |
| `RuntimeDecisionSinkPort` | `:443-457` |
| `TaskExecutionAuthorityPort` | `:497-502` `issue_envelope(...)` — §9.4 提到的 TaskScope 授权就在这里 |
| `ToolCatalogSnapshot` / `DurableToolCatalogResolver` | `:504-518` / `:573-...` |

新模式要加"受版本约束的 AgentTurn Context 请求/选择载体"，最小做法是在 `RunContextAuthorityRequest` 旁边新增一个 `AgentTurnContextRequest`，**不要**改动现有 dataclass 的字段顺序（`RunContextSnapshot.payload_hash` 会被 provider 侧比对）。

---

## 5. `runtime/start_snapshot.py` 与 `drivers/start_mode.py`

- `RunStart`：`start_snapshot.py:153-255`；`start_mode` 默认 `"ordinary"`（`:167`），枚举校验在 `:232`（`{"ordinary", "host_control"}`）
- `StartSnapshot`：`start_snapshot.py:257-533`；同样在 `:324` 校验 `start_mode`
- **快照 schema 版本**：`start_snapshot.py:351` `"schema_version": 6 if self.start_mode == "host_control" else 7`；`from_json` 接受 `{1,2,3,4,5,6,7}`（`:407`）。v7 显式**拒绝** `start_mode/host_control_*` 字段（`:449-452`）。⇒ 新的 `base_agent` 模式必须是 **schema_version 8**，并且旧的 v6/v7 解码路径一个字节都不能动。
- `bind_start_snapshot`：`start_snapshot.py:535-571`（把 `RunStart` 的每个字段搬到 `StartSnapshot`）
- `StartModeDriverRouter`：`drivers/start_mode.py:16-35`，`select()` 只认 `ordinary` / `host_control`，其他一律 `raise ValueError("unsupported durable driver start mode")`（`:30`）。**新增 `base_agent` 分支时，未注册 driver 必须继续走这条 raise，不能落回 ordinary。**
- `_drive` 里的 router 解析：`kernel.py:2801-2802`（`if type(driver) is StartModeDriverRouter: driver = driver.select(snapshot.start_mode)`）；驱动契约标签在 `:2803-2809`（`sdk.react.v2` / `sdk.workflow.v2`）。

---

## 6. `execution/command_ingress.py`：api_mode 从哪里进

- `RunApiMode`：`runtime/commands.py:34-36`，只有 `LEGACY = "legacy"` 和 `COMMAND = "command"`
- 表：`conversation_run_modes(run_id, namespace, api_mode, intent_hash, created_at)`
- `reserve_legacy_run`：`execution/command_ingress.py:137-183`；首次插入 `namespace + RunApiMode.LEGACY`（`:165-168`），重复时比对三元组，不一致 → `CommandError(CommandErrorCode.RUN_MODE_CONFLICT)`（`:183`）
- `reserve_host_control_run`：`execution/command_ingress.py:186-195` — **只是换了 namespace `"host-control/v1"`，api_mode 仍是 LEGACY**
- `require_legacy_or_unmanaged`：`execution/command_ingress.py:197-202`，要求 `("legacy/runtime", "legacy")` 或没有行
- UoW facade：`execution/sqlite/uow.py:1140`（`reserve_legacy_run_mode`）、`:1149`（`reserve_host_control_run_mode`）、`:1154`（`require_legacy_or_unmanaged_run`）
- kernel 侧：`kernel.py:1575-1606`（`_reserve_legacy_start` / `_require_legacy_mode`）

**⇒ base_agent 的隔离用同一机制最省事：新增 `reserve_base_agent_run(namespace="base-agent/v1")`。**`require_legacy_or_unmanaged` 天然就会拒绝它（因为 namespace 不等于 `legacy/runtime`），BA06 几乎白送；只需补 `signal_conversation` / `start_conversation` 的对称拒绝测试。

---

## 7. `execution/dispatch.py`：唯一 Provider 调用路径与 request_id

- `ProviderInvocationCoordinator`：`execution/dispatch.py:226-...`
  - `prepare_claim` `:312`、`_prepare_claim_with_binding` `:337`
  - **`invocation_id = provider_invocation_id(run_id, request.request_id)`（`:351`）**
  - `provider_invocation_id` 定义：`execution/provider_invocations.py:151-158`，= sha256 of `{"protocol": "simple-harness-provider-invocation-v1", "run_id", "request_id"}`
  - `invoke` `:477`；budget 读取 `:469`
- **request_id 的实际来源是 ReAct：`react_loop.py:364` `f"{run_id}:provider-turn:{N}"`**，N = `state.provider_turns_reserved_total`（永不重置，`termination.py:154` 注释）。
- 规范 §8.2 想要 `agent_id + turn_id + local_model_step + purpose`。**现实是 `run_id + 全局递增 turn 序号`。** 由于 `agent_id ↔ run_id` 一对一，且 `provider_turns_reserved_total` 永不重置，跨 AgentTurn 的 request_id 天然不会撞（BA34 的一半已经成立）；但"purpose=summary"和"同 Turn 恢复沿用身份"需要新增 codec。计划里把这条降级为"沿用现有 run 级单调序号 + 在 `base_agent_turns_v1` 记录每个 Turn 的 `provider_turn_ordinal` 区间"，不改 request_id 字符串格式，避免动 ledger。

---

## 8. `runtime/termination.py` 与 `runtime/react_checkpoint.py`

- `TerminationLimits`：`termination.py:131-149`（`max_turns=32 / max_tool_calls=64 / max_wall_seconds=900.0 / max_cost_micros=10_000_000 / max_consecutive_same_tool=3`）
- `TerminationState`：`termination.py:153-193`，**"totals are reservations and never reset on restart"（`:155`）**。里面已经有 `pending_child_completion / _hash / _append_id`（`:172-174`）——那是 workflow spawn 把子结果塞回父 Context 的载体。
- 计数点：`before_provider` `:346-370`（`>= limits.max_turns` 抛 `TerminationBudgetExceeded`）、`before_tool_batch` `:371-398`、`before_tool` `:399-408`
- `max_wall_seconds=900` 是 **Run 级 wall clock**，规范 §10.2 明确要求 Agent 生命周期不能沿用它 ⇒ 新模式必须用 Turn 级 deadline。
- `DurableReactCheckpoint`：`react_checkpoint.py:19-100`，namespace 固定 `"react.termination.v1"`（校验在 `:114`），**按 `run_id` 单键**，`read_initial_react_checkpoint` 要求 version==0 的锚（`:67-69`）。新模式要按 `(agent_id, turn_id)` 管理 checkpoint，只能新开 namespace（例如 `base_agent.turn.v1`），**不能改这个锚的语义**。
- Port 定义在 `kernel.py:310-348`（`ReactCheckpointPort`），实现是 `SqliteExecutionUnitOfWork`（`consumer_adapter.py:434` `react_checkpoint=uow`）。

---

## 9. `execution/sqlite/uow.py` 与 `schema.py`

### 9.1 schema 当前状态

`execution/sqlite/schema.py`（全文 92 行）：
- `SCHEMA_VERSION = 9`（`:12`）
- `legacy_v7_descriptor()`（`:40-52`）= `migrations/0005_fresh.sql` + `_V6_CATALOG_COLUMNS` + `_V7_MEMORY_AUTHORITY_COLUMNS`
- `legacy_v8_descriptor()`（`:55-59`）= v7 SQL + `context_use.DDL`
- `fresh_descriptor()`（`:62-66`）= v8 SQL + `short_context_schema.DDL`
- **checksum 算法**：`hashlib.sha256(sql.encode()).hexdigest()`，其中 `sql` 是"从头拼到尾的整串 DDL"（`:51/:59/:66`）
- **descriptor 冻结方式**：`accepted_descriptor_rows()`（`:69-73`）返回 4 种可接受的 `sdk_schema_migrations` 行组合：`(9,)`、`(7,9)`、`(8,9)`、`(7,8,9)`
- `migrations()` 只返回 fresh descriptor（`:76-79`），**历史 migration 从不重放** ⇒ "不重写历史 migration checksum"这条在这个结构下自动成立

`Database`：`execution/sqlite/database.py`
- `schema_version` 属性 `:88-95`（读 `sdk_schema_migrations` 最大 version）
- `_initialize_or_validate` `:201-241`：新库建表并插入 fresh descriptor 行（`:214-231`）；已有库比对 `accepted_descriptor_rows()`（`:235`），不匹配即 `ExecutionSchemaIncompatible`

**⇒ v10 的正确做法：** 新增 `legacy_v9_descriptor()`（= 现在的 `fresh_descriptor()` 内容），`fresh_descriptor()` 改成 `legacy_v9_descriptor().sql + base_agent.DDL`，`SCHEMA_VERSION = 10`，`accepted_descriptor_rows()` 扩成含 `10` 结尾的组合。旧 checksum 一个都不动。

### 9.2 升级已有库的现成范式

`execution/sqlite/short_context_migration.py`（v7/v8 → v9）：备份优先 + 回执（`ExecutionShortContextUpgradeReceiptV1` `:24-40`）+ 载体校验 `_carriers`（`:42-66`）+ `_validate`（`:69-...`）比对 `accepted_descriptor_rows()`。`context_use_migration.py` 是 v7→v8 的同类。v10 迁移照抄这个骨架。

### 9.3 UoW facade

- `SqliteExecutionUnitOfWork` 单文件 **17140 行**；`transaction_owner` 在 `kernel.py:532` 的 Protocol 里，实现保证"唯一 Database transaction owner"（`kernel.py:1500-1504` 校验 workflow runner 与 runtime 共享同一个 owner）
- 子 Run 相关关键方法：
  - `issue_profile_launch_ticket`：`uow.py:4537-4583`（父 Run 终态时拒发 ticket，`:4560-4561`）
  - `claim_profile_launch_and_commit_child`：`uow.py:4585-4790`（校验 `launch_request.profile_key/driver_kind/catalog_generation`，`:4615-4619`；fingerprint 绑定 `:4612`）
  - `_commit_child_terminal`：`uow.py:4835-5045`；`finalize_child_and_enqueue_parent_signal`：`uow.py:4791`；`commit_detached_child_terminal`：`uow.py:4806`
  - `claim_next_child_signal`：`uow.py:5046-...`
  - `read_child_terminal_result_for_run`：Protocol 在 `execution/uow.py:408`
- 事务 helper 约定："交易 helper 接受外层 connection，不自行再次开启事务"——现有代码里 `_append_context_in_transaction`（`runtime/context.py:102`）就是这个范式的样板。

---

## 10. `runtime/consumer_adapter.py` 与 `runtime/production.py`

### 10.1 Memory 可选注入怎么做到的

`consumer_adapter.py:_build_consumer_runtime` `:336-497`：
- `ports.memory` 为 `None` 时（`:419-451`）：
  - `memory_dispatcher=None`（`:441-450`）
  - `conversation_memory_enabled=False`（`:451`）
  - `context_staging=None`（`:452`）
  - `context_preparation_mode=None`（`:453-455`）
  - `agent_memory=None`（`:456`）
- `RuntimePorts.__post_init__`（`kernel.py:700-706`）只在 `conversation_memory_enabled=True` 时才要求 staging / dispatcher / preparation mode ⇒ **不给 memory 就是合法的完整运行时**
- 驱动：`build_react_driver(limits=TerminationLimits(max_turns=..., max_tool_calls=...), budget_policy=..., estimator=...)`（`:459-466`）
- profiles 固定 `{"agent.general": RuntimeProfile("agent.general", "react")}`（`:481`），drivers `{"react": driver}`（`:482`）

### 10.2 production 强制 Memory

`runtime/production.py`：`ProductionRuntimeConfig.memory: AgentMemoryPort`（`:86`，无默认值）；`__post_init__` 在 `:120-134` 把 `"memory"` 列进必填名单并逐个校验方法。

**⇒ `build_agent_runtime(config, ports)` 建在 `build_consumer_runtime` / `build_runtime` 这一层之上：**
```
build_agent_runtime(config, ports)
  ├─ Database.open / SqliteExecutionUnitOfWork          （同 consumer_adapter:385-386）
  ├─ ToolRegistry（含 agent.delegate，晚绑 runtime）
  ├─ EffectExecutor / ProviderInvocationCoordinator / SqliteContextPort
  ├─ RuntimePorts(..., agent_memory=None, conversation_memory_enabled=False,
  │               context_staging=None, memory_dispatcher=None)
  ├─ drivers={"react": legacy_react, "base_agent": AgentExecutionDriver}
  │   （经 StartModeDriverRouter 分流）
  └─ build_runtime(...) → Runtime，再 late-bind delegate 工具的 runtime 引用
```

---

## 11. 现有子 Run 机制：能不能当"主 Agent 委派子 Agent"的底座

### 11.1 三条独立的机制

| 机制 | 文件 | 父子协议 | checkpoint | 信号回传 |
|---|---|---|---|---|
| **ChildCoordinator（通用）** | `runtime/child_coordinator.py:13-41`、`runtime/child_runs.py`、`execution/contracts/children.py` | 父先 `issue_profile_launch_ticket`（一次性 ticket，绑 `child_launch_fingerprint`），再 `claim_profile_launch_and_commit_child` 原子建子 Run + start snapshot，然后 `_activate` + `_schedule` | 无（子 Run 用自己 driver 的 checkpoint） | 由 `_terminalize`（`kernel.py:3195-3240`）按 `AttachmentPolicy` 决定：ATTACHED → `child_signals` 行 → `ChildSignalRuntime` ack 成父的 `{"kind":"child_terminal"}` continuation；DETACHED → 只写 `child_terminal_receipts`，父自己读 |
| **workflow_spawn（模型面已下线）** | `runtime/workflow_spawn.py`、`kernel.py:370-527`、`uow.py:2376-2760` | 工具产出 `WorkflowSpawnToolOutcome` → `_drive` 特判（`kernel.py:2845`）→ `_accept_workflow_spawn_control`（`:3351`）；子 Run 的 `driver_kind` **硬编码 WORKFLOW**（`kernel.py:510`） | ReAct checkpoint `phase="child_wait"`（`react.py:140`）+ `workflow_spawn_child_wait_receipts` 表 | 子终态 signal → continuation → `ack_spawn_child_continuation_and_continue_batch`（`uow.py:2376`）把子结果写成 `pending_child_completion`，恢复原来的工具批次 |
| **child_signal（通用管道）** | `runtime/child_signal_runtime.py:45-122` | 与 driver 无关 | 无 | `receive_one` → `ack_child_signal_and_commit_parent_progress`，continuation payload = `{"kind":"child_terminal","signal_id","child_run_id","payload"}`（`:76-81`） |

### 11.2 关键证据

- **通用子 Run 可以是 react**：`tests/integration/runtime/test_runtime_h12.py:156-190` 用 `launch = {"profile_key":"workflow.child","driver_kind":"react",...}` 成功启动，`handle.run.run_id == "child-1"`，子 Run 跑到 WAITING。
- **子终态回执与 driver 无关**：`kernel.py:3195` 只判断 `run.parent_run_id is not None`。
- **spawn 路径写死 workflow**：`kernel.py:504-511` `bind_start_snapshot(start, profile_key=verified.profile_key, driver_kind=WORKFLOW_DRIVER_KIND, workflow_admission=request)`。
- **ReAct 会吞掉 `child_terminal` continuation**：`react.py:150-155`（见 §2.2）。
- Host 侧已于 2026-09-09 下线模型面 `workflow_spawn`（Host `plans/2026-09-09-remove-workflow-line/`），SDK 内协议仍在但无人调用。

### 11.3 结论

**复用 = ChildCoordinator + child_terminal_receipts（DETACHED）；不复用 = workflow_spawn 协议；新写 = 一个最小 `agent.delegate` 工具。**

理由：
1. spawn 协议的子 Run 只能是 workflow，改它等于改 workflow 线，与 Host 已下线的方向相反。
2. spawn 的挂起/恢复语义（`workflow_spawn_child_wait_receipts` + `phase="child_wait"`）绑死在 workflow 表上，为 BaseAgent 复制一份等于写第二个 spawn。
3. ChildCoordinator 提供的正是需要的最小原语：**一次事务里"消费配额票 + 建子执行身份 + 冻结子 start snapshot"**，且 `driver_kind` 自由。
4. 选 `AttachmentPolicy.DETACHED` 可以完全绕开 §2.2 的 continuation 吞噬缺口：子终态写进 `child_terminal_receipts`，`agent.delegate` 工具直接 `read_child_terminal_result_for_run` 读回，走的是既有工具网关/授权/effect 账本（`tools/executor.py:235-305`），UNKNOWN 时由 `ToolReconciliationPort.observe`（`tools/reconciliation.py:41-42`）以该回执为证据结算——**这条恢复路径正好是规范 §8.4 想要的"不盲重试"**。

---

## 12. 工具系统

| 主题 | 位置 | 事实 |
|---|---|---|
| 注册 | `tools/registry.py:104-113`（`register` / `register_function`）；`seal()` `:88-102` 冻结并算 inventory digest | 密封后禁止再注册（`:105-106`） |
| schema 校验 | `tools/contracts.py:65-89` `ToolSpec.__post_init__` → `validate_tool_schema(input_schema)`（`tools/schema.py`）；调用参数校验 `validate_arguments` | sidecar 存在时校验 `schema_hash` 一致（`:83-89`） |
| ToolContext | `tools/contracts.py:107-145` | 已有 `workflow_spawn_context` / `task_execution_envelope` 两个特权字段的先例，新增 `agent_delegate_context` 走同一模式 |
| 执行/审计 | `tools/executor.py:235-305`（`execute`：requested → \_execute_audited → 结果记账）；`:307+` `_execute_audited` 校验 lease/fence/call_id/effect_id | 已发生 effect 的真实结果不可篡改（`:288-293` 注释） |
| effect 分类 | `tools/runtime_catalog.py:65-69` `ToolEffectClass{CONTEXT_CONTROL, PROJECT_EFFECT, NON_PROJECT_EFFECT}`；`:71-75` `ToolRouteRequirement` | `PROJECT_EFFECT` 会要求 TaskExecution authority（`react_loop.py:199-215`）⇒ **`agent.delegate` 应声明为 `NON_PROJECT_EFFECT`**，否则触发 §9.4 说的 TaskScope 缺口 |
| 每 Run 工具集 | `react.py:377-401` `_tools(start.input["capability_snapshot"]["tools"], ...)` | 子 Agent 不给 delegate ⇒ 结构性禁递归 |
| 冻结清单 / 公共 API 快照 | `tests/conformance/tool-public-api.json` + `tests/conformance/test_tool_public_api.py:12-19`（断言 `sorted(tools.__all__) == snapshot["symbols"]`）；`tests/conformance/provider-public-api.json` + `test_provider_public_api.py`；`tests/unit/contracts/public-api.json` + `test_public_api.py:15-26` | 见 §13 |
| 协议版本 | `tests/conformance/test_protocol_version.py:29` 断言 `PROTOCOL_VERSION == "1.0.0"` | 新增 Agent 层**不要**动 PROTOCOL_VERSION（那是 Host↔SDK 传输协议），否则 Host 会判不兼容 |

### 12.1 新增公共导出会不会打破快照，怎么合法更新

会。三处：
1. `tests/unit/contracts/public-api.json`：`test_public_api_matches_frozen_snapshot`（`tests/unit/contracts/test_public_api.py:15-26`）断言 `list(simple_harness.__all__) == snapshot["simple_harness"]` **完全相等**，且 `simple_harness.__version__ == snapshot["version"] == "0.7.8"`。
2. `tests/conformance/tool-public-api.json`：`sorted(tools.__all__)` 完全相等。
3. 若新增 provider 符号还会碰 `provider-public-api.json`。

**仓库已有的合法更新范式**（`test_public_api.py:29-67`）：为上一版本冻结一份 `public-api-<old>.json`，再加一条 `test_<feature>_successor_preserves_h<old>_exports`，断言 `set(old[module]) <= set(current[module])`（只增不减）。0.7.1/0.7.2/0.7.3/0.7.4/0.7.5/0.7.7 都是这么做的。⇒ Slice 1 照此新增 `public-api-0.7.10.json` + 一条 successor 测试，并把 `public-api.json` 的符号表与 `version` 一起更新。

---

## 13. 测试基础设施与真实 Provider

### 13.1 可直接复用的东西

| 资产 | 位置 | 用途 |
|---|---|---|
| 端到端 fixture（消费者运行时 + 确定性 provider + noop 工具 + allow 授权） | `tests/conformance/future_consumer_fixture.py:1-238`（`FutureConsumerFixture` `:140-232`、`DeterministicProvider`、`NoopTools`、`AllowAuthorization`） | Slice 1 的 mock 端到端直接照抄这个骨架，把 `memory=None` |
| 多轮 continuation 用法 | `future_consumer_fixture.py:194-232`（`start_turn` / `continue_turn`）+ `tests/conformance/test_future_consumer_memory.py:347-400` | 现有多轮只在 provider 被堵住时成立（见 §0.1） |
| Provider ledger 假件 | `tests/integration/provider_ledger_fakes.py:23-90`（`FakeProviderInvocationUnitOfWork`） | 单测 dispatch 层 |
| 生产装配测试骨架（假 Memory/Catalog/Driver/Pump） | `tests/runtime/test_production_runtime.py:46-120` | `build_agent_runtime` 的装配测试与 BA38 的 spy |
| 子 Run 启动 | `tests/integration/runtime/test_runtime_h12.py:156-190` | delegate 工具的最小可行样板 |
| 子终态与 ticket | `tests/integration/execution/test_child_terminal_h12.py`、`test_ticket_generation.py`、`test_atomic_child_launch.py`、`tests/runtime/test_child_restart.py` | 委派恢复语义 |
| sqlite fixture | 各测试直接 `Database.open(tmp_path / "execution.db")` + `SqliteExecutionUnitOfWork(database)`（如 `tests/runtime/test_conversation_continuation.py:12-14`） | — |
| 自带 conformance 跑器 | `src/simple_harness/testing/`（`cli.py` / `runner.py` / `suites/`），`pytest_plugin.py` | A6 的 exact-wheel 验证 |

### 13.2 真实 Provider 怎么接

- SDK 侧适配器已存在：`src/simple_harness/providers/openai_compatible.py:56-113`
  ```python
  OpenAICompatibleProvider(client: httpx.AsyncClient, base_url: str, model: str,
                           secret: Secret, timeout=30.0, *, provider_id=None, pricing_key=None)
  ```
  端点自动补 `/chat/completions`（`:93-98`）；非环回地址强制 HTTPS（`:88-92`）；`ProviderTarget.adapter_key = "openai-compatible.chat-completions.v1"`（`:112`）。
- **配置来源（本机实测）**：
  - 任务书里写的 `simple_harness/backend/llm_runtime.json` **不存在**。真实路径是应用 user-data 目录：`backend/main.py:250` `LLM_RUNTIME_PATH = _paths.user_data_dir() / "llm_runtime.json"`。
  - 可直接读的等价配置是 Host 仓库根的 `.env`，键名：`BASEURL`、`APIKEY`（另有 `DEEPSEEKER_APIKEY`、`CHINZY_APIKEY`、`CHINZY_BASEURL`）。`BASEURL = https://ai.svtun.cn/v1`。
  - 证据目录里的 `llm_runtime.json` 样本（`simple_harness/.local-test-evidence/real-ui-channel/verify-06/userdata/llm_runtime.json`）字段为 `{base_url, model}`，`model = gpt-5.6-luna`。
  - **key 值本文不复制、不打印**；测试里只从环境/文件读取后直接塞进 `Secret`。
- 建议：真实 provider 测试用 `-m real_provider` 标记，默认 deselect；CI 与"源码红集"不混算（BA40）。

### 13.3 基线红集（**重要**）

- `tests/integration/runtime/test_context_use_{admission,durable,public_memory}.py` import 不存在的 `simple_harness_memory`，收集即中断，必须 `--ignore`。
- 忽略后：**60 failed / 15 errors / 1843 passed / 2 skipped**，与 `plans/2026-09-10-base-agent-phase1/baseline-known-failures.txt`（75 条）逐条一致，与解释器版本无关（3.12 与 3.14 相同）。
- 分类见同目录 `baseline.md`。本次抽样复核过的代表：
  - `tests/unit/contracts/test_public_api.py:17` `assert '0.7.10' == '0.7.8'`
  - `tests/integration/execution/test_open_close.py:29` `assert 9 == 7`
  - `tests/execution/test_command_ingress.py:65` `assert 9 == 7`
  - `tests/integration/execution/test_atomic_decision.py:185` `assert 4 == 2`（`run_events` 计数）
  - `tests/integration/runtime/test_decision_terminal_recovery.py` 15 errors：`H077_LEGACY_075_TARGET must name preserved installed H075`（需本机预装历史 wheel）
  - `tests/artifact/test_release_candidate_contract.py:89` 断言 `__version__ = "0.7.5"`

---

## 14. 根包 `__init__.py` 的惰性导出机制

- `src/simple_harness/__init__.py:14-38` 直接从 `.contracts` 导入轻量契约；`from .version import __version__`（`:39`）
- `_RUNTIME_EXPORTS`：`:41-357`，一个 frozenset，列出所有"延迟到 `simple_harness.runtime` 再解析"的符号
- `__getattr__`：`:360-388`
  - 特判 `ExpiredAuthorizationTerminalRecoveryV1`（`:361-364`）
  - 特判一组 audit 符号 → `from .execution import audit`（`:365-381`）
  - 其余落在 `_RUNTIME_EXPORTS` 里的 → `from . import runtime; getattr(runtime, name)`，并写回 `globals()` 缓存（`:382-388`）
- `__dir__`：`:391-392`
- `__all__`：`:395+`（首元素必须是 `"__version__"`，见 `tests/artifact/test_import_purity.py` 内嵌脚本的断言）
- **import 纯度门**：`tests/artifact/test_import_purity.py` 在子进程里禁网、禁环境变量读写、比对线程/文件快照后 `import simple_harness`。⇒ 新增 `BaseAgent / AgentRuntime / AgentConfig / ...` 必须走 `_RUNTIME_EXPORTS` + `__getattr__`，**不能在模块顶层 import sqlite/httpx 或打开数据库**。

---

## 15. 规范与现实的偏差

> 以下每条都是"BA-v1.0 的假设"与"fd12e7dd 的真实代码 / 用户目标"的不一致，实施前必须先认账。

| # | 规范的说法 | 现实 | 影响与处置 |
|---|---|---|---|
| **D1** | §2「本阶段不实现……**自动子 Agent 委派**」；§15「默认不向模型暴露自动 create_many 权限」 | **用户给第一阶段定的验收目标就是"主 Agent 委派出一个子 Agent 完成复杂任务"** | **这是规范与用户目标的正面冲突，也是本次拆片最关键的判断。** 处置：不实现"模型可调用的 create_many"（尊重 §15 的防无限繁殖意图），而是实现一个**单层、有配额、非递归**的 `agent.delegate` 工具；子 Agent 的 `capability_snapshot.tools` 不含该工具，从结构上禁递归。计划里把它记为 DG01–DG04，**不占用 BA01–BA40 编号**，并在 program.md 显式标注"这是对 BA-v1.0 §2 的一次有意越界" |
| **D2** | §0.2「`runtime/kernel.py` 190–256：DriverResult 的对话输出只允许绑定 COMPLETED」 | 真实行数是 `kernel.py:199-244`，约束语句在 `:218-222`。语义一致，行号偏移 ~9 行 | 仅行号；`DriverResult` 已有 4 组"独占载体"校验（`:224-243`）可照抄 |
| **D3** | §0.2「`runtime/drivers/react.py` 250–320：默认最终响应映射到 COMPLETED」 | 真实是 `react.py:286-306`，`RunState.COMPLETED` 在 `:292` | 仅行号 |
| **D4** | §0.2「`runtime/drivers/react_loop.py` 360–430：新 Provider 请求准备和 Context authority 插槽」 | 真实是 `react_loop.py:359-420`；`return ReActResult` 的最终点在 `:732` | 仅行号；规范没提到 `:717-731` 的"phase 复位"，那才是挂 RESULT_PENDING 的实际边界 |
| **D5** | §0.2「`runtime/context.py` 1–215」 | 真实 253 行；全量重写在 `:170-174` | 仅行号 |
| **D6** | §8.2「内部 request_id = agent_id + turn_id + local_model_step + purpose」 | 现实是 `f"{run_id}:provider-turn:{N}"`（`react_loop.py:364`），N 为 Run 级永不重置的预留计数（`termination.py:155`）；`invocation_id = sha256(run_id, request_id)`（`provider_invocations.py:151`） | 改 request_id 格式 = 改整个 Provider ledger 的身份。**处置：不改格式**；用 `base_agent_turns_v1` 记录每个 Turn 占用的 `provider_turn_ordinal` 区间来做归属；`purpose=summary` 推迟到 Slice 3/4 再用独立 request 前缀 |
| **D7** | §9.3「`runtime/react_checkpoint.py`：新模式以 `(agent_id,turn_id)` 管理 checkpoint」 | `DurableReactCheckpoint` 与 `ReactCheckpointPort.cas_react_checkpoint` 都是 `run_id` 单键（`react_checkpoint.py:93-100`、`kernel.py:315-326`），namespace 硬编码 `"react.termination.v1"`（`react_checkpoint.py:114`） | 需要**新 namespace + 新 port 方法**，不是"加个参数"。Slice 1 先用同一 `run_id` + 在 checkpoint payload 里带 `turn_id`，把按 `(agent_id,turn_id)` 分键推迟到 Slice 2 |
| **D8** | §9.3「`execution/command_ingress.py`：验证新旧 API mode」，隐含要新增 api_mode | `RunApiMode` 只有 `LEGACY/COMMAND`（`runtime/commands.py:34-36`）；host_control **复用 LEGACY**，靠 namespace 区分（`command_ingress.py:186-195`） | 照 host_control 的先例用 `namespace="base-agent/v1"` 即可，**不必新增枚举值**（新增枚举会打 `command-public-api.json` 快照） |
| **D9** | §9.4「新增 `build_agent_runtime(config, ports)`，基于下层 build_runtime／RuntimePorts」；「现有 `build_production_runtime` 若要求 Memory，保留其旧合同」 | 属实：`production.py:86/120-131` 强制 memory；`consumer_adapter.py:419-456` 是现成的"无 Memory 装配"范式 | 无偏差，但计划要点名"抄 `_build_consumer_runtime` 而不是抄 production" |
| **D10** | §11.2「当前从已知 v9 起步时可将本阶段作为唯一 v10 候选」 | v9 属实（`schema.py:12`）。但 `accepted_descriptor_rows()`（`:69-73`）是一个**穷举白名单**，加 v10 必须同时扩它，否则所有既有 v9 库开不开 | 计划里 T2 显式列出这 4 组 → 8 组的扩展 |
| **D11** | §11「先 source tests，再安装所构建 exact wheel 执行无 Memory SDK 的 conformance」，隐含 source tests 是绿的 | **基线不绿**：60 failed / 15 errors（见 §13.3）。其中 ~20 条是 schema/迁移夹具漂移，**v10 会让它们再变一次** | A0 必须把"红集"当成冻结基线（已有 `baseline-known-failures.txt`），并在 v10 之后重新冻结一次差异 |
| **D12** | §13 BA38「基础包安装不要求 simple-harness-memory-sdk」 | 已经成立（`memory=None` 合法），但 `tests/integration/runtime/test_context_use_*.py` 三个文件硬 import `simple_harness_memory`，使**全量 pytest 无法收集** | Slice 1 的回归命令必须固定带 `--ignore`；这三个文件的归属推迟到 Slice 5 |
| **D13** | §14 端到端演示：Agent A/B 各自处理材料、超预算轮换、跨重启恢复 | 演示里没有"主 Agent 委派子 Agent"这一步 | Slice 1 的演示按**用户目标**重写：主 Agent 收复杂任务 → 委派 → 子 Agent 两轮 → 结果回主 → 重启后旧结果可读。§14 的原演示留给 Slice 3/4 |
| **D14** | §5.1「`create_many` … 先验证整批配置与资源配额」等批量语义 | 没有任何现成批量原语；`Runtime` 的所有入口都是单 Run | Slice 1 只做 `create` + 一个"顺序 create 的 create_many"最小实现（够 BA01），完整批量幂等/冲突/整批回滚（BA02–BA04）放 Slice 2 |
| **D15** | §9.3「`execution/sqlite/uow.py`：暴露 facade，委派给 base_agent 事务 helper」 | `uow.py` 已 17140 行；再往里加会继续劣化 | 新 helper 放 `execution/sqlite/base_agent/*`，`uow.py` 只加薄 facade 方法（≤10 行/个），并复用 `runtime/context.py:102` 的"接受外层 connection"范式 |
| **D16** | 任务书：「真实 Provider 配置在 Host 仓库 `simple_harness/backend/llm_runtime.json`」 | 该文件不存在。实际路径 = 平台 user-data 目录（`backend/main.py:250`）；仓库内可读的等价来源是 Host 根 `.env`（`BASEURL` / `APIKEY`） | 真实 provider 测试从 `.env` 读；文档与代码都不落 key |

---

## 16. Slice 1 会碰到的既有约束（必须原样保留）

1. `DriverResult` 旧规则：`conversation_output` 仍然只能出现在 COMPLETED（`kernel.py:218-222`）——新载体必须与它**互斥**，不能共存。
2. `_drive` 的 preflight 四道闸：policy_fingerprint（`:2737`）、tool catalog 冻结（`:2751`）、CANCEL_REQUESTED（`:2782`）、continuation claim（`:2789`）。
3. lease / fence / 受控 handoff：`ExecutionLease` + `RunFenceLease` 贯穿 context.append（`context.py:115-141`）、effect 执行（`executor.py:326-336`）、checkpoint CAS。
4. 旧 start snapshot 字节：v6/v7 的 `to_json` / `from_json` 一个字节都不能变（`start_snapshot.py:346-533`）。
5. `react.termination.v1` 的初始锚（`react_checkpoint.py:67-69`，version==0）不可被新模式覆盖。
6. `TerminationState` 的"totals never reset on restart"（`termination.py:155`）。
7. 单一 transaction owner（`kernel.py:1500-1504`）。
8. 已发生 effect 的真实结果不可改写（`executor.py:288-293`）。
9. import 纯度：根包 import 不开库、不下模型（`tests/artifact/test_import_purity.py`）。
10. `PROTOCOL_VERSION == "1.0.0"`（`tests/conformance/test_protocol_version.py:29`）不动。
