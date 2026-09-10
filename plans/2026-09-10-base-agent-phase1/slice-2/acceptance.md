plan-status: finalized (由主编排者依据 BA-v1.0 拆片，2026-09-10)

# 验收标准：Slice 2 · 生命周期与批量创建

## 矛盾分析

**主要矛盾（一句话）**

> 要让 BaseAgent 有一个可控的持久生命周期（能被批量造出、能排队、能叫停、能关闭），就必须有"停"的语义；可 SDK 内核里唯一的"停"是 `CANCEL_REQUESTED → _terminalize_cancelled → RunState.CANCELLED`（`kernel.py:2962-2966`、`kernel.py:3223-3262`、`kernel.py:3413-3455`），而 BaseAgent 的执行身份**永不终态**是 Slice 1 交付的立身之本——于是"可控的生命周期"与"永不终态的执行身份"在现有内核里互相排斥。

**价值在前**

调用方今天能用 BaseAgent 干活（S1 已证明），但**不敢用**：投多条输入不知道会不会乱序，关不掉，队列没有上限，一个配置模板造 100 个实例只能顺序 `create` 且崩溃后无从收敛。S2 交付的就是"敢用"——**先能顺序吃输入、能干净关掉且重启后仍然关着**，其余是加固。

**矛盾的主要方面**

是**"停而不死"的控制面**（`close` / `cancel_turn` 的持久控制状态与拒收闸门），**不是批量创建**。
批量创建是纯持久化与幂等问题，与内核语义没有冲突（现成的 `creation_key` 幂等、稳定 `agent_id` 摘要都在），只要"写前整批校验 + 两段式批次行"就能做对；而"停"必须**绕开执行内核的取消路径**另建一层持久生命周期（binding 的 `lifecycle` 列 + 控制命令回执 + `submit` 事务闸门），否则一关就死、一取消就终态，S1 的价值当场作废。因此资源优先投在 T2（顺序保证）与 T3（close 控制面），二者合成 T3.5 的里程碑。

**产生原因**

1. `kernel.py:3223-3262` / `:3413-3455`：`_cancel_run` 与 `_terminalize_cancelled` 把"停"直接等同于终态。
2. `kernel.py:2962-2966`：`_drive` preflight 见到 `CANCEL_REQUESTED` 即终态化。
3. `uow.py:4167-4173`：Run 一旦终态，所有 pending/claimed continuation 被 quarantine，Agent 再也收不到输入。
4. `kernel.py:3171-3180`：`asyncio.CancelledError` 在非 `CANCEL_REQUESTED` 时**不做任何处置**，直接 `self._cancels[run_id].cancel()` 会留下悬挂的 continuation claim。
5. `agents/config.py:62-65`：per-turn 限制只进 `to_json()`，从不执法（S1 review F13 / journal L3）。
6. `agents/runtime.py:315-325`：`create_many` 只是顺序 `create`，无批次行、无 fingerprint、无冲突判定、无整批回滚。
7. `agents/runtime.py:327-332`：`open` 完全不校验 `owner_scope`。

**解决方向**

- **close 不碰内核**：`base_agent_bindings_v1` 增 `lifecycle('open'|'closing'|'closed')` 列；新表 `base_agent_control_commands_v1` 存幂等回执与 `control_generation`；拒收闸门下沉到 `sqlite/base_agent/turns.py::open_turn`（execution 层，跨进程、无 TOCTOU），**内核仍不 import `simple_harness.agents`**。
- **顺序处理靠既有结构 + 一道护栏**：continuation FIFO（`uow.py:4174-4181`）、单 drive task（`live_index.py:23-27`）、finalize 后 `_reschedule`（`kernel.py:3405-3411`）已经保证串行；S2 补 `max_pending_inputs` 的**事务内配额判定**与决定性测试。
- **cancel_turn 走协作式取消**：把 driver 自己的 `CancellationToken` 传进 `ReActLoop.run(tool_cancel=…)`（该参数已存在，`react_loop.py:268`），**不改 `react_loop.py`**；取消结果先 stage 再由既有 finalize 路径提交，`_commit_agent_turn` 一行不改。
- **批量创建两段式**：写前整批准入校验（BA04 因此零副作用可测）→ `reserved` 批次行 → 逐个 `create`（已幂等）→ `committed` 回执；同 key 同 fingerprint 返回原 IDs，不同则 `batch_identity_conflict`。
- **per-turn 限制按轮派生 `TerminationLimits`**：`ReActLoop` 每次都读 `self._collaborator.limits`（`react_loop.py:360-361`、`:554-557`），totals 又是 run 级永不重置（`termination.py:350-358`），故"本轮起点总量 + per-turn 上限"即可精确执法，**`react_loop.py` / `termination.py` 一行不改**，`policy_fingerprint` 不变。
- **checkpoint 明确不分键**：分键会让 `{run_id}:provider-turn:{N}` 跨轮撞 request_id，直接违反 BA34；改为把已有但从未被写的 `active_turn_id` 写进 checkpoint，作为"同 Turn 恢复"的断言锚。

