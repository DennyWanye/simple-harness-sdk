plan-status: finalized-v2 (第 1 轮挑战 21 条已裁决落实，2026-09-10)

# 验收标准（修订版 v2）：Slice 1 · 委派最短价值链

## 矛盾分析

**主要矛盾**

> 要让主 Agent 用一个受配额的委派工具造出子 Agent、并把子 Agent 的结果可靠取回用于回答，就必须让**父子两个执行身份都永不终态**（否则"答完再问"不成立）；可 SDK 里唯一的父子结果通道 `child_terminal_receipts` 恰恰**只在子 Run 终态时才产生**（`kernel.py:3195-3241`），于是"不死"与"能取回结果"在现有内核里互相排斥。

**矛盾的主要方面**

是**"单轮终态约束"与"父子结果回传依赖终态"这同一件事的两面**，不是委派本身。
初稿把矛盾写成"只缺一个不死的执行身份、委派底座已就绪"，后半句被代码证伪：一旦执行身份不死，既有的结果回传通道同时失效。因此本片必须**同时**交付两样东西——

1. 一个能反复交结果而不进终态的执行身份（`agent_turn_outcome` 载体 + `_drive` 分流 + stage/finalize，T5/T6/T6.5）；
2. 一条**不依赖终态**的父子结果通道（直接读子 Agent 的 `base_agent_turn_results_v1` 行 + 条件等待带超时，T8）。

少任何一样，价值链都断。资源优先投在 T5/T6/T6.5（第一面）与 T8（第二面），二者**同为核心任务**。

**产生原因**

1. `src/simple_harness/runtime/drivers/react.py:292`：ReAct 的最终响应被硬编码映射为 `RunState.COMPLETED`。
2. `src/simple_harness/runtime/kernel.py:218-222`：`DriverResult` 规定只有 COMPLETED 才能携带 `conversation_output`，"返回结果"与"进终态"被绑成一件事。
3. `src/simple_harness/runtime/kernel.py:1267`：`signal_conversation` 对终态 Run 抛 `terminal Run rejects new continuations`。
4. `src/simple_harness/runtime/kernel.py:3195-3241`：子 Run 的结果回执（`child_terminal_receipts` / `child_signals`）**只在 `_terminalize` 里写**，子 Run 不终态就永远没有回执。
5. 现有多轮 conformance 之所以看起来能跑，是因为 fixture 把 provider 堵死让 Run 停在非终态（`tests/conformance/test_future_consumer_memory.py:355` 的 `block_provider=True`），并非真的支持"答完再问"。

**解决方向**

- 新增一个与 `conversation_output` **互斥**的 `agent_turn_outcome` 载体（类型定义在 `runtime/agent_turn.py`，纯 JSON，内核不 import `agents`），让 driver 能在 `RunState.WAITING` 下交出本轮结果；`_drive` 在 WAITING 分支之前分流到 stage(`result_pending`) → finalize，**finalize 同事务 ack 本轮 continuation 并按既有范式重新调度**，Run 保持非终态。
- 父子结果通道**不用**任何终态设施：子 Agent 也是永不终态的 BaseAgent，`agent.delegate` 直接条件等待子 Agent 的 `base_agent_turn_results_v1` 行（带超时，超时返回模型可见的失败结果、不抛、不重发）。
- 委派**不新造父子协议**：仍复用现成的 `ChildCoordinator.launch`（`runtime/child_coordinator.py:17-41`）+ `claim_profile_launch_and_commit_child`（`execution/sqlite/uow.py:4585`）建子执行身份，只是不再消费它的终态回执；`AttachmentPolicy.DETACHED` 降级为纵深防御。
- **不新增 start mode**：用 `driver_kind="base_agent"`，`kernel.py:2735` 取到的就是真实 driver，指纹预检（`:2736-2738`）语义自然成立；`start_snapshot.py` 与 `StartModeDriverRouter` 零改动，Agent 绑定放 `start.input`。
- 单层不递归靠**结构**保证：子 Agent 的 `capability_snapshot.tools` 不含 `agent.delegate`（`react.py:377-401` 决定每 Run 的工具集）。

