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

review 由独立子代理（claude-opus-5）对 Slice 5 累计 diff 做正确性 review，原文见 `reports/review-s5-independent.md`。处置如下（全部有决定性测试，`tests/agents/test_slice5_review.py` 6 条 + `tests/execution/test_execution_v9_to_v10_migration.py` 1 条）：

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| K1 | P0 | `kernel._drive` 中 base_agent driver 异常只放弃权限，没人再唤醒（不进 `_pending_wakes`，只有 recover() 扫描）；且确定性异常无上限重试 | (1) driver `start()` 加边界：已入队 Turn 内的任意异常 → 可见失败 Turn `base_agent_driver_exception`（含 error_type/message），Agent 存活；UNKNOWN 效应或无 Turn 行时才抛给内核。(2) 内核分支：放弃权限后加入 `_pending_wakes`，同进程每 Run 最多 `BASE_AGENT_DRIVER_EXCEPTION_WAKES=3` 次再唤醒（driver 正常返回即清零），超限记 error 日志、留给 recover()。(3) 排水循环对"上一 drive 仍在收尾"的 Run 保留唤醒而不是丢弃。(4) `execution.py` 的 `raise ValueError/TypeError` 改为 `_binding_failure`（`base_agent_input_mismatch`/`base_agent_input_malformed`）。 |
| M1 | P1 | 备份路径已存在的外来 v9 文件被接受 | 迁移前计算源库 `_root` 快照哈希写入回执 `source_root_hash`；已存在的备份文件必须 `_root` 相同才接受；回放时重新校验备份的版本与 `_root`。 |
| C1 | P1 | `ExposureGuardedTool.invoke` 在 `agent_delegate` 等子 Agent 期间持有工具信号量，`max_concurrent_tool_calls=N` 下 N 个父等待即死锁 | 新增 `simple_harness/tools/permit.py`（ContextVar 携带 `ToolPermit`，放在 `tools` 包以免 agents 包重导入改变身份）；委派工具进入等待前 `release_tool_permit()`；finally 幂等释放。回归：`max_concurrent_tool_calls=1` 下父委派、子用 echo 工具，链路完成且 `max_tools_in_flight==1`。 |
| M2 | P2 | `_validate` 不校验 catalog/audit 内容 | 文档化保证范围（描述符序列 + integrity/FK + 半应用探测），行级内容由 `source_root_hash` 绑定备份。 |
| E1 | P2 | 输出上限升级循环每次尝试不再检查持久取消意图/截止；每次尝试弹出取消令牌 | 令牌改为每次入队一个（try/finally 包住整个循环）；每次升级前重查令牌、持久取消意图与 Turn 截止。 |
| E2 | P2 | 成功时不记录 `output_cap_escalations` | 成功的 DriverResult payload 带 `output_cap_escalations`（outcome 本身与 companion 保持字节一致）。 |
| A1 | 推测 | companion 内 `stage_result` 抛错会回滚最终 CAS | companion 内捕获 `ValueError/RuntimeError/sqlite3.Error`，记 warning 后由内核自身 stage 兜底。 |
| 记录 | 备注 | 提交改写了 `public-api-0.7.10.json` | 该文件是从 0.7.10 时的 `public-api.json` 另存的旧快照，仅作记录，不参与契约测试。 |

复验：`tests/agents + tests/execution + tests/unit/contracts` 360 passed / 6 failed（全部基线红）；全量回归 73 红 ⊆ 基线，0 新红。

## 4. 发布证据（BA40）

- 版本 `0.8.0`（`src/simple_harness/version.py`）；`tests/unit/contracts/public-api.json` 只新增根导出 `migrate_execution_to_v10`、`ExecutionBaseAgentUpgradeReceiptV1`（旧快照存 `public-api-0.7.10.json`）。
- wheel（预览，review 前）：基于 `5fb33cb`，sha256 `d2321b6…69cf77`，干净 venv 217 passed / 3 skipped。
- **最终 wheel（review 处置后，钉入 Host 的版本）**：`git archive d1f5166` + `SOURCE_DATE_EPOCH=1789049537 uv build --wheel` → `simple_harness_sdk-0.8.0-py3-none-any.whl`，sha256 **`8affc2c3fb7b82f506b92d4d9395145867c264e232d28191f919ababfc417d4e`**（源提交 `d1f51660d2a9144958e0ee0321a3c19f6ed50887`）；干净 venv（uv, py3.12，仅装 wheel + pytest + tiktoken）从归档源根跑 `tests/agents + schema v10 + v9→v10 迁移 + contracts`：**238 passed, 3 skipped**（探针确认 import 自 site-packages）。
- 真实端点全链路（S1 委派链，DeepSeek）在 S5 HEAD 上：run 7 通过；run 8 见 §5 说明。
- review 处置后（`d1f5166`）真实端点复跑：`reports/real-delegation-run9.txt` **1 passed（11.6 s）**，委派链 + 工具许可释放路径在 DeepSeek 上跑通。

## 5. 遗留

| # | 事项 | 归属 |
|---|---|---|
| L5-1 | ~~driver 异常无上限重试~~ 已由 review K1 处置：Turn 内异常成为可见失败 Turn；逃逸内核的异常同进程最多 3 次再唤醒，之后只靠 recover() | 已关闭 |
| L5-2 | 索引任务并发只有单泵；`max_concurrent_index_jobs` 未做 | 后续 |
| L5-3 | 语义摘要调用的成本账本（BA36 的摘要部分）未做（无语义摘要） | 后续 |

## 6. 终态（回填）

**VERDICT: SHIPPED**（2026-09-10，SDK main `d1f5166` + 本文档提交）

| 项 | 结果 |
|---|---|
| 决定性测试 | `test_slice5_recovery.py` 7 条 + `test_slice5_review.py` 6 条 + 迁移 5 条 + 响应耐久 4 条：全绿 |
| 独立 review | P0 ×1、P1 ×2、P2 ×3、推测 ×1 全部处置（§3） |
| 回归 | 全量 73 红 ⊆ 基线（0 新红）；agents/execution/contracts 360 passed / 6 基线红 |
| 真实端点 | DeepSeek 委派链 run 7、run 9 通过（run 8 为模型改写 nonce） |
| 发布 | 0.8.0 wheel sha256 `8affc2c3…417d4e`，干净 venv 238 passed |
| 遗留 | L5-2、L5-3（§5）；Host 集成与原生 App 手工测试在 Host 仓库记录 |