**最小验证动作**

价值验证里程碑（第 3 个任务后即可跑）：

```
.venv/bin/python -m pytest -q tests/agents/test_agent_lifecycle.py
```

一条用例串起：并发投两条输入 → 严格按 seq 1、2 顺序处理（第二次请求含第一轮回答）→ `close` → 第三条输入被拒（`AgentClosed`）→ Run 仍 `WAITING`、run_events 无终态 → 重启后 `open` 同一 Agent，`submit` **仍**被拒、历史仍可读。

全片：

```
.venv/bin/python -m pytest -q tests/agents tests/execution/test_base_agent_schema_v10.py
```

---

## 范围

### 包含

1. schema v10 **增补**（同一个 v10、重算描述符）：`base_agent_bindings_v1` 增 `lifecycle` / `lifecycle_updated_at`；新表 `base_agent_creation_batches_v1`、`base_agent_control_commands_v1`。
2. 输入队列护栏：`max_pending_inputs` 在 `submit_agent_input` 的**同一事务**内执法；同 Agent 多条输入的顺序保证与"不并发写 Context"的决定性测试。
3. `BaseAgent.close(*, command_id)` / `AgentRuntime.close_agent(...)`：`open→closing→closed`、幂等回执、drain 带超时、可查询的 closing receipt、跨重启持久、不擦除历史；`runtime.shutdown()` 不关闭任何逻辑 Agent。
4. `AgentRuntime.open` / `create` / `binding` 的 `owner_scope` 闸门：越权与不存在**同一异常、同一消息模板**。
5. `create_many` 完整批量语义：写前整批准入（`max_batch_size` / `max_agents` / 工具目录）、batch fingerprint、同 key 同内容返回原 IDs、不同内容 `batch_identity_conflict`、任一非法整批零写入、崩溃后同 `batch_key` 续做。
6. per-turn 限制执法（L3）：`max_model_calls_per_turn` / `max_tool_calls_per_turn` / `turn_deadline_seconds` 按轮派生 `TerminationLimits`，超限 = 失败的 Turn、Agent 不死、lifetime 计数不重置。
7. UNKNOWN / 授权恢复的同 Turn 同动作身份（L4）：checkpoint 写 `active_turn_id`、在途 checkpoint 上换 Turn 被拒、`turn_snapshot` 的 blocked 投影、不重复预留费用。
8. `ask` 超时与**调用方主动取消等待**都不取消底层 Turn；`AgentTurnTimeout` 携带原回执。
9. `cancel_turn(turn_id, *, command_id)` 最小合同：协作式取消、`control_generation` 递增、已发生 effect 不回滚、跨进程最终一致（显式声明）。
10. `uow` 的两个大 facade 下沉到 `sqlite/base_agent/turns.py`（L12），**纯重构**。
11. D7 记账：**S2 明确不分键**，理由与替代物写进 `investigation.md` §9.2 与 journal。

### 明确不包含

- 有界 Context 装配、真实 tokenizer 计数、ProtocolGroup、增量 Journal（S3 的 BA13–BA19/BA22）。
- 混合召回、Agent 间检索隔离、索引世代与降级（S4）。
- **多 Agent 并发公平限流、Provider/工具并发槽位、FIFO 轮转调度**（S5 的 BA35）；本片只做**单 Agent 的队列深度配额**，不承诺跨 Agent 公平性。
- 故障注入矩阵；`react_loop.py:717-732` 最终 CAS 与 stage 之间的**残留冻结窗口**（S5 的 BA31，S1 遗留 L1）。
- 已有 v9 库的就地升级器与迁移回执（S5 的 BA37）；**本片之后，任何已存在的 v10 开发库会因描述符重算而拒绝打开**（investigation S2-D9），本片不提供升级路径。
- exact-wheel conformance 与发布说明（S5 的 BA40）。
- **`RunClient.cancel` 对 BaseAgent Run 的围栏缺口**：只暴露测试并记遗留，正式围栏归 S5（plan 附 C.1）。
- ~~L5 / F-BA-1~~：主编排者裁决**并入本片**（AC13，次要），见 plan T10。
- 真正的 `CANCELLED` Turn 阶段枚举（investigation S2-D1：取消表达为 `state=failed` + `error_code="agent_turn_cancelled"`）。
- `memory.search` / `memory.read`（S4）；向模型暴露 `create_many` / `close` / `cancel_turn`（控制面只对调用方开放）。
- 修复 `../baseline-known-failures.txt` 里的既有红；任何对 `simple_harness_memory` 的依赖。
- 根包 `simple_harness.__all__` 与 `public-api.json` 的任何变化（新符号只从 `simple_harness.agents` 导出）。

