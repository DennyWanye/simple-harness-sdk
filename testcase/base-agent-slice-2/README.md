# BaseAgent Slice 2 · 生命周期与批量创建 · testcase 归档

- 验收标准：`plans/2026-09-10-base-agent-phase1/slice-2/acceptance.md`（AC1–AC13，V1–V5）
- 全部 testcase 均为可复跑的 pytest 脚本（库类被测对象）
- 回归入口：`.venv/bin/python -m pytest -q tests/agents tests/execution/test_base_agent_schema_v10.py tests/unit/contracts`
- 价值里程碑：`.venv/bin/python -m pytest -q tests/agents/test_agent_lifecycle.py`

## AC ↔ 脚本绑定

| AC | 矛盾地位 | 脚本（决定性断言） | 步骤要点 |
|---|---|---|---|
| AC1 close 拒收·不死·跨重启 | 决定性 | `tests/agents/test_agent_close.py`（8 例）、`tests/agents/test_agent_lifecycle.py` | close 回执 closed → submit 抛 `AgentClosed` → Run WAITING、run_events 无 `run.completed/failed/cancelled` → 同 command_id 回执逐字段相同 → drain 超时回执 closing 且随后自动收敛 closed → 重启仍拒收 → `shutdown()` 不动 binding |
| AC2 顺序·不并发写 Context·队列配额 | 决定性 | `tests/agents/test_input_queue.py`（7 例） | gather 投 i1/i2 → seq 1/2 与 fifo 一致、第二次请求含第一轮回答；`SqliteContextPort.append` 区间不重叠、revision 递增、零冲突；`_drive` 不重叠；第三条投递 `AgentPendingInputsExhausted` 零写入；幂等重放不受配额影响；空转 Agent 不持租约/心跳；30 条突发输入零丢失 |
| AC3 超时/放弃等待不取消 Turn | 决定性 | `tests/agents/test_turn_resume_identity.py::test_ask_timeout_carries_the_receipt_and_caller_cancel_does_not_cancel_the_turn` + S1 `test_build_agent_runtime.py::test_ask_timeout_does_not_cancel_the_turn` | 超时错误携带原回执（turn_id/seq/input_id）；等待 task 被 cancel 后 Turn 照常提交、provider 1 次 |
| AC4 UNKNOWN/授权同 Turn 恢复 | 必须 | `tests/agents/test_turn_resume_identity.py`（UNKNOWN、授权、身份拒绝、单键+序号、阻塞快照） | transport 失败 → 阻塞器、turn_snapshot.blocked；reconcile 后同 Turn 续跑，`provider_invocations` 仍 1 行、ack 1 次、seq 不变；授权 REQUIRE_USER→ALLOW 同 Turn 续跑、effect 1 行；伪造在途 checkpoint 驱动另一 Turn → `base_agent_turn_identity_conflict` 且 provider 不被调用 |
| AC5 批量幂等/冲突 | 必须 | `tests/agents/test_create_many_batches.py::{test_same_batch_key_same_content_returns_the_same_ids,test_same_batch_key_different_content_conflicts,test_reserved_batch_is_resumed_not_duplicated,test_existing_binding_conflict_is_caught_before_any_write}` | 同内容逐位相等且跨重启；内容/顺序/数量变化 → `batch_identity_conflict` 逐表行数不变；reserved 后崩溃续做补齐、batch 仍 1 行 |
| AC6 整批零副作用 | 必须 | `test_create_many_batches.py::{test_invalid_config_leaves_no_partial_batch,test_batch_size_and_instance_caps_are_enforced_before_any_write}` | 第 3 个 config 工具不存在 → `AgentBatchRejected(index=2)`，六张表行数与调用前相等；`max_batch_size`/`max_agents` 越界写前拒绝 |
| AC7 100 IDLE 不发请求 | 必须 | `test_create_many_batches.py::test_hundred_idle_agents_make_no_provider_request` | provider 0 次、turns 0 行、100 个不同 id、Context revision 0、协调器单例、重启 recover 不激活 |
| AC8 owner 闸门 | 必须 | `tests/agents/test_agent_open_scope.py`（5 例） | 跨 owner 与不存在 id 同类同模板异常；消息不含 name/instructions/creation_key；`binding()` None；同 owner 重启仍可 open |
| AC9 失败不清空会话/费用 | 次要 | `tests/agents/test_cancel_turn.py::test_turn_failure_keeps_session_and_cost` | 第 2 轮超限失败 → history 仍含第 1 轮、Context 不回退、committed_micros 不减、第 3 轮请求含第 1 轮对话、seq 1/2/3 |
| AC10 per-turn 执法 | 次要 | `tests/agents/test_turn_limits.py`（7 例） | 模型调用/工具调用上限 → 失败 Turn + checkpoint ready + 下一轮正常；deadline 锚定首次准入、重启不延长；policy_fingerprint 不随限额变化；两 Agent 互不干扰 |
| AC11 cancel_turn | 次要 | `tests/agents/test_cancel_turn.py`（8 例） | 取消点观察 → `agent_turn_cancelled`、Run WAITING、continuation ACKED；同 command_id 幂等；已结算 → `already_settled` 不改写；effect 账本不变、无"已回滚"字样；generation +1；排队中的取消意图跨重启生效且不调 provider |
| AC12 D7 记账 | 次要 | `test_turn_resume_identity.py::test_checkpoint_stays_single_key_and_ordinals_never_repeat` | 只有 `react.termination.v1` 一个 namespace；两轮序号 (0,2)/(2,5) 严格递增、request_id 不重复 |
| AC13 空正文可见失败 | 次要 | `tests/agents/test_provider_response_durability.py`（2 例） | 空正文无 tool_calls → `provider_empty_response` 失败 Turn、invocation failed、下一轮正常；tool_calls 响应的空正文不受影响 |

## 非功能

| 项 | 脚本 / 证据 |
|---|---|
| schema v10 三次追加、v7/v8/v9 checksum 冻结、v9 仍可打开 | `tests/execution/test_base_agent_schema_v10.py`（10 例） |
| facade 形状与 fault 注入点 | `tests/agents/test_uow_facade_shape.py` |
| 纯重构护栏 | `tests/agents/test_turn_finalize.py`、`tests/agents/test_base_agent_kernel_spike.py` 未改全绿 |
| 内核不 import agents、根包 API 不变 | `tests/agents/test_config_contracts.py`、`tests/unit/contracts` |
| 真实端点 | `tests/agents/test_delegation_e2e_real_provider.py --run-real-provider`（journal §4.3：4/4） |
