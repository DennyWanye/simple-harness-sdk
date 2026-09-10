# Slice 2 调查：生命周期与批量创建（代码事实核对）

- 仓库：`/Users/taiwan/PROJECTS/SimplaHarness/simple-harness-sdk`，基线 main = `4613e62`（Slice 1 已交付，journal VERDICT SHIPPED）
- 绿色起点实测：`.venv/bin/python -m pytest -q -p no:cacheprovider tests/agents tests/execution/test_base_agent_schema_v10.py` → **83 passed / 1 skipped / 4.33 s**
- 上游口径：Host `plans/taskSys2/base-agent-phase1-plan.zh-CN.md`（BA-v1.0）§1.3 / §4.2 / §4.3 / §5 / §8 / §10 / §13
- 切片纲要：`../program.md` §2「S2 生命周期与批量创建」
- 上一片：`../slice-1/{plan.md,acceptance.md,journal.md}`；`../investigation.md` §15（D1–D16）/ §16（旧约束）
- **本文所有 `文件:行号` 均在 `4613e62` 工作树上逐条核对**

---

## 0. 结论摘要（先看这一节）

1. **S2 的主要矛盾不是批量创建，而是"停而不死"的控制面**。现有内核里唯一的"停"是 `CANCEL_REQUESTED → _terminalize_cancelled → RunState.CANCELLED`（`kernel.py:2962-2966`、`kernel.py:3223-3262`、`kernel.py:3413-3455`），而 BaseAgent 的 `close`/`cancel_turn` 按 BA-v1.0 §1.3/§8.4 必须"拒收新输入但不进终态"。**复用内核 cancel 会直接杀死 Agent**，因此 close 必须走**执行内核之外的持久生命周期状态**（bindings 列 + 控制命令回执 + `submit` 事务闸门）。
2. **BA08（同 Agent 两条输入顺序处理）在结构上已经成立**：continuation 按 `fifo_seq ASC` 单条认领（`uow.py:4174-4181`），一个 Run 同时只有一个 drive task（`live_index.py:20-35`、`kernel.py:2894-2895`），finalize 后由 `_reschedule` 串行拉下一条（`kernel.py:3405-3411`）。S2 要补的是**护栏（`max_pending_inputs` 执法）与决定性测试**，不是新机制。
3. **per-turn 限制（L3）可以完全不改 `react_loop.py` 做到**：`ReActLoop` 每次都读 `self._collaborator.limits`（`react_loop.py:360-361`、`:554-557`），而 `TerminationState` 的三个总量都是**永不重置的 run 级累计**（`termination.py:155-160`、`:350-358`、`:380-381`）。因此在 driver 里按"本轮起点总量 + per-turn 上限"派生一份**逐轮的 `TerminationLimits`**，超限时 `TerminationBudgetExceeded` 已经被 driver 捕获转成失败 Turn（`execution.py:248-266`），Agent 不死。
4. **checkpoint 按 `(agent_id, turn_id)` 分键（D7）在 S2 不应做**，且应当**明确记为"不分键"**：`{run_id}:provider-turn:{N}` 的请求身份唯一性与 lifetime totals 都建立在 run 级永不重置的计数上（`react_loop.py:364`、`termination.py:155`），分键会让每轮从 1 起、跨轮撞 request_id，**直接违反 BA34**。S2 改为在 checkpoint 里写 `active_turn_id`（字段已存在但当前永远是 `None`），用它做"同 Turn 恢复"的断言锚。
5. **批量创建无法做成"一个短事务"**（BA-v1.0 §5.1 的字面要求）：`base_agent_bindings_v1.run_id` 外键指向 `runs`（`base_agent/schema.py:14`），runs 行只能由 `start_base_agent_run` 逐个建（`kernel.py:1702-1716`，各自事务）。处置是**写前整批准入校验 + 两段式批次行（reserved → committed）**：BA04 的"整批不留部分成功"由**校验先于任何写**保证，崩溃残留由同 `batch_key` 续做收敛。
6. **v10 描述符必须重算**，且这会让**任何已存在的 v10 开发库拒绝打开**（`schema.py:69-95`、`database.py:235`）。v10 未发布、无 Host 使用，记为已知后果（S2-D9），升级器归 S5 的 BA37。
7. 一条**未归属的 S1 遗留**：L5 / F-BA-1（真实端点 `provider_response_not_darable`）不在用户为 S2 划定的范围内，本文 §15 给出根因候选与建议，**请主编排者裁决归属**。

---

## 1. S2 主要矛盾的代码依据："停而不死"

### 1.1 内核现有的"停"全都是终态

| 位置 | 现状 |
|---|---|
| `kernel.py:1458-1460` | `RunClient.cancel(run_id)` → `Runtime._cancel_run` |
| `kernel.py:3223-3262` | `_cancel_run`：非 workflow 走 `request_run_cancel` → `CANCEL_REQUESTED`，随后 `:3259-3260` 立刻 `_terminalize_cancelled` |
| `kernel.py:3413-3455` | `_terminalize_cancelled`：非 workflow 分支 `_terminalize(run, state=RunState.CANCELLED, ...)` |
| `kernel.py:2962-2966` | `_drive` preflight 第三道闸：`run.state is RunState.CANCEL_REQUESTED` → `_terminalize_cancelled(reason="pre_drive_cancel")` |
| `kernel.py:2131-2132` | `recover()`：`CANCEL_REQUESTED` 的 Run 在启动恢复时被终态化 |
| `uow.py:4167-4173` | `claim_continuation`：Run 已终态则把所有 pending/claimed continuation **quarantine** 并返回 None |

结论：**任何走 `CANCEL_REQUESTED` 的路径都会让 BaseAgent 进入 `CANCELLED` 终态**，之后 `signal_base_agent_input` 会在 `kernel.py:1741-1742` 抛 `terminal Run rejects new continuations`，`get_result`/`history` 仍可读但 Agent 永久死亡——这与 BA-v1.0 §1.3「close() 是持久生命周期操作」「close() 不等于擦除历史」「Turn 失败不默认永久杀死 Agent」全都冲突。

### 1.2 `asyncio.CancelledError` 路径也不能直接用