**最小验证动作**

价值验证里程碑（内核级，第 5 个任务后即可跑）：
```
.venv/bin/python -m pytest -q tests/agents/test_base_agent_kernel_spike.py
```
完整价值链：
```
.venv/bin/python -m pytest -q tests/agents/test_delegation_e2e_mock.py
```
跑通"主 Agent 收复杂任务 → 委派 → 子 Agent 出结论（带只有子 Agent 知道的验证码）→ 结果回到主 Agent 并出现在主 Agent 的最终回答里 → 重启后旧结果可读且不重生成 → 主 Agent 再接第二条输入"。

---

## 范围

### 包含

1. schema v10 的 4 张 BaseAgent 表（bindings / turns / turn_results / delegations，delegations 为最小映射），新库直建；同步修正 `short_context_migration` 的 v9 目标描述符。
2. `src/simple_harness/runtime/agent_turn.py`（`AgentTurnOutcome`，纯 dataclass）+ `src/simple_harness/agents/` 包：`config` / `contracts` / `codec` / `execution` / `completion` / `base` / `runtime` / `ports` / `tools/delegate`。
3. `namespace="base-agent/v1"` 的 API 隔离 + 两个 kernel 公开入口 `Runtime.start_base_agent_run` / `Runtime.signal_base_agent_input`。**不新增 start mode、不改 `start_snapshot.py`、不改 `StartModeDriverRouter`。**
4. `DriverResult.agent_turn_outcome` 互斥载体 + `_drive` 分流 + stage/finalize（**同事务 ack continuation + 提交后重新调度**）+ `recover` 的 `result_pending` 优先 finalize（不重进 driver）。
5. `AgentExecutionDriver`（复用 `ReActLoop`，最终响应不关 Agent；无输入不调 provider；意外 continuation kind 返回 FAILED 结果不抛）。
6. `build_agent_runtime(config, ports)`，显式无 Memory。
7. `agent.delegate` 工具：单层、每 Turn 配额、`delegation_id` 幂等、`NON_PROJECT_EFFECT`、**结果与 UNKNOWN 结算都直读子 Agent 的 `base_agent_turn_results_v1` 行**、条件等待带超时、子 Run 与委派登记同事务围栏。
8. `BaseAgent.submit / wait_turn / ask / get_result / status`；`AgentRuntime.create / create_many(最小) / open / shutdown`。
9. mock 确定性端到端（4 次模型调用）+ 真实 provider 端到端（`--run-real-provider` 开关，默认 skip）。
10. 公共导出的合法快照更新（`public-api.json` + `tool-public-api.json` + successor 只增不减断言 + 四处版本硬编码修正）。

### 明确不包含

- `cancel_turn` / `close` / `memory.search` / `memory.read`（S2/S4）。
- 完整批量语义：`batch_key` 幂等回执、`batch_identity_conflict`、整批回滚（S2 的 BA02–BA04）。
- `open` 的 owner_scope 越权校验（S2 的 BA05）。
- 有界 Context 装配、真实 tokenizer 计数、ProtocolGroup 轮换、增量 Journal 替换全量快照（S3 的 BA13–BA19/BA22）。
- 词面/向量混合召回、Agent 间隔离检索、索引世代与降级（S4 的 BA20/BA23–BA29）。
- 并发公平限流、队列配额、故障注入矩阵（S5 的 BA31–BA36/BA39）；**含 react_loop 最终 CAS 与 stage 之间的残留冻结窗口**（见非功能表）。
- **已有 v9 库的就地升级器与迁移回执**（S5 的 BA37）；本片只保证已有 v9 库仍能打开。
- exact-wheel conformance 与发布说明（S5 的 BA40）。
- 修复 `baseline-known-failures.txt` 里的既有红（唯一例外：T10 会顺手修 `test_public_api_matches_frozen_snapshot` 及其三条同因红，须在 journal 显式记录）。
- 任何对 `simple_harness_memory` 的依赖。
- 向模型暴露 `create_many`（BA-v1.0 §15 明令禁止）。
- 多层委派（子 Agent 再委派）：由子的 `capability_snapshot.tools` 结构性禁止，不做运行时特判。

