# Slice 1 · 计划挑战第 1 轮（primary breadth）与裁决

- 日期：2026-09-10
- 挑战者：独立 Opus 子代理（只读，按 `plan-primary-challenger.md`），覆盖八维全部为 true
- 被挑战版本：`plan.md` / `acceptance.md` 初稿（由调研子代理起草）
- 裁决人：主编排者（用户已授权不打断、自决技术取舍）

## 主要矛盾（挑战者重述，采纳）

> 成败取决于同一件事的两面能否同时成立：让 BaseAgent 的执行身份永不终态（才能"答完再问"），同时又要从子执行身份把结果可靠取回——而 SDK 里唯一的父子结果通道 `child_terminal_receipts`（`kernel.py:3193-3241`）只在子 Run **终态**时才产生。初稿把矛盾写成"只缺一个不死的执行身份、委派底座已就绪"，后半句被代码证伪。

## 逐条裁决

| # | finding | 级别 | 裁决 | 落到哪个任务 |
|---|---|---|---|---|
| 1 | child-terminal-receipt-never-written | P0 | **采纳方案 B**：子 Agent 也是永不终态的 BaseAgent；委派结果与 UNKNOWN 结算证据一律直接读子 Agent 的 `base_agent_turn_results_v1` 行，**彻底不用** `child_terminal_receipts`。父侧用"条件等待子 Turn 结果行（带超时，超时返回可见的 `agent_delegation_timeout` 结果，不抛）"。 | T8 |
| 2 | router-fingerprint-gate-fails-base-agent | P0 | **采纳 Ponytail（finding 16）**：不新增 start_mode，改用 `driver_kind="base_agent"` + `RuntimeProfile("agent.base","base_agent")`；`kernel.py:2735` 取到的就是真实 driver，指纹预检语义自然成立。 | T4 缩水 |
| 3 | start-snapshot-v8-version-gates-incomplete | P0 | 同上：**完全不动** `start_snapshot.py`；Agent 绑定（agent_id/config_hash）放 `start.input`（与 `capability_snapshot` 同处）。`test_v7_snapshot_bytes_unchanged` 保留作回归。 | T4 |
| 4 | continuation-ack-missing-in-agent-turn-commit | P0 | 采纳：finalize 事务内必须 ack 本轮 continuation（receipt_id 照 `commit_runtime_state_and_ack_continuation` 的 `{run_id}:progress:{continuation_id}:{claim_epoch}` 构成），提交后按既有范式 `_reschedule`。`_commit_agent_turn` 的签名带 `continuation_claim`。 | T5 |
| 5 | no-input-delivery-path | P1 | 采纳：`BaseAgent.submit` = 在一个短事务里写 `base_agent_turns_v1(phase='queued')` + `enqueue_continuation(kind="base_agent_input")`，然后调用 kernel 新增的**公开**入口 `Runtime.signal_base_agent_input(run_id, ...)`（只接受 base-agent namespace，对 legacy/unmanaged 拒绝——与 AC7 对称）。新增断言"提交后 Run 被重新调度"。 | T7 |
| 6 | create-drives-driver-immediately | P1 | 采纳：`AgentExecutionDriver.start` 在"无 `base_agent_input` continuation 且无 `result_pending` Turn"时立即返回 `DriverResult(RunState.WAITING)`，不进 ReActLoop、不调 provider；独立断言。 | T6 |
| 7 | public-api-version-pin-breaks-three-green-tests | P1 | 采纳：T10 把 `test_public_api.py` 的 `:17/:41/:53/:64` 四处硬编码版本改为与 `__version__` 比对；journal 记"触碰既有绿"。 | T10 |
| 8 | v10-breaks-v9-migration-validator | P1 | 采纳：T2 同步改 `short_context_migration._validate` 的期望 catalog 为 `legacy_v9_descriptor()`；该文件列入改动面；补一条"已有 v9 库 `migrate_execution_to_v9` 仍返回 9"的断言。 | T2 |
| 9 | result-freeze-window-outside-oracle | P1 | **收窄声明**：本片保证的是"stage 之后任意点崩溃不重生成"（= BA30 原文）；react_loop 最终 CAS 与 stage 之间的残留窗口**如实记入 journal 遗留**，归 S5 的 BA31 处理（届时把 RESULT_PENDING 并进同一次 CAS）。acceptance 的失败域措辞相应改写。 | acceptance、journal |
| 10 | agent-turn-outcome-layering-cycle | P1 | 采纳：`AgentTurnOutcome` 定义在 `runtime/agent_turn.py`（纯 dataclass，零 agents 依赖），agents 层单向引用；kernel 不 import agents。 | T5 |
| 11 | child-run-not-mode-fenced | P1 | 采纳：委派 helper 在建子 Run 的同一事务里写 `conversation_run_modes(namespace="base-agent/v1")`；AC7 覆盖 root 与 child。 | T8 |
| 12 | primary-contradiction-one-sided | P1 | 采纳：矛盾陈述改写为两面一体；T8 由"最小封装"升为核心任务之一。 | acceptance、program |
| 13 | value-milestone-behind-eight-tasks | P1 | 采纳：在 T6 之后插入 **T6.5 里程碑 spike**（裸 `build_runtime` + `base_agent` driver_kind，不依赖 agents 包与 delegate 工具）：同一 Run 连交两次结果不终态、continuation 被 ack、stage 后 kill 重启只提交一次。T4（围栏）后移到 spike 之后。T2 因结果表是 stage/finalize 的直接依赖，保留在 spike 之前。 | 任务顺序 |
| 14 | ac2-not-decidable | P1 | 采纳 nonce 口径：子 Agent 的 `instructions`（来自委派配置模板，主 Agent **看不到**）要求"在结论末尾附验证码 `<NONCE>`"，NONCE 由测试随机生成；AC2 = 主 Agent 最终 `public_output` 含 NONCE，且 NONCE 不出现在主 Agent 的 instructions/输入/工具描述里。mock 与 real_provider 共用同一口径。 | T8/T9/T10 |
| 15 | child-two-turns-has-no-driver | P1 | 采纳：**一次委派 = 子 Agent 一个 AgentTurn**（Turn 内可多次模型/工具循环）；"同 Agent 两轮"的断言只留在主 Agent（turn-1 委派，重启后 turn-2）。T9 脚本改为 4 次调用。 | T8/T9 |
| 16 | simpler-driver-kind-instead-of-start-mode | P1 | 采纳（见 #2/#3）。AC7 第二子条款改写为："`build_agent_runtime` 未注册 `base_agent` driver 时 `build_runtime` 拒绝（root profile driver_kind 未注册），绝不落回 react"。**AC 措辞变更由主编排者裁决并记录**。 | T4、acceptance |
| 17 | wait-idle-returns-on-first-drive | P2 | 采纳（随 #1）：不用 `wait_idle` 作完成信号。 | T8 |
| 18 | uow-conflict-is-swallowed-not-raised | P2 | 采纳：对不该出现的 continuation kind 返回 `DriverResult(FAILED, reason="base_agent_unexpected_continuation")`，不抛 `UnitOfWorkConflict`；断言下沉到端到端。 | T6 |
| 19 | addopts-marker-override | P2 | 采纳：不改 addopts；conftest 增 `--run-real-provider` 开关，未开启时 `real_provider` 标记的用例 skip；markers 表只注册 marker。 | T10 |
| 20 | schema-all-inconsistent | P2 | 采纳：T2 顺带把 `accepted_descriptor_rows`、三个 legacy descriptor 补进 `__all__`。 | T2 |
| 21 | four-tables-may-be-two | P2 | 部分采纳：保留 bindings / turns / turn_results 三张；`base_agent_delegations_v1` 缩为最小映射 `(delegation_id PK, parent_agent_id, parent_turn_id, ordinal, child_agent_id UNIQUE, ticket_id UNIQUE, state)`，其余事实从既有账本推导。仍是 4 张表。 | T2 |

## 对 acceptance 的改动摘要

- 主要矛盾 / 矛盾的主要方面：按 #12 改写。
- AC2：按 #14 改为 nonce 口径。
- AC4：主 Agent 两轮；子 Agent 不再要求两轮。
- AC5：失败域措辞收窄为"stage 之后任意点"（#9）。
- AC7：第二子条款按 #16 改写；覆盖 root 与 child（#11）。
- AC9：证据源改为子 Agent 的 `base_agent_turn_results_v1`（#1）。
- 新增非功能项：创建不驱动模型（#6）、提交后重新调度（#5）。

## 处置状态

全部 21 条：采纳 19、部分采纳 1（#21）、收窄声明 1（#9）。修订版 plan/acceptance 完成后做一轮 closure 复核。