---

## 功能验收条款

> MUST（必须）**8 条**，决定性条款排前。

| ID | 功能点 | 验收条件（可验证） | 矛盾地位 | 优先级 | 对应 BA | 最小决定性测试 |
|---|---|---|---|---|---|---|
| **AC1** | `close` 拒收新输入 · Agent 不死 · 跨重启有效 · `shutdown` 不关闭逻辑 Agent | (a) `close(command_id)` 后 `submit` 抛 `AgentClosed`，`base_agent_turns_v1` 不新增行；(b) `read_run(run_id).state is RunState.WAITING`，run_events 全量回放**无** `completed/failed/cancelled`；(c) 同 `command_id` 重放返回逐字段相同的 `AgentClosingReceipt`，控制命令行仍 1 行、`control_generation` 只 +1；(d) 有开放 Turn 且 drain 超时 → 回执 `state="closing"` 且带 `open_turn_id`，**不无限阻塞、不强杀**，该 Turn 随后正常 committed；(e) 进程重启后 `open` 同一 Agent，`submit` **仍**抛 `AgentClosed`，`history()` / `get_result` 不变；(f) `runtime.shutdown()` 之后直接读库，全部 binding `lifecycle=='open'`、`control_generation==0` | 决定性 | 必须 | BA12 | `tests/agents/test_agent_close.py::{test_close_rejects_new_input_and_keeps_the_agent_non_terminal,test_close_is_idempotent_by_command_id,test_close_drains_the_open_turn_and_returns_a_queryable_receipt,test_closed_agent_survives_restart,test_shutdown_does_not_close_any_agent}` |
| **AC2** | 同 Agent 两条并发输入顺序处理 · 不并发写 Context | 并发 `asyncio.gather` 投 i1/i2 后：(a) 两 Turn 的 `seq` 为 1、2 且与 `continuations.fifo_seq` 顺序一致；(b) provider 恰好 2 次调用，第 2 次请求含第 1 轮 assistant 回答与第 1 条用户消息；(c) 两条 continuation 均 `ACKED`；(d) `SqliteContextPort.append` 的调用区间两两不重叠、`expected_revision` 严格递增、无重复 `append_id`、零 `UnitOfWorkConflict`；(e) 同一 run 的 `_drive` 执行区间不重叠；(f) 队列深度达 `max_pending_inputs` 时第三条投递抛 `AgentPendingInputsExhausted` 且**零行写入**，幂等重放不受配额影响 | 决定性 | 必须 | BA08 | `tests/agents/test_input_queue.py::{test_two_concurrent_inputs_are_processed_in_order,test_context_is_never_written_concurrently,test_only_one_drive_task_per_run,test_pending_inputs_quota_rejects_the_third_input,test_quota_does_not_block_idempotent_replay}` |
| **AC3** | `ask` 超时 / 调用方取消等待都不取消底层 Turn | (a) provider 阻塞时 `ask(timeout=0.05)` 抛 `AgentTurnTimeout`，`err.receipt.turn_id == err.turn_id` 且 `err.receipt.seq` 正确（**超时携带原回执**，BA-v1.0 §4.2）；(b) 放开后同 `turn_id` 拿到结果，`provider.calls == 1`；(c) 另起 task 等 `wait_turn` 后 `task.cancel()`，底层 Turn 仍照常提交、`provider.calls` 不增；(d) 全程 `base_agent_turns_v1` 该行 phase 不因等待方行为改变 | 决定性 | 必须 | BA09 | `tests/agents/test_turn_resume_identity.py::test_ask_timeout_carries_the_receipt_and_caller_cancel_does_not_cancel_the_turn` + 既有 `tests/agents/test_build_agent_runtime.py::test_ask_timeout_does_not_cancel_the_turn`（一行不改仍绿） |
| **AC4** | 授权 / UNKNOWN 恢复保持同 Turn 与相同动作身份 | (a) provider UNKNOWN → 阻塞器；该 Turn `phase=='running'`，checkpoint `provider_request_id` 不变（**执行期修订**：`active_turn_id` 绑定 Context-use 协议不可借用，同 Turn 身份改由持久序号规则断言，见 `challenge-round-1.md` 文末）；(b) `reconcile` 后**同一** Turn 续跑，provider invocation 行仍 **1** 条（同 `invocation_id`，不重复预留费用），continuation 只被 ack 一次，`seq` 未变；(c) 授权 pending→allow 后同 Turn 续跑，effect 的 `effect_id`/`call_id` 与首次一致，不产生第二个 effect 行；(d) 在途 checkpoint（`phase in {provider_reserved, tool_batch_reserved}`）上驱动**另一个** turn_id → `DriverResult(FAILED,"base_agent_turn_identity_conflict")` 且 provider 不被调用；(e) UNKNOWN 期间 `turn_snapshot(turn_id).blocked is True` 且带 blocker，`get_result` 仍返回 `None`（合同不变） | 必须 | 必须 | BA11 | `tests/agents/test_turn_resume_identity.py::{test_unknown_provider_resumes_the_same_turn_with_the_same_request_identity,test_authorization_wait_resumes_the_same_turn,test_resuming_a_different_turn_on_an_inflight_checkpoint_is_refused,test_turn_snapshot_reports_blocked_with_a_reason}` |
| **AC5** | 同 `batch_key` 同内容返回原 IDs；不同内容明确冲突 | (a) 二次 `create_many` 同内容 → 返回的 `agent_id` 序列**逐位相等**，`base_agent_creation_batches_v1` 仍 1 行、`bindings` 行数不变；(b) 跨进程重启后第三次调用仍返回同一序列；(c) 任一 config 内容变化（含顺序变化）→ `AgentBatchIdentityConflict`，`code == "batch_identity_conflict"`，且 binding/run/batch 行数**逐表不变**；(d) 在 `reserved` 之后崩溃，重试同 `batch_key` 续做补齐，`agent_ids` 与预留完全一致、batch 行仍 1 行 | 必须 | 必须 | BA03 | `tests/agents/test_create_many_batches.py::{test_same_batch_key_same_content_returns_the_same_ids,test_same_batch_key_different_content_conflicts,test_reserved_batch_is_resumed_not_duplicated}` |
| **AC6** | 一项配置不合法 → 整批不留部分成功 | 5 个 config、第 3 个的 `tool_names` 含运行时目录里没有的工具 → 抛 `AgentBatchRejected` 且 `index == 2`；断言 `base_agent_bindings_v1`、`runs`、`conversation_run_modes`、`base_agent_creation_batches_v1`、`continuations` 的行数**与调用前逐表相等**（零副作用）；`max_batch_size` / `max_agents` 越界同样在**任何写入之前**拒绝 | 必须 | 必须 | BA04 | `tests/agents/test_create_many_batches.py::{test_invalid_config_leaves_no_partial_batch,test_batch_size_and_instance_caps_are_enforced_before_any_write}` |
| **AC7** | 创建 100 个 IDLE Agent 不发 100 次 Provider 请求、不复制模型服务 | `create_many([cfg]*100, batch_key=…)` 后：(a) `provider.calls == 0`，`base_agent_turns_v1` 0 行；(b) 100 个互不相同的 `agent_id`/`run_id`，任取两个的 Context `revision == 0`；(c) 全过程只有**一个** `ProviderInvocationCoordinator` 实例、`runtime.kernel._ports.provider` 对象同一（`id()` 相等）；(d) 重启后 `recover()` **不激活**这些 Run（`_live.active_run_ids()` 为空、`provider.calls` 仍 0） | 必须 | 必须 | BA02 | `tests/agents/test_create_many_batches.py::test_hundred_idle_agents_make_no_provider_request` |
| **AC8** | `open` 只打开已存在 Agent；错误 owner 不泄露 | (a) 跨 `owner_scope` 的 `open` 与"不存在的 id"的 `open` 抛**同一异常类** `AgentNotFound`，且消息模板逐字相同（只差 id）；(b) 异常消息不含目标 Agent 的 `name` / `instructions` / `run_id` / `creation_key`；(c) 跨 owner 无法经 **`open` / `binding` / `create` / `BaseAgent` 句柄**读到该 Agent 的结果或配置（`binding(...) is None`）；`AgentRuntime.uow/kernel/driver/ports` 是可信调用方的 escape hatch，不在本条承诺内（执行期收窄，C12）；(d) 同 owner 重启后 `open` 仍成功且 `config_hash` 一致 | 必须 | 必须 | BA05 | `tests/agents/test_agent_open_scope.py::{test_open_across_owner_scope_is_indistinguishable_from_not_found,test_cross_owner_error_leaks_no_content,test_cross_owner_cannot_read_results,test_same_owner_open_still_works_after_restart}` |
| AC9 | 普通 Turn 失败不清空 session 或 lifetime 费用 | 第 1 轮成功 → 第 2 轮失败（取消或 per-turn 超限）→ (a) `history()` 仍含第 1 轮结果；(b) Context 未回退、第 1 轮消息仍在；(c) 费用账本 `committed_micros` 未减少；(d) 第 3 轮的 provider 请求仍含第 1 轮对话；(e) `base_agent_turns_v1` 三行俱在、seq 1/2/3 | 次要 | 可选 | BA10 | `tests/agents/test_cancel_turn.py::test_turn_failure_keeps_session_and_cost` |
| AC10 | per-turn 限制真正执法（S1 遗留 L3 的 **per-turn 一半**；per-agent lifetime 精确执法移交 S5，C10） | (a) `max_model_calls_per_turn=2` 时第 3 次模型调用 → 该 Turn `state==failed`、`error_code=="react_max_turns_exceeded"`，Run 仍 `WAITING`，下一条输入正常提交；(b) `max_tool_calls_per_turn` 同理；(c) 失败后 checkpoint 的 lifetime totals **未被清零**，下一轮 `provider_turn_ordinal_from` 严格大于上一轮 `provider_turn_ordinal_to`；(d) `turn_deadline_seconds` 以 `turn.created_at`（首次持久准入）折算，**恢复不延长**；(e) `driver.policy_fingerprint` 不随 per-turn 限额变化；(f) 两个限额不同的 Agent 并发跑互不干扰 | 次要 | 可选 | —（BA-v1.0 §4.1/§10） | `tests/agents/test_turn_limits.py`（7 例） |
| AC11 | `cancel_turn` 最小合同 | (a) 取消进行中的 Turn → 结果 `state==failed` + `error_code=="agent_turn_cancelled"`，Run 仍 `WAITING`，continuation `ACKED`，下一条输入正常；(b) 同 `command_id` 幂等；(c) 已 `result_pending`/`committed` 的 Turn **不被改写**（返回原始事实，回执标 `already_settled`）；(d) 已落账的 effect 行数与内容不变，取消结果**不宣称**回滚现实动作；(e) `control_generation` 递增 | 次要 | 可选 | —（BA-v1.0 §4.2/§8.4） | `tests/agents/test_cancel_turn.py`（5 例） |
| AC12 | D7 裁决落地：checkpoint 不分键 + `active_turn_id` | 跑两轮后该 run 只有 **1** 条 `react.termination.v1` checkpoint 记录；`active_turn_id` 等于最后一轮 `turn_id`；两轮的 `{run_id}:provider-turn:N` 的 N 严格递增、无重复；`ReactCheckpointPort` 签名未变（`public-api.json` 不动） | 次要 | 可选 | —（BA-v1.0 §9.3 / BA34 前置） | `tests/agents/test_turn_resume_identity.py::test_checkpoint_stays_single_key_and_records_active_turn` |
| AC13 | F-BA-1：推理型响应 / 空正文不杀 Agent，可配置持久化能力 | (a) 空正文且无 tool_calls 的最终响应 → Turn `state==failed`、`error_code=="provider_empty_response"`、Run 仍 WAITING、下一条输入正常；(b) 含 reasoning 块且无 opaque ref → Turn 失败、Agent 存活；同一响应在 `AgentRuntimePorts.continuation_capability=OPAQUE_REFERENCE`+`opaque_continuation_ref` 下成功，且持久 Context 无 reasoning 原文；(c) 能力透传到 driver 的 `provider_budget_fingerprint` | 次要 | 可选 | S1 L5 / F-BA-1 | `tests/agents/test_provider_response_durability.py`（3 例） |

