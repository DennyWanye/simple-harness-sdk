# BaseAgent Slice 1 · 委派最短价值链 · testcase 归档

- 验收标准：`plans/2026-09-10-base-agent-phase1/slice-1/acceptance.md`（AC1–AC10，V1–V5）
- 全部 testcase 均为可复跑的 pytest 脚本（库类被测对象，phase-4 ①路由：自动化脚本）
- 回归入口：`.venv/bin/python -m pytest -q tests/agents tests/execution/test_base_agent_schema_v10.py tests/execution/test_short_context_migration_still_targets_v9.py`

## AC ↔ 脚本绑定

| AC | 矛盾地位 | 脚本（决定性断言） | 步骤要点 |
|---|---|---|---|
| AC1 委派创建子 Agent | 决定性 | `tests/agents/test_delegate_tool.py::test_delegate_creates_child_agent_and_returns_child_result_row` | 主 Agent 调 `agent_delegate` → bindings 多一行 role=child → 子 run.parent_run_id = 主 run → 委派行 settled → `child_terminal_receipts` 0 行 |
| AC2 结果回到主 Agent | 决定性 | 同上 + `tests/agents/test_delegation_e2e_mock.py` | NONCE 只在子模板里；主最终 `public_output` 含 NONCE；工具结果携带子 result_hash |
| AC3 单层/配额/幂等 | 决定性 | `test_delegate_tool.py::{test_child_catalog_excludes_delegate,test_quota_rejects_second_delegation_in_same_turn,test_same_delegation_id_is_idempotent,test_one_delegation_is_exactly_one_child_turn}` | 子快照工具集无 delegate；第二次 REJECTED `agent_delegation_quota_exceeded`；同 id 只 launch 一次 |
| AC4 同 Agent 多轮 | 决定性 | `tests/agents/test_base_agent_kernel_spike.py::test_two_results_on_one_run_never_terminal`、`test_agent_driver.py::test_two_turns_same_agent_reuse_one_execution_identity`、`test_delegation_e2e_mock.py` | seq 1、2；run_events 无终态；第二次请求含第一轮回答 |
| AC5 RESULT_PENDING 后重启不重生成 | 决定性 | `test_turn_finalize.py::test_result_pending_then_kill_then_recover_commits_once`、`test_base_agent_kernel_spike.py::test_stage_then_kill_then_recover_commits_once` | stage 后崩溃 → 新运行时恢复 → 结果行 1、hash 不变、provider 不重调 |
| AC6 实例互相独立 | 次要 | `test_build_agent_runtime.py::{test_create_returns_independent_agents,test_create_does_not_call_provider}` | 3 个不同 id/run；其它 Agent Context revision 0；创建 provider 0 次 |
| AC7 旧 API 拒新模式 | 次要 | `test_api_mode_fence.py`（9 例）+ `test_delegate_tool.py::test_child_run_is_mode_fenced_before_it_exists` | RUN_MODE_CONFLICT 双向；ordinary 独立 react Runtime 不受影响；start_snapshot.py / start_mode.py sha256 冻结 |
| AC8 不依赖用户记忆 SDK | 次要 | `test_build_agent_runtime.py::test_no_memory_entrypoint_is_called`、`test_delegation_e2e_mock.py` | ports 全 None；`sys.modules` 无 `simple_harness_memory`；agents/ 源码无该 import |
| AC9 委派 UNKNOWN 结算 | 可选 | `AgentDelegationReconciliation`（`agents/tools/delegate.py`）；间接由 `test_delegate_timeout_returns_visible_result_not_raise` 覆盖等待面 | 见 journal 遗留：UNKNOWN 注入的端到端断言归 S2/S5 |
| AC10 真实模型复现 | 可选 | `tests/agents/test_delegation_e2e_real_provider.py`（`--run-real-provider`） | 见 journal §4.3 报告 |
| V1–V5 变异 | — | `test_delegate_tool.py::{test_unknown_delegation_id_is_rejected_not_created,test_malformed_arguments_never_create_child,test_oversize_objective_is_rejected_before_launch,test_oversize_child_result_returns_ref_not_truncated_fact,test_no_delegation_still_commits_a_valid_turn}` | 每条都断言"不落库、不建子、Agent 不终态" |

## 非功能

| 项 | 脚本 |
|---|---|
| schema v10 / v9 仍可打开 / 描述符 checksum 冻结 | `tests/execution/test_base_agent_schema_v10.py` |
| v7/v8→v9 升级器仍以 v9 为目标 | `tests/execution/test_short_context_migration_still_targets_v9.py` |
| 契约与分层（kernel 不 import agents） | `tests/agents/test_config_contracts.py` |
| 同事务 ack 全有或全无、幂等回放 | `tests/agents/test_turn_finalize.py` |
| 创建不驱动模型、意外 continuation 不杀 Agent、预算超限=失败 Turn、provider 拒绝可恢复、未知工具名可见拒绝 | `tests/agents/test_agent_driver.py` |
| queued Turn 跨重启、超时不取消、instructions 到模型、历史按 Agent 隔离、import 纯度 | `tests/agents/test_build_agent_runtime.py` |
| assistant tool_calls 线上恢复、持久 Context 无元数据 | `tests/agents/test_provider_wire.py` |
| 公共 API 只增不减 | `tests/unit/contracts/test_public_api.py::test_base_agent_successor_preserves_h0710_exports` |

## 结果回写

见 `plans/2026-09-10-base-agent-phase1/slice-1/journal.md` §2（每任务命令与结果）、§4（核心价值 smoke、mock 与真实 provider 两份报告）、§5（兑现表）。
