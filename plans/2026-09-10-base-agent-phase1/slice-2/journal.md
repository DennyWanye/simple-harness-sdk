# Slice 2 执行日志：生命周期与批量创建（2026-09-10）

- 仓库：`simple-harness-sdk`，起点 `701d89c`（S1 已交付 + S2 plan v1），被测 HEAD 见 §10
- 方法：按用户要求**不再走 plan-test/plan-task 仪式**，保留实质——先写决定性测试、每任务一提交、全量回归红集 ⊆ 基线、独立 review、真实端点验证、中文记账
- 关联：`plan.md`（v1，执行期修订见 §1）、`acceptance.md`、`challenge-round-1.md`、`investigation.md`

## 1. 计划挑战与执行期修订

挑战结果与 17 条裁决全部在 `challenge-round-1.md`。对 plan v1 的实质修订：
1. T6/T7/T8 共同加上"确定性失败的 Turn 复位 run 级 checkpoint"（C1，P0）；
2. T10 删掉 `continuation_capability` 旋钮，改为 wire 层空正文可见失败（C2，P0）；
3. T8 改为**协作式**取消：调用方只写持久意图，driver 经正常 outcome 路径产出失败结果（C6/C7）；
4. T7 的锚点由 `active_turn_id` 改为持久序号规则（D7 补充裁决，见 challenge 文末）；
5. 内核为 base_agent Run 增加"空转即释放租约/fence/心跳"（C9）与"唤醒等待在途 drive"；
6. T1 追加 `tool_call_ordinal_from` 列与 `UNIQUE(owner_scope, creation_key)`（C4、T4 需要：不同 owner 可同 key）。

## 2. 任务执行记录（每任务一提交）

| 任务 | 提交 | 交付 | 决定性测试 |
|---|---|---|---|
| T1 schema | `fe9ade5` | bindings `lifecycle/lifecycle_updated_at`、`UNIQUE(owner_scope,creation_key)`（`cdd9886`）、turns `tool_call_ordinal_from`（`e016cac`）、新表 `base_agent_creation_batches_v1`/`base_agent_control_commands_v1`；v7/v8/v9 checksum 不变 | `tests/execution/test_base_agent_schema_v10.py`（+4 例） |
| T2 队列 | `fe9ade5`/`06fcd8f` | `open_turn` 同事务配额（`PendingInputsExhausted`）与关闭闸门（`AgentClosedError`），幂等重放先于两者；`AgentPendingInputsExhausted` 公开错误 | `tests/agents/test_input_queue.py`（7 例） |
| T3 close | `d9faafe`/`de9a35f` | `control.py`（生命周期单向、控制命令幂等）、uow `close_agent/mark_agent_closed`、`AgentRuntime.close_agent`（事务 1 → drain → 事务 2）、finalize 尾部 closing→closed 收敛、`AgentStatus.lifecycle_state/control_generation`、`CLOSING/CLOSED` 投影 | `tests/agents/test_agent_close.py`（8 例） |
| **T3.5 里程碑** | `d9faafe` | `tests/agents/test_agent_lifecycle.py` 一条用例：并发两输入 seq 1/2 且第二次请求含第一轮回答 → close 拒收、turns 仍 2 行、Run WAITING、run_events 无终态 → 重启后仍拒收、history 2 条、provider 2 次 | **绿** |
| T4 owner | `cdd9886` | `open/binding/create` 三入口 owner 闸门（同一 `AgentNotFound` 模板）；creation_key 唯一性改为 per-owner | `tests/agents/test_agent_open_scope.py`（5 例） |
| T5 批量 | `9f98364` | `batches.py`、uow 四个 facade、`AgentRuntimePorts.max_agents/max_batch_size`、`AgentBatchRejected(error_code,index)`/`AgentBatchIdentityConflict`、`create_many` 四段式（写前整批准入含既有 binding 冲突 → reserve → 逐个 create → commit；committed 直接按回执 open） | `tests/agents/test_create_many_batches.py`（7 例，含 100 个 IDLE、崩溃续做） |
| T6 限额 | `e016cac` | per-turn 限额随 start input 下发（driver 不查 agents 表）；基线取持久 `provider_turn_ordinal_from/tool_call_ordinal_from/created_at`；每轮新建 `ReActLoop`，policy_fingerprint 不变；admission 期 deadline 已过直接失败不调 provider；`_settle_failed_turn` | `tests/agents/test_turn_limits.py`（7 例） |
| T7 恢复身份 | `e016cac`/`392500a` | 序号身份闸门、`turn_snapshot`、`AgentTurnTimeout.receipt`、`AgentRuntimePorts.authorization` 接受 SDK 原生端口（prepare/bind_decision） | `tests/agents/test_turn_resume_identity.py`（6 例） |
| T8 cancel | `b650ae9` | `request_agent_turn_cancel`（持久意图 + generation+1，已结算 → `already_settled`）、driver 准入期读意图/loop 取消点 token、`AgentCancelReceipt(cancelled/already_settled/pending)` | `tests/agents/test_cancel_turn.py`（8 例，含 BA10 会话与费用不清空） |
| T9 下沉 | `388470d` | `turns.submit_input/finalize_turn`；两个 facade 收缩到 ≤ 40 行、各一次事务；6 个 fault 点名字与顺序不变 | `tests/agents/test_uow_facade_shape.py`；`test_turn_finalize.py`、`test_base_agent_kernel_spike.py` **未改**全绿 |
| T10 F-BA-1 | `649d411` | `ProviderEmptyResponseError`（`provider_empty_response`）于 wire 抛出；工具调用响应的空正文不受影响 | `tests/agents/test_provider_response_durability.py`（2 例） |