---

## 非功能 / 边界

| 类别 | 要求 | 验证方式 |
|---|---|---|
| 回归门 | 固定命令（含 3 个 `--ignore`）的 FAILED/ERROR 集合 **⊆** `../baseline-known-failures.txt`；新红一条即阻断 | `diff` 红集（每个任务收尾各一次） |
| **`react_loop.py` / `termination.py` 字节不变** | per-turn 限制走派生 `TerminationLimits`；取消走既有 `ReActLoop.run(tool_cancel=…)` 入口 | 两个文件的 `sha256` 冻结断言（照抄 S1 的 `test_start_snapshot_module_untouched` 手法） |
| **`start_snapshot.py` / `drivers/start_mode.py` 字节不变** | S1 的冻结闸继续生效 | `tests/agents/test_api_mode_fence.py`（不改，仍绿） |
| **不走内核 cancel** | 全片不调用 `RunClient.cancel` / `request_run_cancel` / `_terminalize_cancelled`；`close` / `cancel_turn` 之后 Run 必须仍 `WAITING` | `grep` + AC1(b) / AC11(a) 的 run_events 断言 |
| **`_commit_agent_turn` / `_drive` 的 BaseAgent 分支不改** | 取消结果由 driver 回交**已 stage 的那一份**，命中 `kernel.py:3320-3322` 的 hash 相等分支 | 代码评审 + `tests/agents/test_turn_finalize.py` 与 `test_base_agent_kernel_spike.py` 一行不改全绿 |
| schema 唯一性 | 全阶段只有一个 v10；新表/新列**追加进同一份 DDL**；`legacy_v7/v8/v9_descriptor().checksum` 一位不变；`migrate_execution_to_v9` 仍升到 9；已有 v9 库仍能打开 | `tests/execution/test_base_agent_schema_v10.py::{test_descriptor_checksums_are_stable,test_v9_database_still_opens,test_v10_descriptor_changed_and_is_the_only_ten}`（前两条不改） |
| **已存在 v10 库将拒绝打开** | 描述符重算的必然后果（`database.py:235`）；v10 未发布、无 Host 使用；**不提供升级路径** | 写进 journal 遗留与本文「明确不包含」；不写假装通过的测试 |
| 分层纪律 | `runtime/kernel.py` 与 `runtime/agent_turn.py` 仍**不得** import `simple_harness.agents`；close 闸门在 execution 层；per-turn 限额随 start snapshot 下发，driver 不查 agents 配置表 | `tests/agents/test_config_contracts.py` 的 ast 扫描（不改，仍绿） |
| 公共 API 面 | S2 的新符号**只**从 `simple_harness.agents` 导出；根包 `__all__`、`public-api.json`、`__version__` **不动** | `tests/unit/contracts/test_public_api.py` 不因本片变红 |
| 事务纪律 | 配额判定 / close 闸门 / 批次预留 / 取消 stage 均与写入**同一事务**；base_agent helper 接受外层 connection、不自开事务、不 await 网络；facade ≤ 25 行 | `tests/agents/test_uow_facade_shape.py` + 代码评审 |
| import 纯度 | 根包 import 不开库、不建连接、不起线程；`__all__[0] == "__version__"` | `tests/artifact/test_import_purity.py` 不由本片变红 |
| 协议版本 | `PROTOCOL_VERSION` 保持 `"1.0.0"` | `tests/conformance/test_protocol_version.py` |
| 不依赖用户记忆 SDK | `grep -rn "simple_harness_memory" src/simple_harness/agents/` 无匹配；`sys.modules` 无该模块 | 既有 `test_build_agent_runtime.py::test_no_memory_entrypoint_is_called` |
| **跨进程取消的边界** | `cancel_turn` 的 token 只在**持有该 Run 的进程内**立即生效；对端在下一次提交尝试时收敛到已 stage 的取消结果。本片保证**最终一致**，不保证"立刻停止在途动作" | 文档声明 + journal；**不写假装通过的测试** |
| **并发公平的边界** | 本片**不承诺**跨 Agent 的公平调度、Provider/工具并发槽位、全局待处理上限（S5 的 BA35）；只承诺**单 Agent 队列深度配额**与单 Agent 串行 | 文档声明 |
| **首轮 turn deadline 略短** | checkpoint 在第一轮才诞生，`max(0, turn.created_at - started_at)` 首轮为 0，首轮实际期限 = `turn_deadline_seconds`（不含创建到首驱动的间隔） | journal 记账；测试用假时钟避免误判 |
| `RunClient.cancel` 围栏缺口 | `_cancel_run`（`kernel.py:3223-3262`）不查 `api_mode`，能把 BaseAgent Run 打成 `CANCELLED`。本片**只暴露不修复** | 一条标注遗留的测试 + journal（归 S5） |
| 性能边界 | 本片**不承诺** Context 增长复杂度（仍是 `runtime/context.py:170-174` 的全量重写）；`create_many(100)` 在本机 < 20 s（宽松计时断言） | AC7 的计时断言 |
| 密钥 | 不引入任何真实端点调用；无 key 出现在源码/测试/计划/日志 | 人工 grep |

