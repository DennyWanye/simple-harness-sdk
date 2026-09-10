# Slice 1 执行 journal · 委派最短价值链

- 仓库：simple-harness-sdk，执行基线 main = `fd12e7dd`（0.7.10）
- 方法：plan-task（plan 由主编排者依据 BA-v1.0 拆片，见 `../program.md`）
- 上游：Host `plans/taskSys2/base-agent-phase1-plan.zh-CN.md`

## 0. 路径判定与执行模式（开场留痕）

- `TASK_TYPE = delivery`。
- `FLOW_TIER = FULL`：命中"新持久化状态 / 多阶段状态机 / 公共 API 导出面变化"。
- `MACHINE_GATE`：**不启用**，完成记录走一页 journal + DoD 清单（无机器 receipt）。理由：本片新增 `base_agent` 模式默认不被任何 Host 使用；已有 v9 库只要求"仍可打开"，**不做**就地迁移器（迁移器与回执归 S5，届时再评估机器账本）；不涉及权限/身份/支付/共享基础设施。本片所有声明仍须附实测证据（命令 + 输出）。
- 输入语义敏感 / LLM 载荷驱动：`agent.delegate` 由模型输出驱动 → acceptance 已含 LLM 行为变异清单 V1–V5，每条有端侧容错断言；真人测试广度门不适用（SDK 库，无 UI），真实 provider 端到端作为证据留证。
- 冷路径：`build_agent_runtime` 对空目录直建 v10 库即冷路径，由 T2/T9 覆盖。
- **执行模式：集中兵力（当前 session 串行）**。T2→T9 环环相扣（schema → 契约 → mode → 内核分流 → driver → 装配 → 工具 → 端到端），且主要矛盾链路需要连贯的内核上下文；做完一个任务验一个、提交一个。T10 的真实 provider 演示与快照更新独立，可在 T9 之后并行。
- 回归口径见 `../baseline.md`；新红一条即阻断。

## 1. 计划挑战记录

- 第 1 轮（primary breadth，独立 Opus 挑战者）：4 P0 / 12 P1 / 5 P2，全部裁决见 `challenge-round-1.md`；核心发现：BaseAgent 永不终态会切断依赖终态的 `child_terminal_receipts` 通道 → 改为直接读子 Agent 结果行；用 `driver_kind="base_agent"` 取代新 start_mode，消掉两条 P0；finalize 同事务 ack continuation。
- **修订完成（2026-09-10）**：`plan.md` / `acceptance.md` 已改为 v2（`plan-status: finalized-v2`），21 条裁决的逐条落点见 `plan.md` 附 B；任务重排为 10 个，**价值验证里程碑 = T6.5**（内核级 spike），完整价值链验收 = T9。`program.md` 的主要矛盾与 S1 段已同步。closure 复核在执行开始前做一次。

## 1.1 v2 修订中新发现、但不在本片处理的问题

1. **`build_runtime` 硬锁 root profile key**（`kernel.py:147/3473-3479`）：`root_profile_key` 固定为 `"agent.general"`，无法直接用 `RuntimeProfile("agent.base","base_agent")` 作为 root。落地口径改为：root 条目键仍为 `"agent.general"` 但 `driver_kind="base_agent"`；`"agent.base"` 作为**子 Agent 的 profile_key** 一并注册（子 Run 由 `ChildCoordinator.launch` 按 `launch_request` 建立，不查 `self._profiles`）。裁决 #16 要求的"未注册即拒绝"由 `kernel.py:3490-3493` 天然成立。
2. **裁决 #11 的"同事务"落地方式**：`claim_profile_launch_and_commit_child`（`uow.py:4585-4790`，200 行、自开事务）内部无法插入模式行而不重写该方法。改为**先围栏后建 Run**（围栏行 + 委派行 + 子 binding 同一事务，且早于子 Run 存在），与 `RunClient.start_conversation` 的 `reserve_legacy_run_mode`（`kernel.py:911-933`）先于 `_start_run`（`:949-950`）同范式。**保证不变**：子 Run 从存在的第一刻起即被围栏。
3. **裁决 #8 的改动面比裁决表写的宽**：`short_context_migration.py` 有 **4 处**（`:83/:114/:191/:194`）`fresh_descriptor()` 语义都是"v9 目标"，都要改成 `legacy_v9_descriptor()`，不止 `_validate` 一处。
4. **T4 需要两个公开入口而非一个**：除裁决 #5 点名的 `Runtime.signal_base_agent_input`，创建路径也需要 `Runtime.start_base_agent_run`（否则 `AgentRuntime.create` 只能调私有 `_start_run`）。两者成对，同属围栏任务。

## 2. 任务执行记录