---

## 功能验收条款

> MUST（必须）8 条，决定性条款排前。

| ID | 功能点 | 验收条件（可验证） | 矛盾地位 | 优先级 | 对应 BA/DG | 最小决定性测试 |
|---|---|---|---|---|---|---|
| **AC1** | 委派创建子 BaseAgent | 主 Agent 调用 `agent.delegate` 后：`base_agent_bindings_v1` 新增一行 `role='child'` 的 `child_agent_id`；对应子 `run_id` 的 `parent_run_id == 父 run_id`；`base_agent_delegations_v1` 恰好 1 行且 `state='settled'`；全过程只发生一次 `claim_profile_launch_and_commit_child`；`child_terminal_receipts` 表 0 行 | 决定性 | 必须 | DG01 | `tests/agents/test_delegate_tool.py::test_delegate_creates_child_agent_and_returns_child_result_row` |
| **AC2** | 子 Agent 结果回到主 Agent（**nonce 口径**） | 测试随机生成 `NONCE` 并**只**写进子 Agent 的 `instructions`（来自委派配置模板，主 Agent 不可见、不可改）；前置断言 `NONCE` 不出现在主 Agent 的 `instructions`、主 Agent 收到的任何输入、主 Agent 的工具描述/schema 中；然后断言 (a) 工具返回值 `value["result_hash"]` == 子 Agent `base_agent_turn_results_v1` 行的 `result_hash`，(b) 主 Agent 本轮 Context 存在对应 tool 消息，(c) **主 Agent 最终 `AgentTurnResult.public_output` 含该 `NONCE`**。mock 与 real_provider 共用同一口径 | 决定性 | 必须 | DG02 | `tests/agents/test_delegation_e2e_mock.py::test_main_agent_answer_carries_child_only_nonce` |
| **AC3** | 单层 · 不递归 · 有配额 · 幂等 | (a) 子 start snapshot 的 `input["capability_snapshot"]["tools"]` 不含 `"agent.delegate"`；(b) `max_delegations_per_turn=1` 时第二次调用返回 `ToolOutcome.REJECTED` + `error_code="agent_delegation_quota_exceeded"` 且不新增行；(c) 同 `delegation_id` 二次调用返回同一 `child_agent_id`、不新增行、不第二次 launch；(d) **一次委派恰好对应子 Agent 的一个 AgentTurn**（`base_agent_turns_v1 WHERE agent_id=child` 只有 `seq==1`） | 决定性 | 必须 | DG03 | `tests/agents/test_delegate_tool.py::{test_child_catalog_excludes_delegate,test_quota_rejects_second_delegation_in_same_turn,test_same_delegation_id_is_idempotent,test_one_delegation_is_exactly_one_child_turn}` |
| **AC4** | **主 Agent** 同身份多轮 | 同一个**主** `agent_id` 连续两次 `ask` 均返回 `AgentTurnResult`；`base_agent_turns_v1` 中该 agent 的 `seq` 为 1、2；对 run_events 全量回放，主 `run_id` 全程**从未**进入 `COMPLETED/FAILED/CANCELLED`；第二轮的 provider 请求 messages 含第一轮 assistant 回答；每轮的输入 continuation 均为 `ACKED`。**子 Agent 不要求两轮**（一次委派 = 子一个 Turn） | 决定性 | 必须 | BA07 | `tests/agents/test_base_agent_kernel_spike.py::test_two_results_on_one_run_never_terminal` |
| **AC5** | **stage 之后**任意点崩溃不重生成 | 失败域 = **结果已 stage（Turn `phase='result_pending'`、`staged_result_hash` 已落库）之后的任意点**。在该域内中断进程；新 Runtime `recover()` 后：provider 调用计数未增加；driver `start` **未被再次调用**；`base_agent_turn_results_v1` 恰好 1 行且 `result_hash` 与 stage 时相同；`get_result` 返回同一结果；Run 版本单调递增。**stage 之前（react_loop 最终 CAS 与 stage 之间）的残留窗口不在本片保证内**，见非功能表 | 决定性 | 必须 | BA30 | `tests/agents/test_turn_finalize.py::test_result_pending_then_kill_then_recover_commits_once` |
| **AC6** | 实例互相独立 | `create_many([cfg]*3, batch_key="b1")` 产出 3 个不同 `agent_id` / 3 个不同 `run_id`；对 agent[0] 提交输入后，agent[1]、agent[2] 的 `SqliteContextPort.load(...).revision == 0`；创建阶段 provider 调用计数 == 0 | 次要 | 必须 | BA01 | `tests/agents/test_build_agent_runtime.py::test_create_returns_independent_agents` |
| **AC7** | 新旧 API 互相拒绝（覆盖 root 与 child） | (a) **旧 API 拒新身份**：对已围栏为 `base-agent/v1` 的 run（**主 Agent 的 root run 与委派产生的 child run 各测一次**），`RunClient.start_conversation` 与 `signal_conversation` 均抛 `CommandError(RUN_MODE_CONFLICT)`；(b) **新入口拒旧身份**：`Runtime.signal_base_agent_input` 对 legacy 与 unmanaged run 均抛 `CommandError(RUN_MODE_CONFLICT)`；(c) **未注册即拒绝，绝不落回 react**：`build_agent_runtime` 未注册 `base_agent` driver 时 `build_runtime` 抛 `ValueError`（root profile 的 `driver_kind` 未注册，`kernel.py:3490-3493`），不返回任何落回 react 的 Runtime；(d) 普通 ordinary Run 行为不变、v7 快照字节不变、`start_snapshot.py` 与 `start_mode.py` 文件 sha256 不变 | 次要 | 必须 | BA06 | `tests/agents/test_api_mode_fence.py::{test_legacy_start_conversation_rejects_base_agent_run,test_signal_conversation_rejects_base_agent_run,test_signal_base_agent_input_rejects_legacy_run,test_signal_base_agent_input_rejects_unmanaged_run,test_build_runtime_rejects_unregistered_base_agent_driver,test_v7_snapshot_bytes_unchanged,test_start_snapshot_module_untouched}` + `tests/agents/test_delegate_tool.py::test_child_run_is_mode_fenced_before_it_exists` |
| **AC8** | 不依赖用户记忆 SDK | 完整链路跑完后：memory spy 的 `recall_for_turn/release_recall/record_committed_turn` 零调用；`runtime._ports.agent_memory is None`、`conversation_memory_enabled is False`、`memory_dispatcher is None`、`context_staging is None`；`sys.modules` 无 `simple_harness_memory`；`grep -r "simple_harness_memory" src/simple_harness/agents/ src/simple_harness/runtime/agent_turn.py` 无匹配 | 次要 | 必须 | BA38 | `tests/agents/test_build_agent_runtime.py::test_no_memory_entrypoint_is_called` |
| AC9 | 委派 UNKNOWN 不盲重试 | 委派 effect 停在 UNKNOWN 时，`ToolReconciliationPort.observe` **以子 Agent 的 `base_agent_turn_results_v1` 行为证据**（路径：`EffectRecord.arguments["delegation_id"]` → `base_agent_delegations_v1` → `child_agent_id` → `base_agent_bindings_v1.run_id` → 结果行）返回 `COMPLETED`（有结果行，`evidence_ref` = 该行 `commit_receipt_id`）或 `STILL_UNKNOWN`（无结果行）；**不发生第二次** `claim_profile_launch_and_commit_child`；实现中 `grep` 不到 `child_terminal` | 次要 | 可选 | DG04 | `tests/agents/test_delegate_tool.py::test_delegate_unknown_reconciles_from_child_result_row` |
| AC10 | 真实模型复现 | `--run-real-provider` 下端到端脚本跑通一次并留证（判定同 AC2 的 nonce 口径）；不带开关时该用例 skip；配置缺失时 skip 而非 fail；输出中不含 API key 任何前缀 | 次要 | 可选 | — | `tests/agents/test_delegation_e2e_real_provider.py` |