---

## LLM 行为变异清单

> S2 不新增任何**模型可见**的工具（`close` / `cancel_turn` / `create_many` 都只对调用方开放），但 per-turn 限制与取消会改变模型在一轮内的可达行为，故以下每条都必须有**端侧容错断言**。

| # | 变异 | 端侧容错要求 | 断言（测试） |
|---|---|---|---|
| **V1 一轮内狂调工具** | 模型在同一 AgentTurn 里连续要求工具调用，冲破 `max_tool_calls_per_turn` / `max_model_calls_per_turn` | 端侧**不截断、不伪造**工具结果：`TerminationBudgetExceeded` → 该 Turn 记为**失败结果**（`error_code` 可见），Agent 回到可接新输入的状态；lifetime totals 不被重置；已发生的工具 effect 全部保留在账本里 | `tests/agents/test_turn_limits.py::{test_per_turn_model_call_limit_fails_the_turn_not_the_agent,test_per_turn_tool_call_limit_is_enforced,test_per_turn_limits_do_not_reset_lifetime_totals}` |
| **V2 取消时模型仍在产出** | `cancel_turn` 到达时模型正在生成或刚返回一批 tool_calls | 已 stage 的取消结果是唯一被提交的事实；driver **只回交那一份**（hash 相同），不把模型的半成品写成结果；已落账的 effect 不回滚、不宣称回滚 | `tests/agents/test_cancel_turn.py::{test_cancel_turn_ends_the_turn_without_killing_the_agent,test_cancel_does_not_claim_to_undo_real_effects}` |
| **V3 取消晚于结果冻结** | 结果已 `result_pending` 或已 committed 后才收到取消 | **事实优先**：返回原始成功结果，取消命令仍留幂等回执（`already_settled`），不改写任何已冻结的事实 | `tests/agents/test_cancel_turn.py::test_cancel_after_result_pending_does_not_rewrite_the_fact` |
| **V4 close 之后的在途轮仍在跑** | `close` 时有开放 Turn，模型还在跑 | close 进入 `closing` 并 drain；drain 超时返回**可查询回执**（不强杀、不无限阻塞）；该 Turn 随后正常 committed；期间新输入一律被拒 | `tests/agents/test_agent_close.py::test_close_drains_the_open_turn_and_returns_a_queryable_receipt` |
| **V5 队列被灌满** | 调用方（或上层编排）连续投超过 `max_pending_inputs` 条输入 | 超额投递是**结构化拒绝**（`AgentPendingInputsExhausted`），零行写入；已受理的输入与幂等重放不受影响；队列消化后可继续投 | `tests/agents/test_input_queue.py::{test_pending_inputs_quota_rejects_the_third_input,test_quota_does_not_block_idempotent_replay}` |

