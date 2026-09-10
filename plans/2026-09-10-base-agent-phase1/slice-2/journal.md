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
本片跑 6 次委派链（T10 前 4 次、review 修复后 2 次）：**6/6 passed**（13.8 / 38.5 / 17.6 / 20.3 / 19.4 / 25.7 s），`main_error=None`，`finish_reasons=[tool_calls,stop,stop]`，`empty_contents=[True,False,False]`（首个 True 为 tool_calls 响应，属正常）；脱敏报告在 `reports/real-provider-run{1..6}.txt`。F-BA-1 累计 12 次未复现；若再出现空正文，现在会以 `provider_empty_response` 可见失败落在 Turn 结果里而不是 dispatch 深处的 `provider_response_not_durable`。

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
| L2-9 | 空正文/不可持久化响应的真实 usage 未进预算账本（dispatch 只在成功路径记 usage；本片把 usage 写进 Turn 结果 `error.detail`） | S5（dispatch 失败结算带真实 charge） |
| L2-10 | 工具自己抛 CancelledError 导致的 UNKNOWN effect 依赖 Host 调用 `record_tool_reconciliation` 调和（legacy H13 路径）；SDK 内无表驱动的 effect 清扫器 | 既有设计，S5 BA37 一并看 |

## 7. 独立代码 review（子代理，审 `701d89c..649d411` 全 diff，2026-09-10）

结论：1 条 P1、5 条 P2（其中 1 条标 speculative）；无 P0。同时确认无缺陷的面：序号身份规则（UNKNOWN/授权/重启三条恢复路径无误判）、cancel token 以 turn_id 全局唯一且 finally 清理、close 与 submit/finalize 收敛无竞态、内核改动对 legacy Run 无语义影响、`_abandon_run_authority` 与并发 `_activate` 之间无租约窃取窗口。

| # | 发现 | 处置（提交 `d0de87b`） | 测试 |
|---|---|---|---|
| F1 P1 | `_settle_failed_turn` 复位 `tool_batch_reserved` 会让"工具自己抛 CancelledError → 执行器标 UNKNOWN"的 effect 永远无人调和（effect 身份由 run 级 totals 派生，工具 reconcile 只从 execute() 进入） | 三处闸门：driver 收到取消型 CancelledError 时若有 UNKNOWN effect → 走 `ToolEffectUnknownError` 等待路径而非失败；准入期读到取消意图但仍有 UNKNOWN effect → 继续等待；`_settle_failed_turn` 存在 UNKNOWN effect 时不复位。Host 用 `record_tool_reconciliation` 调和后，唤醒 → 意图生效 → 失败 Turn + 复位。新增 uow `list_unknown_effects_for_run` | `test_cancel_turn.py::test_cancel_inside_a_tool_leaves_the_effect_reconcilable_not_orphaned`（三段：阻塞不复位 → Host 调和后取消生效、effect succeeded、checkpoint ready → 下一轮正常） |
| F2 P2（speculative） | 复位不清 `mandatory_context_repairs` | 复位时一并清 `mandatory_context_repairs=()` 与 spawn 相关字段（与 loop 自身的轮末转换一致）；BaseAgent 未启用 Context-use，无用例可达 | — |
| F3 P2 | close drain 遇 `mark_agent_closed` 冲突时 `continue` 跳过 deadline 与 sleep → 多进程下死转 | 冲突路径落到同一段有界等待 | `test_agent_close.py::test_close_drain_conflict_is_bounded_not_a_spin` |
| F4 P2 | `max_agents` 只在 `create_many` 预检、非原子；单个 `create` 与委派路径无上限 | 上限在 `insert_binding` 同事务判定（`AgentInstanceCapExceeded`，公开错误 `agent_instance_cap_exceeded`）；`create`/委派路径都传 `ports.max_agents`；委派子 Agent 与父同 owner 计入（记账） | `test_create_many_batches.py::test_instance_cap_is_enforced_at_insert_for_single_create` |
| F5 P2 | `_wake_continuation` 激活冲突时静默 return，输入滞留到 recover() | 冲突登记 `_pending_wakes`，`_wake_drain` 每 tick 重试 | `test_input_queue.py::test_wake_activation_conflict_is_retried_by_the_drain_loop` |
| F6 P2 | wire 抛 `ProviderEmptyResponseError` 后 dispatch 走 settle_failed，真实 usage 不进预算账本 | 既有 `provider_response_not_durable` 路径同样如此（dispatch 只在成功路径记 usage）；本片把 `finish_reason` 与观测到的 usage 放进异常 `detail` 并写入 Turn 结果 `error.detail`；账本侧改动记为 S5 遗留 L2-9 | `test_provider_response_durability.py::test_empty_response_detail_keeps_finish_reason_and_usage` |