| 任务 | commit | 验证命令 | 结果 | 备注 |
|---|---|---|---|---|
| T2 schema v10 + 冻结 v9 | `1ba3bba` | `pytest tests/execution/test_base_agent_schema_v10.py tests/execution/test_short_context_migration_still_targets_v9.py`；固定回归命令 | 8 passed；回归 60 failed / 15 errors 与基线逐条相同 | v9 库仍可打开；v7/v8→v9 升级器仍以 v9 为目标 |
| T3 契约骨架 | `d838a22` | `pytest tests/agents/test_config_contracts.py` | 21 passed | ast 扫描：kernel/agent_turn 不 import agents |
| T5 结果载体 + finalize | `308a67e` | `pytest tests/agents/test_turn_finalize.py`；固定回归命令 | 5 passed；回归红集与基线相同 | 同事务 ack 全有或全无（fault 钩子实测）；任何唤醒路径先 finalize 再 drive |
| T6 AgentExecutionDriver | `f1e149b` | `pytest tests/agents/test_agent_driver.py` + legacy ReAct 回归 3 文件 | 5 passed；legacy 28 passed | 预算超限 = 失败的 Turn，Agent 不死；意外 continuation 被 ack 不终态 |
| **T6.5 价值验证里程碑** | `66f875d` | `.venv/bin/python -m pytest -q tests/agents/test_base_agent_kernel_spike.py` | **3 passed** | 主要矛盾"不死的执行身份"一面已被真实 driver 证明 |
| T4 围栏与公开入口 | `9fc1335` | `pytest tests/agents/test_api_mode_fence.py` | 9 passed；回归红集与基线相同 | start_snapshot.py / start_mode.py sha256 冻结闸 |
| T7 build_agent_runtime / BaseAgent / AgentRuntime | 本次 | `pytest tests/agents/test_build_agent_runtime.py` | 8 passed；回归红集 ⊆ 基线（import purity 转绿） | queued Turn 跨重启由 kernel.recover 唤醒（E6） |

## 3. 触碰既有红的说明

- T2 schema v10：实测 v10 后红集与基线逐条相同（既有 schema/迁移夹具本来就因缺旧环境或旧断言而红，失败方式未变），无需解释差异。
- T7：`tests/artifact/test_import_purity.py::test_import_has_no_host_or_runtime_side_effects` 由红转绿——根因是根包 `__all__[0]` 不是 `"__version__"`（四个 `MandatoryContext*` 条目被放在最前），本片把 `"__version__"` 移回首位并追加 BaseAgent 导出。这是**触碰既有红**，不是本片引入的行为变更；`baseline-known-failures.txt` 保持冻结，回归口径改为「红集 ⊆ 基线红集」。


## 4. 核心价值 smoke

### 4.1 里程碑 T6.5（内核级 spike，2026-09-10）

命令：`.venv/bin/python -m pytest -q tests/agents/test_base_agent_kernel_spike.py` → `3 passed`。
- `test_two_results_on_one_run_never_terminal`：同一 Run 两条输入 → 两条结果行（seq 1、2），run_events 全量回放无 completed/failed/cancelled，第二次 provider 请求含第一轮 assistant 回答，provider 恰好调用 2 次。
- `test_input_continuation_is_acked_and_rescheduled`：两条 continuation 均 ACKED 且回执 id 为 `{run}:progress:{cid}:{epoch}`；第二条输入无需外部唤醒即被消费；`_reschedule` 被调用。
- `test_stage_then_kill_then_recover_commits_once`：在 stage 之后 finalize 之前注入崩溃，旧租约过期后新 Runtime 恢复：结果行恰好 1、hash 与 staged 相同、provider 仍只调用 1 次、Run 保持 WAITING。

**矛盾转化再分析**：主要矛盾的第一面（执行身份能反复交结果而不死）已解决；现在决定成败的问题转为第二面——**父 Agent 能否在不依赖终态的前提下可靠取回子 Agent 的结果并做幂等结算**（T8 的结果直读 + 续做语义），其次是公开入口与围栏（T4）让这条链能被应用代码而不是测试内部路径调用（T7）。剩余任务顺序不变：T4 → T7 → T8 → T9 → T10。

### 4.2 完整价值链（T9）

（待 T9）

## 5. 兑现表

## 6. 遗留清单

> 以下三条在 v2 修订时已确定为"本片不做、如实记账"，执行期只需确认没有被无意扩大。

1. **残留冻结窗口（裁决 #9，归 S5 的 BA31）**：`react_loop.py:717-732` 的最终 CAS（phase 复位、清空 request/response 快照）与本片的 stage 事务之间存在一个未被同一次 CAS 覆盖的窗口；在该窗口内崩溃会重新调用模型。本片的 AC5 失败域**只从 stage 之后起算**，不写"假装通过"的测试。处置方案：S5 把 `RESULT_PENDING` 并进 react_loop 的同一次 CAS。
2. **v10 之后旧 v9 库的只读审计通道会拒绝**：`execution/sqlite/database.py:145` 的只读审计 reader 断言 `reader.schema_version != SCHEMA_VERSION` 即 `RunAuditUnavailable`。v9 库仍能 `Database.open`，但审计 reader 会拒。既有设计（审计只服务当前版本），v10 只是把界线从 9 挪到 10。归 S5 的 BA37。
3. **`execution/sqlite/migrations/execution_v5_to_v6.py:156` 用 `fresh_descriptor()` 盖戳**：v5→v6 迁移完成后把 `sdk_schema_migrations` 单行改写成"当前 fresh 版本"，v10 之后会盖成 `(10,"0010_fresh",…)`，而库里并无 v10 的表。这是**先于本片就存在**的错位（今天已经会盖成 9），本片不改也不扩大；`tests/execution/test_execution_v5_to_v6_catalog_migration.py` 只走 `accepted_descriptor_rows()` 校验，不会因此变红。归 S5 的 BA37。
4. **UNKNOWN 期间的 AgentTurn 停滞**：`agent_turn_outcome` 与 `wait_blocker` 被排他校验挡死（刻意：不确定的动作不得被冻结成已完成结果），因此 provider/工具 UNKNOWN 期间该 Turn 停在 `running` 直到 blocker 解除。完整语义归 S2 的 BA11。