`kernel.py:3171-3180`：`_drive` 捕获 `asyncio.CancelledError` 后，**只有** `current.state is RunState.CANCEL_REQUESTED` 才做处置；否则**什么都不做**——本轮的 continuation claim 不 ack、不释放，执行者租约留在本进程，Run 停在旧状态。因此"取消本轮"**不能**简单地 `self._cancels[run_id].cancel()`（`kernel.py:2845` 建的那个 token），否则会留下悬挂的 claim。

### 1.3 可用的落点：`control_generation` 与 `AgentClosed` 都已存在但从未被写/抛

- `base_agent_bindings_v1.control_generation INTEGER NOT NULL DEFAULT 0`（`base_agent/schema.py:21`），读进 `AgentBindingRecord.control_generation`（`execution/base_agent.py:38`、`sqlite/base_agent/turns.py:37`），**全仓库没有任何一处写它**（`grep -rn control_generation src/` 只有这 4 处定义/读取）。
- `agents/contracts.py:66-67` 已定义 `AgentClosed(AgentError)`，`code = "agent_closed"`，**没有任何地方抛出**。

这两个是 BA-v1.0 §8.4「取消 Turn 提交 control generation，阻止旧实例的新 handoff」预留的接口，S2 正是把它们接上。

---

## 2. 批量创建：现有原语与缺口

### 2.1 现有的创建路径（逐条核对）

| 环节 | 位置 | 事实 |
|---|---|---|
| 稳定身份 | `agents/runtime.py:187-191` | `agent_id_for(owner_scope, creation_key) = "agent-" + sha256(owner_scope\x00creation_key)[:32]`，**重试天然复现同一个 id** |
| 幂等创建 | `agents/runtime.py:274-313` | `create()` 先 `read_agent_binding(agent_id)`；命中且 `config_hash` 不同 → `ValueError`；否则复用 |
| binding 幂等 | `sqlite/base_agent/turns.py:89-126` `insert_binding` | 按 `creation_key` 查重（`creation_key TEXT NOT NULL UNIQUE`，`base_agent/schema.py:18`）；`config_hash` 或 `agent_id` 不符 → `UnitOfWorkConflict("creation_key reused with a different BaseAgent")` |
| facade | `uow.py:1179-1204` `create_agent_binding` | 自开一个事务，转发 `turns.insert_binding` |
| 最小 create_many | `agents/runtime.py:315-325` | **顺序调用 `create`，key = `f"{batch_key}:{index}"`**；无批次行、无 fingerprint、无冲突判定、无整批回滚 |
| 创建不驱动模型 | `agents/execution.py:142-161` | 无 `base_agent_input` continuation 时 driver 立即 `DriverResult(WAITING, {"base_agent_stage":"idle"})`，不 load Context、不进 `ReActLoop` |

### 2.2 为什么做不成"一个短事务"（BA-v1.0 §5.1）

- `base_agent_bindings_v1.run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id)`（`base_agent/schema.py:14`）；
- `runs` 行由 `Runtime.start_base_agent_run` → `_reserve_base_agent_start`（`kernel.py:1683-1700`）+ `_start_run`（`kernel.py:2713`）建立，**每个 Agent 至少两个独立事务**（围栏行 + run 行）；
- `AgentRuntime.create` 因此天然是"N 次 await + N 组事务"。

**处置（S2 方案）**：
1. **写前整批准入校验**（不落任何行）：类型、`AgentConfig` 合法性、`tool_names ⊆ 运行时工具目录`、批量大小 ≤ `max_batch_size`、实例总数 ≤ `max_agents`。任一不过 → 抛 `AgentBatchRejected`，**零行写入**（BA04 因此是决定性可测的）。
2. **批次行两段式**：事务 A 写 `base_agent_creation_batches_v1`（`state='reserved'`，带 `batch_fingerprint` 与按序 `agent_ids_json`）→ 逐个 `create` → 事务 B 置 `state='committed'` 并写 `receipt_json`。
3. **幂等/冲突**：同 `(owner_scope, batch_key)` 命中且 `batch_fingerprint` 相同 → **按原顺序返回原 IDs**（`reserved` 状态则先续做未建完的实例再 committed）；fingerprint 不同 → `AgentBatchIdentityConflict`（`error_code="batch_identity_conflict"`，BA-v1.0 §5.1 原文）。
4. **fingerprint 规范化**：`sha256(canonical_json({"owner_scope":…,"batch_key":…,"count":N,"config_hashes":[按提交顺序的 config_hash…]}))`。顺序进 fingerprint（§5.1「规范化 configs 的顺序、配置 hash 和数量进入 batch fingerprint」）。

### 2.3 新表怎么并进同一个 v10（参照 T2 的做法）

Slice 1 的 T2 已经建立范式（`../slice-1/plan.md` T2 段）：

- `schema.py:62-67` `legacy_v9_descriptor()` **冻结不动**（`../slice-1/plan.md` 的 checksum 断言把 v7/v8/v9 三个值硬编码在 `tests/execution/test_base_agent_schema_v10.py:24-29`，S2 一位都不能改）；
- `schema.py:69-73` `fresh_descriptor()` = `legacy_v9_descriptor().sql + base_agent.schema.DDL`，**只要把新表/新列追加进 `sqlite/base_agent/schema.py:11-66` 的 `DDL` 字符串，descriptor 自动重算**，`schema.py` 本身一行不用改；
- `accepted_descriptor_rows()`（`schema.py:76-95`）里的 `ten` 由 `fresh_descriptor()` 现算，也自动跟随；
- 现有测试 `tests/execution/test_base_agent_schema_v10.py::test_descriptor_checksums_are_stable` 只钉 7/8/9（`:24-29`），**不会**因 v10 变动而红；`test_fresh_open_is_v10`（`:65-69`）用集合包含断言，新表只需扩 `BASE_AGENT_TABLES`。

**代价（S2-D9）**：`Database.open` 用 `accepted_descriptor_rows()` 逐组比对（`database.py:235`），**任何已经建出来的 v10 库（本地开发库、测试遗留库）在 S2 之后会拒绝打开**。v10 未发布、无 Host 使用、S1 未提供 v9→v10 就地升级器，故记为已知后果；升级器与迁移回执归 S5 的 BA37。

### 2.4 S2 要追加进 v10 DDL 的内容（清单）