---

## 非功能 / 边界

| 类别 | 要求 | 验证方式 |
|---|---|---|
| 回归门 | 固定命令（含 3 个 `--ignore`）的 FAILED/ERROR 集合，除 T2/T10 显式声明的 schema/快照夹具外，与 `baseline-known-failures.txt` 一致；新红一条即阻断 | `diff` 红集 |
| **创建不驱动模型** | `AgentRuntime.create` / `create_many` 之后、在收到第一条输入之前，**provider 调用计数恒为 0**，`base_agent_turns_v1` 恒为 0 行；driver 在"无 `base_agent_input` continuation 且无 `result_pending` Turn"时立即返回 `DriverResult(WAITING)`，不进 `ReActLoop`、不 load Context | `test_agent_driver.py::test_create_without_input_returns_waiting_without_provider`、`test_build_agent_runtime.py::test_create_does_not_call_provider` |
| **提交后重新调度** | `BaseAgent.submit` 返回后**无需任何外部驱动**，该 Turn 即被消费并提交；每轮 finalize 在**同一事务**内 ack 本轮 continuation（receipt_id = `{run_id}:progress:{continuation_id}:{claim_epoch}`），提交后按既有范式 `_reschedule` | `test_build_agent_runtime.py::test_submit_reschedules_the_run`、`test_turn_finalize.py::test_finalize_acks_the_input_continuation_in_the_same_transaction`、`test_base_agent_kernel_spike.py::test_input_continuation_is_acked_and_rescheduled` |
| **残留冻结窗口（本片不保证）** | `react_loop.py:717-732` 的最终 CAS（phase 复位）与本片 stage 事务之间存在一个**未被同一次 CAS 覆盖的残留窗口**：在该窗口内崩溃，重启后会重新调用模型。本片**如实声明不保证**，AC5 的失败域从 stage 之后起算；处置方案（把 `RESULT_PENDING` 并进 react_loop 的同一次 CAS）**归 S5 的 BA31** | 记入 `journal.md` §6 遗留；不写"假装通过"的测试 |
| 旧路径零改动 | `conversation_output` 只能 COMPLETED（`kernel.py:218-222`）；`_drive` preflight 四道闸顺序不变；**`start_snapshot.py` 与 `drivers/start_mode.py` 文件字节不变**；v6/v7 start snapshot 字节不变；`react.termination.v1` 初始锚不被覆盖；`TerminationState` totals 永不重置；单一 transaction owner；已发生 effect 结果不可改写；`react_loop.py` 不改 | 既有测试全绿 + `test_v7_snapshot_bytes_unchanged` + `test_start_snapshot_module_untouched` |
| 分层纪律 | `runtime/agent_turn.py` 与 `runtime/kernel.py` **不得** import `simple_harness.agents`；`agents` 层单向依赖 `runtime`；`AgentTurnOutcome` 只搬纯 JSON | `test_config_contracts.py::test_agent_turn_outcome_has_no_agents_dependency`（`ast` 扫描 + `sys.modules` 断言） |
| import 纯度 | 根包 `import simple_harness` 不开数据库、不建网络连接、不起线程；新增导出走 `__getattr__` 惰性解析；`__all__[0] == "__version__"` | `tests/artifact/test_import_purity.py` 不由本片变红 |
| 协议版本 | `PROTOCOL_VERSION` 保持 `"1.0.0"` | `tests/conformance/test_protocol_version.py` |
| schema 唯一性 | 全阶段只分配一个 v10；`accepted_descriptor_rows()` 同时接受纯 v9 组合与 v10 组合；历史 checksum 不变；`migrate_execution_to_v9` 仍升到 9 | `tests/execution/test_base_agent_schema_v10.py::test_descriptor_checksums_are_stable`、`tests/execution/test_short_context_migration_still_targets_v9.py` |
| 事务纪律 | 事务内不 `await` LLM / embedding / 网络；委派的条件等待在事务外做；base_agent helper 接受外层 connection、不自开事务；`uow.py` 每个新 facade ≤10 行 | 代码评审 + facade 行数检查 |
| 密钥 | API key 不出现在任何源码、测试、计划文档、日志、断言消息中；`--run-real-provider` 默认关闭 | `examples/base_agent_delegation.py` 自带 assert + 人工 grep |
| 性能边界 | 本片**不承诺** Context 增长复杂度（仍是 `context.py:170-174` 的全量重写）；mock 端到端总时长 < 10s | 计时断言（宽松） |
| 并发边界 | 本片**不承诺**多 Agent 并发公平；同一 Agent 的两条并发输入行为未定义（S2 的 BA08 才定义）；委派条件等待的超时是**上限**不是 SLA | 在文档中声明，不写测试假装通过 |
| queued Turn 跨重启恢复 | submit 后、首次 stage 前进程中断：新 Runtime 启动时 `AgentRuntime.recover_pending_turns()` 重新唤醒该 Run，`wait_turn` 最终拿到结果且 provider 只调用一次（closure E6） | `tests/agents/test_build_agent_runtime.py::test_queued_turn_survives_restart` |
| 委派续做而非毒化 | 预围栏后、launch 前中断，重试同 `delegation_id` 会续做 launch，子 Agent 恰好创建一次（closure E4） | `tests/agents/test_delegate_tool.py::test_reserved_delegation_is_resumed_not_poisoned` |
| 意外 continuation 不杀 Agent | 非 `base_agent_input` 的 continuation 被 ack，Run 保持 WAITING，无 Turn 行、无子终态回执（closure E3） | `tests/agents/test_agent_driver.py::test_unexpected_continuation_kind_keeps_run_waiting_and_acks` |