## 3. 触碰既有红 / 影响面

- 固定回归命令（同 S1，忽略 3 个 import `simple_harness_memory` 的模块）：**58 failed + 15 errors = 73 条红，逐条 ⊆ `../baseline-known-failures.txt`，新红 0 条**；1988 passed。
- 冻结文件逐一比对 `701d89c`：`react_loop.py`、`termination.py`、`start_snapshot.py`、`start_mode.py`、`0005_fresh.sql`、根包 `__init__.py`、`public-api.json` **均未改**。
- v9 描述符 checksum `d9cb3ed5…` 不变；v10 fresh checksum 重算为 `1554e94a…`（本片三次追加 DDL）。仓库无 `*.db` 夹具；**S1 时期建立的本地 v10 开发库需删除重建**（v10 未发布、无升级器，属既定口径）。
- 内核改动只作用于 `driver_kind == base_agent`：空转释放权限（两处）、`_wake_continuation` 等待在途 drive；legacy Run 路径未改，回归红集未变即为证据。
- S1 测试改动：`test_turn_finalize.py` 两处加 `await runtime._activate(...)`（新不变量：空转 Agent 不持租约）；`test_delegate_tool.py` 等其余 S1 用例一行未改。

## 4. 价值验证

### 4.1 里程碑 T3.5（停而不死 + 严格串行）
`.venv/bin/python -m pytest -q tests/agents/test_agent_lifecycle.py` → 1 passed。

### 4.2 第二里程碑（C13 采纳：中途放弃后下一轮干净）
`test_turn_limits.py::test_per_turn_model_call_limit_fails_the_turn_not_the_agent`（超限失败 → checkpoint ready → 下一轮新请求、未重放工具）与 `test_cancel_turn.py::test_turn_failure_keeps_session_and_cost`（失败轮后 history/Context/费用/seq 全在，第三轮请求仍含第一轮对话）均绿。

### 4.3 真实端点（DeepSeek `deepseek-v4-pro`，`--run-real-provider`，key 已脱敏）
本片 HEAD 上跑 4 次委派链：**4/4 passed**（13.8 s / 38.5 s / 17.6 s / 20.3 s），`main_error=None`，`finish_reasons=[tool_calls,stop,stop]`，`empty_contents=[True,False,False]`（首个 True 为 tool_calls 响应，属正常）。F-BA-1 累计 10 次未复现；若再出现空正文，现在会以 `provider_empty_response` 可见失败落在 Turn 结果里而不是 dispatch 深处的 `provider_response_not_durable`。

## 5. 兑现表（AC → 脚本，全部为可复跑 pytest）