| 改动 | 位置 | 内容 |
|---|---|---|
| 改 `base_agent_bindings_v1` | `sqlite/base_agent/schema.py:12-23` | 增两列：`lifecycle TEXT NOT NULL DEFAULT 'open' CHECK(lifecycle IN ('open','closing','closed'))`、`lifecycle_updated_at REAL` |
| 新表 | 同文件末尾 | `base_agent_creation_batches_v1(batch_id PK, owner_scope, batch_key, batch_fingerprint(64), agent_ids_json, config_hashes_json, state CHECK IN ('reserved','committed'), receipt_json, created_at, updated_at, UNIQUE(owner_scope,batch_key))` |
| 新表 | 同文件末尾 | `base_agent_control_commands_v1(command_id PK, agent_id REFERENCES base_agent_bindings_v1(agent_id), kind CHECK IN ('close','cancel_turn'), target_turn_id, control_generation, request_hash(64), receipt_json, created_at)` + `(agent_id,kind)` 索引 |

**不改** `base_agent_turns_v1.phase` 的 CHECK 枚举（见 S2-D1）。

---

## 3. 同 Agent 两条输入顺序处理：现状与要补的东西

### 3.1 现状（三重结构性保证，均已存在）

1. **持久 FIFO**：`uow.py:4174-4181` —— `SELECT * FROM continuations WHERE run_id=? AND state IN ('pending','claimed') ORDER BY fifo_seq ASC LIMIT 1`，一次只认领一条；`:4184-4196` 对"仍被活租约持有的 claimed 条目"直接返回 `None`（不抢）。
2. **一个 Run 一个 drive task**：`live_index.py:23-27` —— 已有未完成 task 时直接关闭新协程并返回旧 task；`kernel.py:2894-2895` `_schedule` 唯一入口。
3. **提交后串行拉下一条**：`kernel.py:3405-3411` `_reschedule` —— `while run_id in self._live.active_run_ids(): await asyncio.sleep(0)` 后再 `_schedule`；`_finalize_agent_turn(..., reschedule=True)`（`kernel.py:3374-3375`）在每次提交后触发。
4. **seq 由持久行分配**：`sqlite/base_agent/turns.py:177-182` —— `SELECT COALESCE(MAX(seq),0)+1 ... WHERE agent_id=?` 在 `submit_agent_input` 的同一事务内（`uow.py:1230-1273`），`UNIQUE(agent_id,seq)`（`base_agent/schema.py:41`）兜底。

已有间接证据：`tests/agents/test_base_agent_kernel_spike.py::test_two_results_on_one_run_never_terminal`（seq 1、2；第二次请求含第一轮 assistant 回答）与 `::test_input_continuation_is_acked_and_rescheduled`。但这两条是**顺序 submit**，不是**并发 submit**，且没有"不并发写 Context"的直接断言。

### 3.2 缺口

| # | 缺口 | 证据 |
|---|---|---|
| Q1 | **无并发投递的决定性测试**：没有任何测试用 `asyncio.gather` 同时投两条输入 | `grep -n "def test_" tests/agents/*.py` 全表无此用例 |
| Q2 | **`max_pending_inputs` 完全不执法**：`AgentLimits.max_pending_inputs`（`config.py:62`）只在 `to_json()` 里出现；`BaseAgent.submit`（`base.py:92-119`）与 `uow.submit_agent_input`（`uow.py:1216-1273`）都不查队列深度 | 这是 L3 的一部分 |
| Q3 | **无"不并发写 Context"的断言**：Context 写入的幂等/CAS 在 `SqliteContextPort.append`（driver 侧 `execution.py:198-214` 用 `{turn_id}:context:user` 与 `{run_id}:context:instructions` 做 append_id），但没有测试证明两条输入不会交叉写 | review F4 只覆盖"首轮重跑不重复写用户消息" |
| Q4 | **`asyncio.create_task(self._wake_continuation(value))`（`kernel.py:1758`）是即发即忘**：两次 submit 会创建两个 wake task，但 `live_index` 去重使其安全；仍需断言"多次 wake 不产生第二个 drive task" | `kernel.py:1758`、`live_index.py:23-27` |

**S2 的落点**：把配额判定放进 `uow.submit_agent_input` 的**同一事务**（与 review F7 对委派配额的处置同范式，见 `sqlite/base_agent/delegations.py:74-79`），拒绝时抛 `AgentPendingInputsExhausted(UnitOfWorkConflict)`，`BaseAgent.submit` 已有 `except UnitOfWorkConflict` 转译框架（`base.py:111-112`），只需分流成新的公开错误。

---

## 4. `cancel_turn` / `close` 需要哪些内核能力

### 4.1 结论：**一个内核新能力都不需要**，但需要一个内核分支

- `close` **不碰内核**：生命周期是 `base_agent_bindings_v1.lifecycle` 列；闸门下沉到 `uow.submit_agent_input` 的事务里（跨进程、无 TOCTOU）；`Runtime.signal_base_agent_input`（`kernel.py:1718-1760`）不需要改，因为它调用的就是 `self._uow.submit_agent_input`（`:1745`）——**内核仍然不 import `simple_harness.agents`**，闸门在 execution 层。
- `close` 的 drain：`AgentRuntime.close_agent` 轮询 `read_open_agent_turn`（`uow.py:1299-1305`）直到无开放 Turn 或超时；超时返回可查询的 `AgentClosingReceipt`（`state='closing'`，BA-v1.0 §8.4「不无限阻塞调用方」），**不强杀**。
- `cancel_turn` 需要**一个内核分支**：当 Turn 已被判为取消而 driver 仍交出 outcome 时，`_commit_agent_turn`（`kernel.py:3298-3332`）当前会走到 `:3324-3325` 的 `elif self._uow.read_agent_turn_result(...) is None: raise UnitOfWorkConflict("agent turn is closed without a result row")`，再被 `_drive` 的 `except UnitOfWorkConflict`（`kernel.py:3181-3185`）吞掉并 `_abandon_run_authority`——**输入 continuation 永不 ack，Run 卡住**。

### 4.2 `close` 的完整方案

```
close(command_id, *, drain_timeout)
  ├─ 事务1：读 binding；lifecycle='open' → 置 'closing'，control_generation+1，
  │         写 base_agent_control_commands_v1(command_id, kind='close', request_hash, receipt_json)
  │         （同 command_id 重放：request_hash 相同 → 返回原回执；不同 → UnitOfWorkConflict）
  ├─ drain：轮询 read_open_agent_turn(run_id)，直到 None 或 deadline
  └─ 事务2：无开放 Turn → lifecycle='closed'；否则保持 'closing'，回执带 open_turn_id
```