执行期另一处修正（F1 复现时发现）：`_wake_continuation` 等待在途 drive 结束后若 Run 已带未解决的 wait blocker，则**不再**重新唤醒（由 resolved-wait drain 唤醒），避免阻塞态 Run 被立刻重驱动。

## 8. DoD 核对

| # | 条款 | 证据 |
|---|---|---|
| 1 | AC1–AC8 全有测试并通过；AC9–AC13 各至少一条 | §5 兑现表 |
| 2 | T3.5 里程碑绿 | §4.1 |
| 3 | 全片套件绿 | `tests/agents + test_base_agent_schema_v10 + tests/unit/contracts` = 195 passed / 1 skipped |
| 4 | 回归红集 ⊆ 基线 | §3（review 修复后复跑见 §10） |
| 5 | mypy 0 issues | `.venv/bin/mypy` → Success (35 files) |
| 6 | 冻结文件 sha 不变、分层、公共 API、v9 可开、`migrate_execution_to_v9` 仍到 9 | §3；`tests/execution/test_short_context_migration_still_targets_v9.py` 在回归内绿 |
| 7 | V1–V5 端侧容错 | S1 用例一行未改仍绿（`test_delegate_tool.py`）；S2 新增空正文（V-empty）与超限/取消变异 |
| 8 | L12 纯重构 | `test_turn_finalize.py`、`test_base_agent_kernel_spike.py` 未改全绿；`test_uow_facade_shape.py` |
| 9 | D7 记账 | `challenge-round-1.md` 文末、`investigation.md` §9.2 修订、`../investigation.md` D7 行 |
| 10 | 不包含项未被声称完成 | §6 遗留：per-agent lifetime、跨 Agent 公平、v9→v10 升级器、强打断在途调用均未声称 |
| 11 | 记录文件齐全 | `challenge-round-1.md`、`journal.md`、`testcase/base-agent-slice-2/README.md`、`testcase/index.md`（`baseline-recheck.md` 未单独建：基线复核结论并入 §3） |
| 12 | 无 memory 依赖、根包 API 不变 | `grep simple_harness_memory src/simple_harness/agents` 仅 docstring 提及；`public-api.json` 未改、`tests/unit/contracts` 绿 |

## 9. 用户验收目标复核

主 Agent（同一 `BaseAgent` 类）收到复杂任务 → 经 `agent_delegate` 创建子 Agent 完成 → 结果回到主 Agent：S1 的 mock 链（`test_delegation_e2e_mock.py`）与本片 6 次真实端点链都在 Slice 2 代码上通过；本片新增的生命周期能力（close / cancel / 批量 / 限额）没有改变这条价值链。

## 10. 终态

- **VERDICT: SHIPPED**（本片范围内全部 AC 有可复跑测试且通过；review 的 1 条 P1 与 4 条 P2 已修并各配测试，1 条 speculative P2 已按建议清字段）
- 被测 HEAD：`21a2f04`（代码最后一次改动 `d0de87b`；本 journal 提交在其后）
- 复跑命令：
  - `.venv/bin/python -m pytest -q tests/agents tests/execution/test_base_agent_schema_v10.py tests/unit/contracts` → 195 passed, 1 skipped
  - 固定回归（同 S1 三个 `--ignore`）→ 58 failed + 15 errors = 73 红 ⊆ 基线，新红 0，1993 passed
  - `.venv/bin/mypy` → 0 issues
  - 真实端点：`--run-real-provider` + `SH_BASEURL/SH_APIKEY/SH_MODEL=deepseek-v4-pro` → 6/6
- 未推送：SDK main（本片 16 个提交）与 Host main 均未 push，等用户决定
- 本地 S1 时期的 v10 开发库需删除重建（v10 描述符重算）
