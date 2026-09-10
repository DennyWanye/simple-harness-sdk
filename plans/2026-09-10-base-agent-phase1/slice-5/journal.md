# Slice 5 执行日志：并发、恢复、迁移与发布（2026-09-10）

- 提交：`e0c6771`（BA31/BA35）、`5fb33cb`（BA37 迁移 + 0.8.0 + CHANGELOG，与 S4 review 修复同提交）、`1cb8406`（测试 + 内核不终态化 + v9 容忍）
- 方法同前；`react_loop.py`、`react_checkpoint.py` 各只加一个可选参数（未对整文件 ruff format；diff 分别 +19/-4、+9/-1）

## 1. 关键裁决

1. **BA31 的实现路径**：program.md 写"把 RESULT_PENDING 并进 `react_loop.py:717-732` 的同一次 CAS"。做法是给最终 CAS 加可选 `companion(connection)`，driver 提供 stage 结果的 companion；内核随后的 stage 是幂等 no-op（同 hash）。
2. **driver 异常不终态化 BaseAgent Run**（执行期发现）：原内核对 `driver.start` 抛异常一律 `driver_failed` 终态化，与 BA-v1.0 §1.3"Agent 不因一轮失败而死"冲突，也让 BA31 用例无从恢复。改为放弃权限并保留 claimed 输入；kernel-integrity 错误仍走显式 FAILED `DriverResult`。
3. **BA33 语义**：接管者对旧执行者的在途 provider 调用**不重发**，等 Host 调和（`CONFIRMED_NOT_STARTED`）后才重发；测试按此写。
4. **v9 库容忍**：新 SDK 打开 v9 库时内核的 `list_runs_with_open_agent_turns` 返回空，legacy Run 正常；BaseAgent 装配对 <v10 明确拒绝并提示 `migrate_execution_to_v10`。

## 2. 执行记录

| 任务 | 状态 | 测试 |
|---|---|---|
| T1 BA31 | 完成 | `test_crash_after_final_cas_never_regenerates_the_answer` |
| T2 BA32 | 完成 | `test_input_arriving_while_the_idle_drive_releases_is_not_lost` |
| T3 BA33 | 完成 | `test_stale_lease_holder_cannot_overwrite_the_new_owner` |
| T4 BA34 | 完成 | `test_request_ids_stay_unique_across_restarts_and_turns` |
| T5 BA35 | 完成 | `test_concurrency_caps_are_enforced_fairly_across_agents` |
| T6 BA36 | 完成 | `test_history_queries_are_bounded_tool_calls` |
| T7 BA37 | 完成 | `tests/execution/test_execution_v9_to_v10_migration.py` 4 例 |
| T8 BA39 | 完成 | `test_lost_tool_response_is_never_blindly_retried` |
| T9 BA40 | 完成 | §4 |

## 3. 独立 review（回填）

## 4. 发布证据（BA40）

- 版本 `0.8.0`（`src/simple_harness/version.py`）；`tests/unit/contracts/public-api.json` 只新增根导出 `migrate_execution_to_v10`、`ExecutionBaseAgentUpgradeReceiptV1`（旧快照存 `public-api-0.7.10.json`）。
- wheel：`git archive HEAD` + `SOURCE_DATE_EPOCH=<commit ct> uv build --wheel` → `simple_harness_sdk-0.8.0-py3-none-any.whl`，sha256 `d2321b60fc6844fa49c406b4bce26447496445585c7f5ae81506718f2de69c77`（基于 `5fb33cb`）；干净 venv（uv, py3.12）安装后跑 `tests/agents + schema + contracts`：**217 passed, 3 skipped**。最终发布 wheel 以推送前 HEAD 重建并记入 Host 钉版清单。
- 真实端点全链路（S1 委派链，DeepSeek）在 S5 HEAD 上：run 7 通过；run 8 见 §5 说明。

## 5. 遗留

| # | 事项 | 归属 |
|---|---|---|
| L5-1 | driver 异常不终态化后，确定性 driver 异常会在每次唤醒重试（无退避/上限）；唤醒来源为 recover/输入/调和，不自旋 | 后续观测项 |
| L5-2 | 索引任务并发只有单泵；`max_concurrent_index_jobs` 未做 | 后续 |
| L5-3 | 语义摘要调用的成本账本（BA36 的摘要部分）未做（无语义摘要） | 后续 |

## 6. 终态（回填）