- **拒收闸门**（BA12 前半）：`turns.open_turn` 之前先读 binding，`lifecycle != 'open'` → 抛 `AgentClosedError`；`BaseAgent.submit` 转成已存在的 `AgentClosed`（`contracts.py:66-67`）。
- **跨重启仍 close**（列是持久的）：重启后 `AgentRuntime.open(agent_id)` 拿到的 binding 带 `lifecycle='closed'`，`submit` 继续拒。
- **`runtime.shutdown` 不关闭全部逻辑 Agent**（BA12 后半）：`AgentRuntime.shutdown`（`runtime.py:264-267`）只 `await runtime.close()`（`kernel.py:2159`），**不触碰任何 binding 行**——S2 只需加一条断言测试，代码不用改。
- **close 不等于擦除历史**：`get_result` / `history()`（`base.py:121-133`、`:182-190`）在 closed 后必须仍可读——测试断言。

### 4.3 `cancel_turn` 的完整方案（协作式，不改 `react_loop.py`）

```
cancel_turn(turn_id, *, command_id)
  ├─ 事务：turn.phase ∈ {queued,running} → 写 control command 行（kind='cancel_turn'），
  │        binding.control_generation+1，并把一份规范化的"取消结果"stage 进该 Turn
  │        （复用 turns.stage_result，result_json = AgentTurnResult(state=failed,
  │          error={"error_code":"agent_turn_cancelled", ...})）
  │        phase ∈ {result_pending,committed,failed} → 幂等返回既有事实，不改写
  ├─ 进程内协作取消：driver 把本轮的 CancellationToken 注册在共享表里，
  │   cancel_turn 命中则 token.cancel() → ReActLoop 在 :343 / :733 的 _cancel(...) 抛
  │   asyncio.CancelledError（react_loop.py:1056-1058）
  └─ driver 捕获 CancelledError：仅当 tool_cancel.cancelled 且 cancel.is_cancelled 为假
      且该 Turn 的持久 phase 已是 result_pending（取消结果已 stage）时吞掉，
      返回 DriverResult(WAITING, agent_turn_outcome=已 stage 的取消结果)；否则原样抛
```

关键代码依据：

- `ReActLoop.run` **接受调用方传入的 `tool_cancel`**（`react_loop.py:268` `tool_cancel: CancellationToken | None = None`，`:272` `tool_cancel = tool_cancel or CancellationToken()`），driver 当前没传（`execution.py:227-247`），因此可以传自己的 token 而**不动 `react_loop.py` 一行**。
- 取消检查点在 `react_loop.py:343`（provider 前）与 `:733`（返回结果前），实现是 `_cancel()`（`:1056-1058`）。
- `_commit_agent_turn`（`kernel.py:3308-3323`）已经有 `elif turn.phase == "result_pending": if turn.staged_result_hash != outcome.result_hash: raise ...`。因为 driver 交回的就是**同一份已 stage 的取消结果**，hash 相同 → 直接走到 `_finalize_agent_turn`，continuation 被 ack，Run 保持 WAITING。**内核需要的唯一改动**是让 `_commit_agent_turn` 在 `phase == 'result_pending'` 且 hash 不同时**优先采纳已 stage 的持久结果**而不是抛冲突（取消是先到的事实），或由 driver 保证只回交 staged 结果——**优先选后者**（零内核改动），把"hash 不同"留作真正的完整性错误。

**跨进程边界（必须声明，不写假装通过的测试）**：持有该 Run 的是**另一个进程**时，`token.cancel()` 只能在本进程生效；对端会在本轮自然结束时于 `_commit_agent_turn` 撞上已 stage 的取消结果。S2 的保证是**最终一致**（对端下一次提交尝试时收敛），不是"立刻停止在途动作"；BA-v1.0 §8.4「Turn 进入 CANCELLED 不代表外部现实已回滚」正是这个口径。

---

## 5. `ask` 超时不取消（BA09）：现状与缺口

- `BaseAgent.wait_turn`（`base.py:141-154`）是**纯轮询**：超时只抛 `AgentTurnTimeout(turn_id)`（`contracts.py:70-77`，携带原 turn_id），**没有任何取消副作用**。
- `ask`（`base.py:156-160`）= `submit` + `wait_turn`，超时后 `turn_id` 可继续查询。
- 已有证据：`tests/agents/test_build_agent_runtime.py:178-193 test_ask_timeout_does_not_cancel_the_turn`（provider 阻塞 → 超时 → 放开 → 同 turn 拿到结果，`provider.calls == 1`）。

**缺口**：
1. 没有覆盖**调用方主动取消等待**（`asyncio.wait_for` / `task.cancel()` 打断 `wait_turn` 的 `await asyncio.sleep`）的情形——BA09 原文是「ask 超时／**调用方取消等待**不自动取消底层 Turn」。
2. `AgentTurnTimeout` 只带 `turn_id`，BA-v1.0 §4.2 要求「超时携带原回执」——应带上 `AgentTurnReceipt`（或至少 `seq`/`input_id`），否则调用方要自己拼。
3. `get_result` 对 queued / running / blocked 一律返回 `None`（`base.py:121-133`），BA-v1.0 §4.2 要求「读取已提交结果或**明确 pending／blocked 状态**」——缺一个只读投影（见 §6.3）。

---

## 6. BA11：UNKNOWN / 授权恢复保持同 Turn 同动作身份

### 6.1 S1 review F2 已经做了什么

`kernel.py:3086-3110`：BaseAgent 分支——driver 返回 `WAITING` 且带 `wait_blocker` 或 `authorization_wait` 时，**不 ack** 本轮 continuation，提交阻塞器/授权决策后 `_abandon_run_authority(run_id)` 释放本执行者租约；阻塞解除后由既有 drain 重新激活，**同一条 continuation 被重新认领**（因为它还是 `claimed`/`pending` 状态且在 fifo 队首），于是同一个 Turn 续跑。
`agents/runtime.py:53-71` `AgentRuntimeReconciliation` 在每次 `reconcile()` 调 `provider coordinator.reconcile_incomplete(...)`（consumer 默认端口原是 no-op）。
测试：`tests/agents/test_agent_driver.py:219 test_unknown_provider_outcome_suspends_the_turn_and_resumes_after_reconcile`。

### 6.2 还缺什么（L4 的实质）

