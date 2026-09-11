# Agent 编排框架（第 2–9 步）：建设纲要

- 日期：2026-09-11
- 依据：Host 仓库 `plans/taskSys2/agent-orchestrator-incremental-build-plan-phase2-zh-CN.md`（ORCH-BUILD-v1.0）、`agent-orchestration-layer-complete-design.md`（原文 31 章）、`agent-orchestration-theory/`（术语定义）
- 代码基线：本仓库 main `dffd13c`（simple-harness-sdk 0.8.0，BaseAgent 第一阶段 S1–S5 已 SHIPPED）
- 编排方式：不用 plan-test skill；每步一个目录 `stepNN/{plan,acceptance,journal}.md`，测试先行、每任务提交、全量回归红集 ⊆ 基线、独立 review、真实模型证据、中文记录。

## 0. 术语纪律

写任何代码/文档前，术语按 `agent-orchestration-theory/` 的定义使用；定义与原文不一致时以原文（31 章设计）为准并在 plan 里注明。本纲要用到的核心定义（摘自理论合订版）：

| 术语 | 定义（出处） | 本框架的落点 |
|---|---|---|
| Mission | 一次完整运行的"任务章程"：目标、成功条件、预算、允许工具、风险等级、停止条件（原文 §5） | `contracts.Mission`，编排库 `missions` 表 |
| Task / Task Contract | 可检查的契约：goal、root_goal/rationale、dependencies、success_criteria、verification_policy、allowed_tools、budget、version（原文 §6.3；理论 13-6） | `contracts.Task` |
| Attempt | 对 Task 的一次具体尝试；同一 Task 可多个 Attempt，一个 Attempt 只能有一个执行者（原文 §12.1、§17.2；理论 06-9、10-5） | `contracts.Attempt`，与 BaseAgent 的映射 attempt_id → agent_id + turn_id |
| Result Envelope | Agent 只能提交的候选结果：outcome、summary、claims、evidence、artifacts、proposed_tasks、used_knowledge、risks、cost（原文 §13、§26.4） | `contracts.ResultEnvelope`，由 `AgentTurnResult.public_output` 严格解析 |
| Claim / Verified Knowledge | 未验证结论只能叫 Claim；状态 PROPOSED→UNDER_REVIEW→SUPPORTED/VERIFIED/REJECTED/DISPUTED，VERIFIED 可 SUPERSEDED；只有 VERIFIED 是正式知识（原文 §14.3、§25.3；理论 04-9） | 第 2 步只落 Claim 状态与验证结果，Blackboard 在第 4 步 |
| Proposal / Commit | Agent 提 Proposal，系统检查后由唯一的 Commit Service 写正式状态（原文 §15；理论 10-14） | `orchestrator.commit_service` 是编排库唯一逻辑写入者 |
| Event / Current State | Event 是不可变事实（录像帧），State 是当前照片；两者都持久（原文 §16；理论 09） | `events` 表 + 各 current state 表，同一事务写 |
| Durable Execution | 崩溃后从最近可靠步骤恢复：COMPLETED 不重跑、PENDING 重入队、RUNNING 且租约过期→LOST→新 Attempt、SUBMITTED 未验证→重进验证队列（原文 §16.4；理论 09-12） | `Orchestrator.recover()` |
| Lease / Heartbeat | 任务租给执行者并有有效期，心跳续租；心跳消失→LOST（原文 §17.6；理论 09-8） | `scheduling.leases`；心跳来自执行者实际存活检查（BaseAgent turn 快照），不是调度器自己续 |
| Reserve / Settle | Attempt 启动前 Reserve 最大预算，结束后按实际 Cost 结算并释放（原文 §18.3） | `governance.budgets`；预算是额度层，SDK 调用账本是费用事实层 |
| Verifier（分层） | 格式→规则→独立 Critic→测试/实验→形式化→人工；按 Task 的 verification_policy 选层（原文 §14.1；理论 13-7） | `verification.verifier_router` |
| Frontier / Allocator / Scheduler | Frontier=依赖满足、未完成、有价值的任务集合；Allocator 决定给多少资源；Scheduler 决定谁何时在哪里运行（原文 §8；理论 05） | 第 2 步只有单 Task，Frontier 退化为该 Task；Allocator 只做资格检查；Scheduler 负责领取/租约/派发 |
| Workspace / Artifact | 每个 Attempt 独立可写工作区；非文本产物以 Artifact 管理（id、type、content_hash、version、produced_by、verification_status）（原文 §20） | `artifacts.workspace/artifact_store/versioning` |
| Tool Gateway | Agent 不直接访问真实系统；工具调用经身份/权限/schema/风险/预算检查后执行并审计（原文 §21.1） | 第 2 步：工具由编排层注入 BaseAgent，只暴露 Task `allowed_tools` 的交集，工作区受限 |