---

## Assurance 摘要（Profile: standard）

**受保护资产**

1. 执行内核的既有终态/租约/围栏语义与旧 Run 的行为（现网 Host 依赖）。
2. Provider / Effect 账本的真实动作事实与费用记录（不可篡改、不可重复）。
3. 已有 v9 execution 数据库中的历史行。
4. 用户的 API key（Host `.env`）。
5. 主 Agent 与子 Agent 之间的隔离边界（子不得越权递归繁殖）。
6. 旧的 start snapshot 编解码路径（v6/v7 字节级不可动）。

**可信假设**

1. `fd12e7dd` 的既有测试（除 `baseline-known-failures.txt` 的 75 条）反映真实约束。
2. SQLite 的事务原子性与 UNIQUE 约束可信；`Database.transaction()` 是唯一写入口。
3. `ChildCoordinator.launch` → `claim_profile_launch_and_commit_child` 的原子性已由 `tests/integration/execution/test_atomic_child_launch.py`、`test_ticket_generation.py` 覆盖（基线绿）。
4. `conversation_run_modes` 的围栏行**可以先于 `runs` 行写入**（`command_ingress.py:151-176` 不要求 run 已存在；`RunClient.start_conversation` 本身就是这个顺序）。
5. 调用方（应用代码）是可信的；模型输出一律不可信。