| # | 缺口 | 证据 |
|---|---|---|
| R1 | **没有"同 Turn 身份"的持久断言**：`TerminationState.active_turn_id`（`termination.py:196` 附近字段定义 `:190`，写入只在 context-use 保护分支 `react_loop.py:292-297`、`:307-312`）。BaseAgent 的 provider 适配器**没有** `context_use_required`（`grep` 在 `consumer_adapter.py` 无此属性 → `getattr(..., False)`，`react_loop.py:280`），因此该分支永不进入，**`active_turn_id` 恒为 `None`**。S1 计划里"在 checkpoint payload 里带 turn_id"这条**实际上没有落地** | `react_loop.py:280-300`、`termination.py:190` |
| R2 | **恢复时不校验"resume 的是同一个 Turn"**：`mark_agent_turn_running`（`uow.py:1371-1390` → `turns.mark_turn_running` `:264-282`）允许 `phase IN ('queued','running')`，不比对 checkpoint 里的在途请求归属 | `sqlite/base_agent/turns.py:275` |
| R3 | **授权等待路径没有 BaseAgent 用例**：`ToolAuthorizationPending` 在 `execution.py:304-309` 与 provider UNKNOWN 走同一分支，但 `tests/agents/` 里只有 provider UNKNOWN 的用例 | `grep -n "def test_" tests/agents/test_agent_driver.py` |
| R4 | **"不重复预留费用"没有断言**：同 Turn 恢复应复用同一 `request_id`（`react_loop.py:364` `f"{run_id}:provider-turn:{N}"`）与同一 invocation（`provider_invocations.py:151` `invocation_id = sha256(run_id, request_id)`），预留不应二次发生 | BA34 的一半，S5 才做完整版；S2 至少要有同 Turn 的那一条 |
| R5 | **UNKNOWN 期间对外不可见**：`status()` 只给 `open_turn_phase='running'`（`base.py:162-180`），看不出 blocked 与阻塞原因 | 与 §5 缺口 3 同源 |

### 6.3 处置

1. **driver 在轮开始时把 `active_turn_id` CAS 进 checkpoint**（`execution.py:185-194` 现有的 `mark_agent_turn_running` 之后、`self._loop.run(...)` 之前），用 `DurableReactCheckpoint`（`react_checkpoint.py:74-82` `cas`，driver 里已有 `_settle_failed_provider_turn` 的同款用法 `execution.py:337-364`）。
2. **恢复时断言**：若 checkpoint 的 `phase` 处于在途（`provider_reserved`/`tool_batch_reserved`）且 `active_turn_id` 与本次要跑的 `turn_id` 不同 → 抛完整性错误（转成 `DriverResult(FAILED, base_agent_turn_identity_conflict)`，属 BA-v1.0 §1.3 允许 FAILED 的内核完整性故障）。
3. **新增只读投影** `BaseAgent.turn_snapshot(turn_id) -> AgentTurnSnapshot`（`state` + `blocker`(可空) + `seq` + `input_id` + `provider_turn_ordinal_from`），blocker 从 run 的 wait blocker 读；`get_result` 合同保持不变（仍返回 `AgentTurnResult | None`）。

---

## 7. per-turn 限制执法（L3）：不改 `react_loop.py` 的可行路径

### 7.1 机制

| 事实 | 位置 |
|---|---|
| `ReActLoop` 每次取限额都是**读 `self._collaborator.limits`**，不是构造期快照 | `react_loop.py:360-361`（`before_provider`）、`:554-557`（`before_tool_batch`） |
| `AgentLoopCollaborator` 只有一个可变属性 `limits` | `react_loop.py:238-242` |
| 三个总量**永不重置**（reservation 语义） | `termination.py:155`（docstring）、`:350-358`、`:379-381` |
| 超限抛 `TerminationBudgetExceeded` | `termination.py:118-126`、`:351`、`:381`、`:388`、`:616-622` |
| driver **已经**把它转成失败 Turn、Agent 不死 | `agents/execution.py:248-266` |
| driver 已能读 checkpoint 的累计值 | `agents/execution.py:372-383` `_reserved_provider_turns` |

### 7.2 做法

在 `AgentExecutionDriver.start` 里，**按轮构造一个新的 `ReActLoop`**（`ReActLoop` 无跨调用状态，全部状态在 checkpoint 里），其 `AgentLoopCollaborator.limits` 是**派生值**：

```
base = 本 runtime 的 lifetime TerminationLimits（ports.termination_limits）
t0   = 读 checkpoint 得到 (provider_turns_reserved_total, tool_calls_reserved_total, started_at)
per  = 该 Agent 的 AgentConfig.limits

max_turns       = min(base.max_turns,      t0.provider_turns + per.max_model_calls_per_turn)
max_tool_calls  = min(base.max_tool_calls, t0.tool_calls     + per.max_tool_calls_per_turn)
max_wall_seconds= min(base.max_wall_seconds,
                      max(0, turn.created_at - t0.started_at) + per.turn_deadline_seconds)
max_cost_micros / max_consecutive_same_tool 保持 lifetime 值
```

- **`policy_fingerprint` 必须保持不变**：kernel preflight 用 `snapshot.policy_fingerprint` 比对 `driver.policy_fingerprint`（`kernel.py:2915-2917`），而 `ReActLoop` 只把它写进 checkpoint 并做一致性校验（`react_loop.py:331-337`）。因此新 loop 传的仍是 `self.policy_fingerprint`（`agents/execution.py:107`），**派生 limits 不进指纹**。
- **并发安全**：不能改 `self._loop._collaborator.limits`（driver 单例被同一 runtime 的所有 Run 共用），必须**每次 `start()` 新建 loop**。
- **turn deadline 的计时起点**：`_check_common` 用 `now - state.started_at >= limits.max_wall_seconds`（`termination.py:616-617`），`state.started_at` 是**该 Run 首次进入 ReAct 循环**的时刻（`react_checkpoint.py:37-49`），不是本轮起点；用 `turn.created_at`（首次持久准入，`base_agent/schema.py:38`）折算成偏移量即可满足 BA-v1.0 §10「deadline 从首次持久准入开始计时，恢复不延长它」。首轮会出现 `turn.created_at < started_at` 的负偏移（创建时 driver 不建 checkpoint，checkpoint 直到第一轮才诞生），故取 `max(0, ·)`，**首轮 deadline 略短**——记为 S2-D6 的已知副作用。