补充纪律（全部变异共用）：任何变异都不得导致——Agent 进入终态、已发生 effect 被改写或重复计费、已冻结结果被覆盖、continuation 悬挂未 ack、或 `base_agent_*` 表出现孤儿行。

---

## Assurance 摘要（Profile: standard）

**受保护资产**

1. Slice 1 已交付的"永不终态的执行身份"与委派价值链（不得被任何控制面操作打成终态）。
2. Provider / Effect 账本的真实动作事实与费用记录（不可篡改、不可重复、取消不回滚）。
3. 已冻结的 AgentTurn 结果（`result_pending` 与 `committed` 都是事实）。
4. 已有 v9 execution 数据库中的历史行与 v7/v8/v9 描述符 checksum。
5. Agent 之间的 owner 隔离边界。
6. 公共 API 面（根包 `__all__` / `public-api.json` / `ReactCheckpointPort` 签名）。
7. `react_loop.py` / `termination.py` / `start_snapshot.py` / `start_mode.py` 的字节级稳定。

**可信假设**

1. `4613e62` 的 `tests/agents` + schema 测试（83 passed / 1 skipped，实测 4.33 s）反映真实约束。
2. SQLite 的事务原子性与 UNIQUE 约束可信；`Database.transaction()` 是唯一写入口。
3. continuation 的 FIFO 认领与 `live_index` 的单 task 去重已由 S1 的 spike 用例覆盖（基线绿）。
4. `ReActLoop` 无跨调用的进程内状态，全部状态在 checkpoint 里（因此可按轮新建 loop）。
5. `ReActLoop.run(tool_cancel=…)` 的取消检查点（`react_loop.py:343`、`:733`、`:1056-1058`）语义稳定。
6. 调用方（应用代码）可信；模型输出一律不可信。