**范围内失败**

1. finalize 事务在任意点崩溃 → 恢复时 `result_pending` 优先提交，不重生成、不重进 driver（AC5）。
2. 委派 effect 停在 UNKNOWN → 凭**子 Agent 结果行**结算，不重复 launch（AC9）。
3. 子 Agent 迟迟不出结果 → 条件等待超时，返回模型可见的 `agent_delegation_timeout`，父 Turn 正常提交，委派记录停在 `'launched'`，**不重发**。
4. 模型输出违反 delegate 的 schema / 超配额 / 重复 `delegation_id` → 结构化 REJECTED，主 Agent 可继续（AC3 + 下节变异清单）。
5. Driver 收到不该出现的 continuation kind → 返回 `DriverResult(FAILED, "base_agent_unexpected_continuation")`，成为可观察的 Turn 失败，**不被静默吞掉**。
6. v10 新库与既有 v9 库共存 → 两者都能打开（非功能表）。
7. Provider 不可用 / 超时 → 沿用既有 `ProviderInvocationUnknownError` + `wait_blocker` 路径，不由本片改写。

**最大可接受影响**

- 单个 AgentTurn 失败并被持久记录为失败；Agent 回到可接收新输入的状态。
- 一个子 Agent 被创建但结果不可用（`state` 停在 `'launched'`），主 Agent 收到明确的 `agent_delegation_timeout` 工具结果。
- **不可接受**：旧 Run 行为改变、旧 start snapshot 字节改变、Provider/Effect 账本被改写或重复计费、结果被重复生成、子 Agent 递归繁殖、子 Run 未被 `base-agent/v1` 围栏、API key 泄露、已有 v9 库无法打开。