### 7.3 代价

- 每轮多一次 checkpoint 读（driver 里已有一次 `_reserved_provider_turns`，合并成一次读三个字段即可，净增 0）。
- 超限的表现是**失败的 Turn**（`error_code = react_max_turns_exceeded` 等，`termination.py:120-125`），不是拒绝新输入——与 BA-v1.0 §1.3「Turn 记录失败，Agent 回到 IDLE」一致。
- `max_pending_inputs` 不走这条路（它是队列配额，落在 `submit_agent_input` 事务里，见 §3.2）。

---

## 8. `open` 的 owner_scope 校验（BA05）

**现状**：`AgentRuntime.open`（`runtime.py:327-332`）只查 `read_agent_binding(value)`，**完全不比对 `self._owner_scope`**；`owner_scope` 虽然进了 `agent_id_for` 的摘要（`runtime.py:190`）与 binding 行（`base_agent/schema.py:15`），但只要拿到 id 就能跨 owner 打开，`BaseAgent` 随后可读 `config`（含 `instructions` 原文）、`history()`、`get_result()`。

**处置**：`open` 在 binding 存在但 `binding.owner_scope != self._owner_scope` 时抛 **`AgentNotFound`**（与"不存在"**同一异常、同一消息模板**，不泄露存在性——BA-v1.0 §13 BA05「错误 owner 或不存在身份不泄露内容」）。
**oracle 要点**：断言两种情况的 `type(err)` 与 `str(err)` **逐字相等**；断言 `str(err)` 不含 `config.name` / `instructions` / `run_id`；断言跨 owner 的 `get_result` / `history` 拿不到内容。

`signal_base_agent_input`（`kernel.py:1718-1760`）不加 owner 校验：`BaseAgent` 句柄只能从 `create`/`open` 获得，两者都被 owner 闸门覆盖；内核层加 owner 概念会把 `agents` 的语义漏进 `runtime`（违反分层纪律）。记为边界。

---

## 9. checkpoint 分键（D7）的代价与裁决

### 9.1 事实

| 事实 | 位置 |
|---|---|
| checkpoint 是 **run_id 单键 + 固定 namespace** | `react_checkpoint.py:93-100`（`cas_react_checkpoint(run_id=...)`）、`:114`（`namespace != "react.termination.v1"` 即报错） |
| port 方法签名只有 run_id | `kernel.py:315-326` `ReactCheckpointPort` |
| provider 请求身份 = `{run_id}:provider-turn:{N}`，N 来自 run 级永不重置的累计 | `react_loop.py:364`、`termination.py:352-358` |
| invocation 身份 = `sha256(run_id, request_id)` | `provider_invocations.py:151` |
| 初始锚 version==0 不可被覆盖 | `react_checkpoint.py:67-69`（`../investigation.md` §16.5 列为必须保留） |
| 每轮的 provider 归属**已经**被记录 | `base_agent_turns_v1.provider_turn_ordinal_from/to`（`base_agent/schema.py:35-36`），driver 在 `execution.py:186`、`:325` 填写 |

### 9.2 裁决：**S2 明确不分键**

理由（按强度排序）：

1. **分键直接违反 BA34**：`(agent_id, turn_id)` 分键意味着每个 Turn 从空 `TerminationState` 起步，`provider_turns_reserved_total` 从 0 起 → 第二个 Turn 的第一个请求又叫 `{run_id}:provider-turn:1`，与第一个 Turn 的请求**撞 request_id**，进而撞 `invocation_id`。要避免就得同时改 request_id 格式（= 改整个 Provider ledger 身份，`../investigation.md` D6 已裁决"不改"）。
2. **lifetime totals 会丢**：`lifetime_model_calls` / `lifetime_tool_calls` / `lifetime_cost` 的执法完全建立在 run 级累计上（`config.py:95-107` → `termination_limits()`），分键后无处累计。
3. **改动面是新 port 方法 + 新 namespace**，不是加参数（`../investigation.md` D7 已经这样判断）；`ReactCheckpointPort` 是公开 runtime 面（`kernel.py:315-326`），改它会牵动 `public-api.json`。
4. **收益已被更便宜的手段拿到**：per-turn 归属由 `provider_turn_ordinal_from/to` 表达（已存在）；per-turn 限额由 §7 的派生 limits 表达（不需要分键）；同 Turn 身份由 `active_turn_id` 表达（字段已存在，只差写入）。

**S2 交付的替代物**：把 `active_turn_id` 真正写进 checkpoint（§6.3），并在 `../slice-2/journal.md` 与本文里把"不分键"记成显式决定，撤销 `../investigation.md` D7 里"推迟到 Slice 2 分键"的说法。若将来真要分键，须与 BA34 的 request_id 方案一并立项（S5 或独立项）。

---

## 10. uow facade 下沉（L12）

**现状**：`uow.py` 的 BaseAgent 段在 `:1175-1541`（`# --- BaseAgent (execution schema v10) ---` 起）。其中：

| facade | 行数 | 是否只是转发 |
|---|---|---|
| `create_agent_binding` `:1179-1204` | 26 | 转发 |
| `submit_agent_input` `:1216-1273` | 58 | **含内联业务**（continuation payload 注入 seq、既有行比对、`_enqueue_continuation_on_connection`） |
| `reserve_child_base_agent_delegation` `:1317-1348` | 32 | 转发 |
| `mark_agent_turn_running` `:1371-1390` | 20 | 转发 + 租约校验 |
| `stage_agent_turn_result` `:1392-1424` | 33 | 转发 + 租约 + fault 钩子 |
| **`commit_agent_turn_result_and_idle` `:1426-1541`** | **116** | **大段内联**：结果行、continuation progress receipt、ack CAS、runs CAS、`_insert_event` |

阻塞点（S1 journal L12 的原话）：这 6 个方法**复用 `uow` 的私有 helper**——`_require_runtime_lease`、`_require_continuation_claim`、`_read_continuation_progress_receipt`、`_enqueue_continuation_on_connection`、`_insert_event`、`_object_json`、`_required`、`_time`、`_fault`。