## 1. 步骤总表（沿用 ORCH-BUILD-v1.0 §3）

| 步 | 原文阶段 | 本步完整功能 | 状态 |
|---|---|---|---|
| 1 | 底座 | BaseAgent + 独立短期记忆（0.8.0） | 已完成 |
| 2 | 一 | 单 Task Mission 的可靠验收闭环 | **SHIPPED**（`step02/`，SDK 0.9.0） |
| 3 | 一 | Planner 自动拆解并并行执行静态 DAG | **SHIPPED**（`step03/`，SDK 0.9.1 / agent_orchestrator 0.3.0） |
| 4 | 二 | 团队共享知识、冲突仲裁、综合 | **SHIPPED**（`step04/`，SDK 0.9.2 / agent_orchestrator 0.4.0） |
| 5 | 二 | 根据 Worker 返回动态修改 Task DAG | **SHIPPED**（`step05/`，SDK 0.9.3 / agent_orchestrator 0.5.0，wheel 自 `399d0e7`；真实 deepseek-flash 运行 3 改图 v1→v2 后完成） |
| 6 | 三 | 多 Mission、多模型、背压、隔离 | **SHIPPED**（`step06/`，SDK 0.9.4 / agent_orchestrator 0.6.0，wheel 自 `a2ce656`；真实 deepseek-flash 两执行池运行 1、2 都完成） |
| 7 | 三 | Human-in-the-loop 与受控真实操作 | **SHIPPED**（`step07/`，SDK 0.9.5 / agent_orchestrator 0.7.0，wheel 自 `418a6d7`；真实 deepseek-flash 运行 1、2 都完成：候选 → L2 审批 → 交接一次 → 回执核对） |
| 8 | 四 | 归因、Replay、策略对照评测 | **SHIPPED**（`step08/`，SDK 0.9.6 / agent_orchestrator 0.8.0，wheel 自 `a094f46`；代码评审两轮全部处置；真实 deepseek-flash 评测运行 1、2 都完成，小样本比较如实写"证据不足"） |
| 9 | 四 | 从历史学习并受控晋级 | **SHIPPED**（`step09/`，SDK 0.9.7 / agent_orchestrator 0.9.0，wheel 自 `WHEEL_COMMIT_PLACEHOLDER`；plan 评审 3 P0 / 11 P1 / 10 P2 与代码评审 0 P0 / 2 P1 / 11 P2 全部处置；真实 deepseek-flash 门槛评测如实给出"样本不足"） |

进入下一步的门槛：本步验收场景全部 PASS + 累计回归 + 安装 wheel 后验证 + 真实模型演示记录 + 独立 review 处置。

## 2. 跨步骤固定纪律

1. 新包 `src/agent_orchestrator/`（模块化单体，目录按原文 §27），与 `simple_harness` 同一 wheel 发布；`agent_orchestrator` → `simple_harness.agents` 公共 API 单向依赖，不 import SDK 私有模块（`execution.sqlite.uow` 内部方法除非公共门面），不改 BaseAgent 类。
2. 编排状态唯一逻辑写入者是 Commit Service；BaseAgent/AgentTurn/Provider invocation/Tool effect 由 SDK Runtime/UoW 唯一写入；两库之间没有原子事务，靠 dispatch intent + 稳定身份（creation_key/input_id）+ SDK 幂等 + 回执核对 + 结果接收去重。
3. 编排层用独立 `orchestrator.db`；SDK 继续独占 `execution.db`（同目录）。
4. 预算 Reserve 与 SDK 费用账本分两层；每条 `usage_ref` 至多导入一次；本地 fixture 显式 `unpriced`，付费模型必须注入价格表，未知费用不能写零。
5. 第 2–6 步只允许 L0 只读 / L1 隔离沙箱动作；真实修改到第 7 步。
6. 回归口径：`.venv/bin/python -m pytest -q -p no:cacheprovider tests/orchestrator`（累计）+ 原有 SDK 回归（`plans/2026-09-10-base-agent-phase1/baseline-known-failures.txt` 基线不变）。
7. 真实模型测试复用 `--run-real-provider` 与 `tests/agents/real_provider_config.py`（SH_BASEURL/SH_APIKEY/SH_MODEL 或 Host `.env`），报告与 fixture 测试分开；凭证不进证据目录。
8. 术语纪律（§0）；术语用错视为缺陷。

交接说明：`HANDOFF.md`（当前状态、第 9 步起步方式、不变量、陷阱、真实模型与 wheel 重建方法、遗留）。