**范围内失败**

1. close 的 drain 超时 → 返回 `state="closing"` 的可查询回执，Agent 仍拒收新输入，在途 Turn 正常收敛（不强杀、不无限阻塞）。
2. 批次在 `reserved` 之后崩溃 → 同 `batch_key` 重试续做，IDs 与预留一致，不产生重复实例。
3. per-turn 限额超限 → **失败的 Turn**，Agent 回到 IDLE，lifetime 计数与 session 历史完好。
4. UNKNOWN / 授权等待 → 同 Turn 挂起并在阻塞解除后续跑，动作身份不变、费用不重复预留。
5. 在途 checkpoint 上被要求跑另一个 Turn → `DriverResult(FAILED,"base_agent_turn_identity_conflict")`（内核完整性故障，允许 FAILED），provider 不被调用。
6. 队列满 → 结构化拒绝，零副作用。
7. 跨进程 `cancel_turn` → 最终一致（对端下一次提交尝试时收敛），显式声明不保证即时停止。

**最大可接受影响**

- 单个 AgentTurn 失败或被取消并被持久记录；Agent 回到可接收新输入的状态。
- 一个 Agent 停在 `closing` 且带可查询回执，直到在途 Turn 收敛。
- 一个批次停在 `reserved`，需一次同 `batch_key` 的重试才收敛。
- **不可接受**：BaseAgent 因 close/cancel 进入终态；已冻结结果被覆盖或重复生成；已发生 effect 被改写或重复计费；continuation 永久悬挂导致 Agent 卡死；跨 owner 泄露内容或存在性；v7/v8/v9 描述符 checksum 变化；已有 v9 库无法打开；`react_loop.py` / `termination.py` / `start_snapshot.py` 被修改；根包公共 API 变化。