| AC | 地位 | 脚本 | 结果 |
|---|---|---|---|
| AC1 close | 决定性 | `test_agent_close.py`（8 例）+ `test_agent_lifecycle.py` | PASS |
| AC2 顺序/队列 | 决定性 | `test_input_queue.py`（7 例） | PASS |
| AC3 超时不取消 | 决定性 | `test_turn_resume_identity.py::test_ask_timeout_carries_the_receipt_and_caller_cancel_does_not_cancel_the_turn` + S1 `test_ask_timeout_does_not_cancel_the_turn` | PASS |
| AC4 同 Turn 恢复 | 必须 | `test_turn_resume_identity.py`（UNKNOWN / 授权 / 身份拒绝 / 单键 / 阻塞快照） | PASS（(a) 的 `active_turn_id` 断言按 D7 补充裁决改为序号规则） |
| AC5 批量幂等 | 必须 | `test_create_many_batches.py::{same_ids, different_content_conflicts, reserved_batch_is_resumed, existing_binding_conflict}` | PASS |
| AC6 整批零副作用 | 必须 | `test_create_many_batches.py::{invalid_config_leaves_no_partial_batch, caps_before_any_write}` | PASS |
| AC7 100 IDLE 不发请求 | 必须 | `test_create_many_batches.py::test_hundred_idle_agents_make_no_provider_request` + `test_input_queue.py::test_idle_agent_holds_no_lease_fence_or_heartbeat` | PASS |
| AC8 owner 闸门 | 必须 | `test_agent_open_scope.py`（5 例） | PASS（(c) 收窄为公开句柄入口） |
| AC9 失败不清空 | 次要 | `test_cancel_turn.py::test_turn_failure_keeps_session_and_cost` | PASS |
| AC10 per-turn 执法 | 次要 | `test_turn_limits.py`（7 例） | PASS（收窄为 per-turn） |
| AC11 cancel_turn | 次要 | `test_cancel_turn.py`（8 例） | PASS |
| AC12 D7 记账 | 次要 | `test_turn_resume_identity.py::test_checkpoint_stays_single_key_and_ordinals_never_repeat` | PASS |
| AC13 空正文可见失败 | 次要 | `test_provider_response_durability.py`（2 例） | PASS |

套件：`.venv/bin/python -m pytest -q tests/agents tests/execution/test_base_agent_schema_v10.py tests/unit/contracts` → 190 passed, 1 skipped（真实端点 opt-in）。`.venv/bin/mypy` 0 issues。ruff 对本片新增/修改文件全绿（uow.py 内两行 >100 列为 `25221a43` 既有）。

## 6. 遗留清单

| # | 事项 | 归属 |
|---|---|---|
| L2-1 | per-agent **lifetime** 限额仍是 runtime 级（driver 单一 policy fingerprint）；`AgentLimits.lifetime_*` 只在 `termination_limits()` 里可读 | S5 |
| L2-2 | cancel_turn 不打断**在途** provider 调用（取消点在调用前与工具批次后）；最终答案先到则 `already_settled` | 已文档化，若需强打断归 S5 |
| L2-3 | close 一个仍被父 Agent 委派中的子 Agent，会让在途委派工具调用失败（`AgentClosedError` → 工具失败） | 已知行为，S5 与 BA37 一并看 |
| L2-4 | `AgentRuntime.uow/kernel/driver/ports` 是可信调用方的 escape hatch，不受 owner 闸门约束 | 信任模型即如此，AC8 已收窄 |
| L2-5 | S1 时期本地 v10 开发库需删除重建（v10 描述符重算，无升级器） | 口径已在 §3 |
| L2-6 | `ProviderInvocationCoordinator` 实例计数断言依赖 monkeypatch `__init__`，属白盒 | 可接受 |
| L2-7 | 委派子 Agent 计入父 owner 的 `max_agents`；跨 Agent 并发公平/轮转仍不在本片 | S5 BA35 |
| L2-8 | plan v1 里 T7 写的 `active_turn_id` 方案不可用（termination schema 7 绑定 Context-use 协议），替代物为序号规则；`../investigation.md` D7 行按此更新 | 已记账 |

## 7. 独立代码 review
（见 §7.1，reviewer 结果落地后回填）

## 8. DoD 核对
（见 §10 前的核对表）