**S2 的下沉方案（低风险、纯搬运）**：
1. 把这些私有 helper 中**纯连接级**的几个（`_insert_event`、`_object_json`、`_required`、`_time`、`_fault`）以**参数注入**或 module 级函数的形式提供给 `sqlite/base_agent/turns.py`；
2. `commit_agent_turn_result_and_idle` 的**主体**搬进 `sqlite/base_agent/turns.py::finalize_turn(connection, *, ..., insert_event, require_claim)`，`uow` 侧只剩"开事务 + 传 helper + fault 钩子"（目标 ≤ 25 行）；
3. `submit_agent_input` 同法搬进 `turns.py::submit_input(...)`。
4. **验收只看行为不变**：`tests/agents/test_turn_finalize.py`（5 例，含 fault 注入的"全有或全无"）与 `tests/agents/test_base_agent_kernel_spike.py`（3 例）**一行不改全绿**，这是纯重构的判据。

---

## 11. 规范与现实的偏差（S2 专属，编号续 `../investigation.md` 的 D1–D16）

| # | 规范的说法 | 现实 | 处置 |
|---|---|---|---|
| **S2-D1** | §8.4「Turn 进入 CANCELLED」 | `base_agent_turns_v1.phase` 的 CHECK 无 `cancelled`（`base_agent/schema.py:32`）；`AgentTurnState` 无 CANCELLED（`contracts.py:37-42`）；`AgentTurnResult.__post_init__` 只接受 committed/failed（`contracts.py:149-151`）；`commit_staged_result` 按 body.state 二选一（`turns.py:344`） | **S2 不加枚举值**：取消表达为 `state=failed` + `error={"error_code":"agent_turn_cancelled","source_kind":"control"}`，turn phase 落 `failed`。理由：加枚举会牵动 4 个文件的校验与所有读路径，收益只是命名。**代价**：调用方要看 `error_code` 才知道是取消而非业务失败——写进 API docstring 与验收条款 |
| **S2-D2** | §8.4「close 先进入 CLOSING…返回可查询的 closing receipt」 | 内核唯一的"停"是 `CANCEL_REQUESTED → CANCELLED` 终态（§1.1 六处证据），与"永不终态"互斥 | close **完全不走内核 cancel**：`base_agent_bindings_v1.lifecycle` 列 + `base_agent_control_commands_v1` 回执 + `submit_agent_input` 事务闸门。`RunClient.cancel` 对 BaseAgent Run 仍应被围栏拒绝（S1 的 AC7 已覆盖 `start_conversation`/`signal_conversation`；**S2 补一条 `RunClient.cancel` 的围栏用例**——现状 `_cancel_run` 不查 api_mode，是个缺口） |
| **S2-D3** | §5.1「在一个短事务中保存批次回执、实例配置和稳定身份」 | bindings.run_id 外键指向 runs（`base_agent/schema.py:14`），runs 只能逐个建（`kernel.py:1702-1716`） | 两段式批次行 + **写前整批准入校验**；BA04 由"校验先于任何写"保证，崩溃残留由同 batch_key 续做收敛（§2.2） |
| **S2-D4** | §8.2「内部 request_id = agent_id + turn_id + local_model_step」 | `f"{run_id}:provider-turn:{N}"`（`react_loop.py:364`），N 为 run 级永不重置 | 沿用 `../investigation.md` D6 的裁决"不改格式"；S2 只补 `active_turn_id` 到 checkpoint 做同 Turn 身份断言 |
| **S2-D5** | §9.3 / D7「新模式以 `(agent_id,turn_id)` 管理 checkpoint」 | run_id 单键 + 固定 namespace（`react_checkpoint.py:93-100`、`:114`） | **S2 明确不分键**，四条理由见 §9.2；撤销 D7 的"推迟到 Slice 2"说法 |
| **S2-D6** | §4.1 AgentLimits 的 4 个 per-turn 上限 | 只记录不执法（`config.py:62-65` 仅进 `to_json`；S1 review F13 / journal L3） | driver 按轮派生 `TerminationLimits`（§7），**不改 `react_loop.py` / `termination.py`**。副作用：首轮 `turn_deadline` 的计时起点是 checkpoint `started_at`，取 `max(0,·)` 后首轮略短 |
| **S2-D7** | §4.3「共享构建配置需要明确最大实例数、每批数量、Provider 并发、工具并发、全局待处理上限」 | `AgentRuntimePorts`（`ports.py:34-60`）一个配额字段都没有 | S2 只加 `max_agents` / `max_batch_size`（批量准入需要）与 `max_pending_inputs` 执法（已在 `AgentLimits`）；**Provider/工具并发与 FIFO 轮转公平归 S5 的 BA35**，S2 不假装做到 |
| **S2-D8** | §4.2「get_result 读取已提交结果**或明确 pending／blocked 状态**」 | `get_result` 对 queued/running/blocked 一律 `None`（`base.py:121-133`） | 新增只读投影 `turn_snapshot(turn_id)`；`get_result` 合同不变（避免破坏 S1 的既有用例与端到端脚本） |
| **S2-D9** | §11「只分配一个 v10」 | 追加 DDL 会重算 `fresh_descriptor().checksum`（`schema.py:69-73`），**已存在的 v10 库将拒绝打开**（`database.py:235`） | 接受：v10 未发布、无 Host 使用、S1 本就没有 v9→v10 就地升级器。写进验收「明确不包含」与 journal；升级器归 S5 BA37 |
| **S2-D10** | §13 BA05「错误 owner 不泄露内容」 | `AgentRuntime.open` 完全不校验 owner_scope（`runtime.py:327-332`） | 越权与不存在**共用 `AgentNotFound` 与同一消息模板**（§8） |
| **S2-D11** | §5.2「同一 Agent 最多一个开放 Turn 有效执行者；其他输入持久排队」 | 已成立（§3.1 三重结构），但**队列深度无上限**，`max_pending_inputs` 不执法 | 配额判定并进 `submit_agent_input` 事务（与 delegate 配额 review F7 同范式，`delegations.py:74-79`） |

---

## 12. S1 遗留在 S2 的落点