---

## LLM 行为变异清单

> `agent.delegate` 由 LLM 输出驱动，以下每条都必须有**端侧容错断言**（不依赖模型自觉）。断言名已按 v2 设计校正。

| # | 变异 | 端侧容错要求 | 断言（测试） |
|---|---|---|---|
| **V1 乱序** | 模型在同一批 tool_calls 里先给出依赖子 Agent 结果的后续调用，再给 `agent.delegate`；或跨轮次乱序引用尚未产生的 `delegation_id` | 引用不存在的 `delegation_id` → `REJECTED` + `error_code="agent_delegation_unknown_id"`，**不创建任何子 Agent、不写委派行、不写围栏行**；批内顺序由 `react_loop.py:734` 的 `call_ordinal` 决定，端侧不重排也不预测 | `tests/agents/test_delegate_tool.py::test_unknown_delegation_id_is_rejected_not_created`（断言 `base_agent_delegations_v1`、`base_agent_bindings_v1`、`conversation_run_modes`、`runs` 行数均不变） |
| **V2 重复** | 模型在同一轮重复发出相同 `delegation_id`；或重试整批 | 幂等：同 `delegation_id` 返回原 `child_agent_id`，`base_agent_delegations_v1` 仍 1 行，**不发生第二次 launch**（断言 `claim_profile_launch_and_commit_child` 调用计数 == 1）；不同 `delegation_id` 但超配额 → `agent_delegation_quota_exceeded` | `tests/agents/test_delegate_tool.py::test_same_delegation_id_is_idempotent`、`::test_quota_rejects_second_delegation_in_same_turn` |
| **V3 schema 违约** | 缺 `objective`、多余字段、`role_hint` 取非枚举值、类型错误（数组/数字冒充字符串） | 闭合 schema（`additionalProperties:false`）在 `validate_arguments` 阶段拦下 → `MalformedToolArgumentsError` → ReAct 转成模型可见的失败观察；**绝不落库、绝不创建子 Agent、绝不写围栏行** | `tests/agents/test_delegate_tool.py::test_malformed_arguments_never_create_child`（断言 `base_agent_delegations_v1`、`conversation_run_modes`、`runs` 行数不变） |
| **V4 超长** | `objective` 超过 8000 字符；或子 Agent 返回超长文本 | 入参 `maxLength` 拦下 → `REJECTED` + `error_code="agent_delegation_objective_too_large"`（不静默截断、不 launch）；子结果超长时工具返回值只带摘要 + `result_ref`，**原文完整留在子 Agent 的 `base_agent_turn_results_v1` 行可回读**（截断的是引用，不是事实；`result_hash` 仍对原文） | `tests/agents/test_delegate_tool.py::test_oversize_objective_is_rejected_before_launch`、`::test_oversize_child_result_returns_ref_not_truncated_fact`（断言 `value["result_hash"]` 仍等于子结果行的 `result_hash`） |
| **V5 拒不调工具** | 模型收到复杂任务后直接自己编答案，从不调用 `agent.delegate`；或调用后忽略返回结果 | 端侧**不强迫**模型调工具（不注入伪造 tool 结果、不改写模型输出）；AgentTurn 正常提交为"未委派"的结果；`base_agent_delegations_v1` 保持 0 行；`AgentTurnResult.delegation_count == 0` 可供上层判断；Run 仍非终态、仍可接下一条输入 | `tests/agents/test_delegate_tool.py::test_no_delegation_still_commits_a_valid_turn` |