---

## 完成的定义

1. AC1–AC8（8 条 MUST）全部有对应测试并通过；AC9–AC13 各至少一条测试通过（AC13 于执行期并入，C11）。
2. **价值验证里程碑达成（T3.5）**：`.venv/bin/python -m pytest -q tests/agents/test_agent_lifecycle.py` 全绿——一条用例同时证明"并发两条输入严格按 seq 顺序处理"与"close 后拒收、Run 不终态、重启后仍拒收"。
3. 全片套件：`.venv/bin/python -m pytest -q tests/agents tests/execution/test_base_agent_schema_v10.py` 全绿（S1 的 83 例**一行不改**仍绿，唯一例外是 `BASE_AGENT_TABLES` 常量扩表）。
4. 固定回归命令的红集 **⊆** `../baseline-known-failures.txt`；任何差异逐条在 journal 说明并落在 T1（schema 牵动的迁移/schema 夹具）声明的范围内。
5. `.venv/bin/mypy` 保持 **0 issues**。
6. 非功能表每一行都有验证记录，尤其：`react_loop.py` / `termination.py` / `start_snapshot.py` / `start_mode.py` 的 sha256 不变、分层纪律、公共 API 面不变、v9 库仍可打开、`migrate_execution_to_v9` 仍升到 9。
7. LLM 变异清单 V1–V5 各有端侧容错断言并通过。
8. L12 的下沉是**纯重构**：`tests/agents/test_turn_finalize.py` 与 `tests/agents/test_base_agent_kernel_spike.py` **未被修改**且全绿；两个 facade 的函数体 ≤ 40 行（执行期放宽：保留参数校验与 replay 分支在 facade 内）。
9. **D7 有明确裁决并记账**：`investigation.md` §9.2 + journal 写明"S2 不分键"及四条理由；替代物为**持久序号规则**（`active_turn_id` 不可用，见 `challenge-round-1.md` 文末），`test_checkpoint_stays_single_key_and_ordinals_never_repeat` 有断言。
10. 计划中标注为"不包含"的能力（尤其**跨 Agent 并发公平**、**残留冻结窗口**、**v9→v10 升级器**、**`RunClient.cancel` 围栏**），**没有**任何一条被声称已完成。
11. `plans/2026-09-10-base-agent-phase1/slice-2/` 下留有：`baseline-recheck.md`（T1 第 0 步）、中文 journal（含遗留清单与"触碰既有红"说明）；`testcase/base-agent-slice-2/README.md` 与 `testcase/index.md` 同步。
12. `src/simple_harness/agents/` 与 `src/simple_harness/runtime/agent_turn.py` 下无 `import simple_harness_memory`；根包 `__all__` 与 `public-api.json` 逐字未变。