| 遗留 | 出处 | S2 落点 | 判据 |
|---|---|---|---|
| **L3** per-agent / per-turn 限制只记录不执法 | journal §6 L3、review F13 | 任务 T6：driver 派生逐轮 `TerminationLimits`（§7）；`max_pending_inputs` 落 `submit_agent_input` 事务（§3.2） | `tests/agents/test_turn_limits.py`：超 per-turn 模型调用 → 失败 Turn 且 Agent 仍能接下一条；lifetime 计数不被重置 |
| **L4** UNKNOWN 期间 AgentTurn 停在 running 的恢复语义 | journal §6 L4、附 D.4 | 任务 T7：`active_turn_id` 写入 + 恢复身份断言 + `turn_snapshot` blocked 投影 + 授权等待用例（§6.3） | `tests/agents/test_turn_resume_identity.py` |
| **L12** uow 新 facade 未下沉（`commit_agent_turn_result_and_idle` ≈116 行） | journal §6 L12、审计缺口 2 | 任务 T9：主体搬进 `sqlite/base_agent/turns.py`（§10） | 纯重构：`test_turn_finalize.py` + `test_base_agent_kernel_spike.py` 一行不改全绿；facade ≤ 25 行 |
| **D7** checkpoint 分键 | `../investigation.md` D7 | 任务 T7 + T9：**裁决"不分键"**并记账（§9.2），交付 `active_turn_id` 作为替代物 | `test_turn_resume_identity.py::test_checkpoint_stays_single_key_and_records_active_turn` |
| L1 / L2 / L9 / L11 | journal §6 | **不属于 S2**（S5 BA31/BA37、S3） | 验收「明确不包含」逐条列出 |
| **L5 / F-BA-1** | journal §6 L5「下一片优先」 | **用户给 S2 划的范围不含它**，见 §15，需主编排者裁决 | — |

---

## 13. 本片必须原样保留的既有约束（自检清单）

在 `../investigation.md` §16 十条之上，S2 额外自检：

1. `start_snapshot.py` / `drivers/start_mode.py` **文件 sha256 不变**（S1 的 `test_api_mode_fence.py` 已有冻结闸）。
2. **`react_loop.py` 与 `termination.py` 一行不改**（per-turn 限制走派生 limits，取消走既有 `tool_cancel` 入口）。
3. `legacy_v7/v8/v9_descriptor().checksum` 三个值一位不变（`tests/execution/test_base_agent_schema_v10.py:24-29` 硬编码）。
4. `migrate_execution_to_v9` 仍升到 **9**（`tests/execution/test_short_context_migration_still_targets_v9.py`）。
5. **`DriverResult` 的互斥规则不放宽**（`kernel.py:218-243`）：取消结果仍然走 `agent_turn_outcome` 独占 WAITING 载体。
6. **根包 `simple_harness.__all__` 与 `public-api.json` 不动**：S2 的新符号（`AgentClosingReceipt`、`AgentBatchIdentityConflict`、`AgentTurnSnapshot` 等）**只从 `simple_harness.agents` 导出，不加进根包**，否则 `tests/unit/contracts/test_public_api.py:14-26` 的 `list(simple_harness.__all__) == snapshot[...]` 与 version 相等断言会红，`__version__` 也要跟着动。
7. `runtime/kernel.py` 与 `runtime/agent_turn.py` **永不 import `simple_harness.agents`**（`test_config_contracts.py` 的 ast 扫描）。
8. 不 import `simple_harness_memory`（`test_build_agent_runtime.py::test_no_memory_entrypoint_is_called` 的源码扫描覆盖整个 `agents/`）。
9. 事务内不 await 网络；base_agent helper 接受外层 connection、不自开事务。
10. 固定回归口径（`../baseline.md`）：红集 ⊆ 基线红集，新红一条即阻断。

---

## 14. 建议的任务边界与价值链（供 plan.md）

最短价值链 = **"停而不死"的控制面**先落地，其余按依赖排：

```
schema v10 增补（close/批次/控制命令需要的持久位）
  → 输入队列护栏与顺序保证（BA08）
  → close 控制面（BA12）
  → 【里程碑：一条 pytest = 顺序处理 + close 拒收 + 重启仍 close】
  → open owner_scope（BA05）
  → create_many 批量幂等（BA02/03/04）
  → per-turn 限制与 turn deadline（L3）
  → UNKNOWN/授权恢复同 Turn 同动作身份（BA11 / L4 / D7）
  → cancel_turn 最小合同 + BA10
  → uow facade 下沉（L12）+ 收尾
```

---

## 15. 未归属项：L5 / F-BA-1（需主编排者裁决）

**事实**：S1 journal §6 L5 把真实端点的 `provider_response_not_durable` 标为「**F-BA-1，下一片优先**」（4 次真实 provider 跑里 2 次失败），但**用户为 S2 划定的 MUST/次要/遗留清单里没有它**。

**根因候选（已定位到两条闸门，均在 `provider_invocations.py`）**：

1. `provider_invocations.py:282-287`：响应里含 `reasoning`/`thinking`/`chain_of_thought` 内容块，而 `capability.mode` 不是 `OPAQUE_REFERENCE` 且 `response.opaque_continuation_ref is None` → `ValueError("opaque reasoning continuation requires a public reference")`。
2. `provider_invocations.py:258-259`：同类隐藏块在 `_durable_public_message_json` 里 → `ValueError("provider hidden reasoning cannot enter durable response state")`；`:255-256` 非白名单内容块 → `ValueError("provider response contains a non-public content block")`；`:244-247` 纯文本为空且无 tool_calls → `_public_text` 报错。

默认能力是 `ProviderContinuationCapability(mode=REASONING_DISABLED)`（`providers/base.py:174-175`），而 `AgentRuntimePorts` **没有** `continuation_capability` 字段（`ports.py:34-60`），`build_agent_execution_driver` 的同名参数被默认值写死（`agents/execution.py:393`）。DeepSeek 的 reasoner 型响应正好命中第 1/2 条。

**建议（成本低、可确定性验证）**：
- 给 `AgentRuntimePorts` 加 `continuation_capability: ProviderContinuationCapability`，透传到 `build_agent_execution_driver`（该参数已存在）与 `provider_binding_fingerprint`（`agents/execution.py:405-407`）；
- 加两条**确定性** mock 用例：provider 返回 (a) 含 reasoning 块、(b) 空文本且无 tool_calls，断言当前是 `provider_response_not_durable` 的失败 Turn 且 Agent 存活，配置成 `OPAQUE_REFERENCE` + `opaque_continuation_ref` 后转为成功；
- 真实端点抓一份原始响应作为证据（可 skip）。

**本片处置**：写进 plan 的「明确不包含」，理由是与 S2 主要矛盾（停而不死的控制面）无耦合、且属真实端点协议问题；**请主编排者裁决**是并入 S2（+1 个任务，总数 10）、单独立项，还是并进 S3 的 Context 工作。