补充纪律（全部变异共用）：

- 工具的**拒绝**一律是 `ToolResult(outcome=REJECTED, error_code=...)`，让模型看得见并可改策略；**超时**是 `ToolResult(outcome=FAILED, error_code="agent_delegation_timeout")`；只有内核完整性问题才抛异常，且**内核完整性问题在 driver 层转成 `DriverResult(FAILED, ...)` 而不是裸抛**（裸抛会被 `_drive` 的 `except UnitOfWorkConflict` 静默吞掉）。
- 任何变异都不得导致：创建出未记账或未围栏的子 Run、写入 `base_agent_delegations_v1` 的孤儿行、改写已发生 effect 的结果、或让主 Agent 进入终态。

---

## 完成的定义

1. AC1–AC8（8 条 MUST）全部有对应测试并通过；AC9、AC10 至少各有一条测试（AC10 允许因缺开关/缺配置而 skip）。
2. **价值验证里程碑达成（T6.5）**：`.venv/bin/python -m pytest -q tests/agents/test_base_agent_kernel_spike.py` 全绿——证明"不死的执行身份 + continuation 被 ack + stage 后重启只提交一次"。
3. **完整价值链达成（T9）**：`.venv/bin/python -m pytest -q tests/agents/test_delegation_e2e_mock.py` 全绿，且 provider 总调用次数恰好 4。
4. 固定回归命令的红集与 `baseline-known-failures.txt` 的差异**逐条有解释**，且全部落在 T2（schema v10 牵动的迁移/schema 夹具）或 T10（快照更新）声明的范围内；无其他新红。
5. `.venv/bin/mypy` 保持 0 issues（基线状态）。
6. 非功能表的每一行都有验证记录（尤其：`start_snapshot.py` / `start_mode.py` 字节不变、v7 快照字节不变、import 纯度、分层纪律、PROTOCOL_VERSION 不变、v9 库仍可打开、`migrate_execution_to_v9` 仍升到 9）。
7. LLM 变异清单 V1–V5 各有一条端侧容错断言并通过。
8. 真实 provider 演示跑过一次并留证；**mock 报告与 real_provider 报告分开记录**，不互相冒充（BA-v1.0 §14 / BA40 口径）。
9. `plans/2026-09-10-base-agent-phase1/slice-1/` 下留有：`baseline-recheck.md`（T2 第 0 步）、执行 journal（含"触碰既有红"的说明与 §6 遗留清单）、两份分开的测试报告。
10. `src/simple_harness/agents/` 与 `src/simple_harness/runtime/agent_turn.py` 下 `grep -r "simple_harness_memory"` 无匹配；`src/simple_harness/agents/tools/delegate.py` 下 `grep -r "child_terminal"` 无匹配。
11. 计划中标注为"不包含"的能力（尤其**残留冻结窗口**、完整批量语义、`cancel_turn`/`close`），**没有**任何一条被声称已完成。
12. 21 条挑战裁决在 `plan.md` 附 B 的落点表里逐条可查，且每条的落点在代码/测试里确实存在（closure 复核）。
