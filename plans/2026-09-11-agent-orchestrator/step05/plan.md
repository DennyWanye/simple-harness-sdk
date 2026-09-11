# 第 5 步：根据 Worker 返回动态修改 Task DAG · 实施计划

- 日期：2026-09-11 · 基线 SDK main `96abf25`（0.9.2 / agent_orchestrator 0.4.0，第 4 步 SHIPPED）
- 原文依据：§6.2、§7–9、§13、§15、§17、§18.6、§19、§25、§28 第二阶段、§29.2–29.3；ORCH-BUILD-v1.0 §7（7.1–7.4）、§12、§13
- 术语：`../program.md` §0；本步新引入/重申的定义（先查 `agent-orchestration-theory/`）：
  - **Dynamic Task Graph**：科研型任务的图在运行中生长（Problem → A/B/C → A1/A2…）；固定 DAG 是业务流程，动态图是本步对象（原文 §6.2；理论 02-5）。
  - **"先欠一个子定理"**：Agent 交出条件式方案并提出新任务"证明 Lemma A"，系统记录依赖并创建新任务；一个 Agent 不必独自钻到底（理论 02-7；原文 §12.3 "新的子任务"）。
  - **Manager**：带队走地图——观察哪里有突破、哪里卡住、哪里重复失败，提出减少/深挖/加 Critic/拆小的调整；**Planner** 画地图（拆解、路线、依赖）；小系统里 Manager 与 Search Controller 合并（原文 §7.1–7.3；理论 07-1/07-2）。
  - **Proposal / Commit**：Agent（含 Manager）只能提出新增 Task、提高优先级、申请预算、停止路线；只有 Commit Service 改正式 Task DAG（原文 §15；理论 10-14）。
  - **Frontier**：依赖已满足、尚未完成、对主目标仍有价值、当前可分配的任务集合（原文 §8.1；理论 02-8）。
  - **Allocator / Scheduler**：Allocator 决定给多少（哪些任务、多少 Agent、什么角色/预算）；Scheduler 决定何时/在哪里；§29.3 起始公式只是起点，参数经评测调整（原文 §8.2–8.3、§29.3；理论 05-3/05-8）。
  - **Dynamic Scheduling / Starvation**：新知识或验证失败改变优先级；等待越久优先级越高（原文 §19.4；理论 05-7/05-10）。
  - **Retry 不是重复同一 Prompt**：换策略/角色/模型/上下文/粒度（理论 06-5）；**停滞检测**：连续无进展 → 换模型/角色、拆小、派 Failure Analyst、暂停或终止（原文 §19.2）；**Goal Drift**：每个 Task 必须说明与根目标的关系、解锁什么、为何值得（原文 §19.5）。
  - **角色 = 搜索偏置**：Explorer / Exploiter / Critic / Simplifier / Connector / Failure Analyst / Synthesizer / Verifier（原文 §9.2；理论 03-6）。
  - **Deadlock / 循环依赖**：每次加边必须做环检测（原文 §19.3；30-06）。

## 1. 主要矛盾

执行证据（blocked / no_progress / failure / proposed_subtasks、反复验证失败）必须能改变**正式**计划，而正式计划的每次改变又必须是**有版本、有依据、可拒绝、可重放**的 Commit——既不能让 Worker 直接改图，也不能让 Manager 的一次"重画"抹掉已有效完成的工作、重复扣费或绕过预算/深度限制。矛盾的主要方面是**"图的变更是带基版本的事务性 Proposal/Commit，而 §25 状态机不加回边"**：能改的只有尚未执行的任务（细化/加前置/改优先级/暂停），已在执行的任务要改合同就按 ACTIVE→CANCELLED 收敛旧执行并创建引用旧任务的**新实体**，已完成任务只被引用、永不重跑。次要方面：Manager 决策的触发是可去重的一次性工作、无进展有限次数、§29.3 优先级公式与老化。

## 2. 范围

演示（ORCH §7.4）：原计划 A 分析输入 → B 实现 → C 验证，A → D 独立文档检查。B 返回 `blocked` 并 `proposed_tasks=[确认格式]`；Manager 提出：新增 E（确认格式）、B2（实现，依赖 A+E，替代 B）、C 改依赖 B2；Commit 后 v1→v2，A/D 成果保留、B 旧 Attempt 收敛；Mission 用 v2 完成。事件时间线与图版本可回看。

做：图变更 Proposal（新增任务、替代任务、改 BLOCKED 任务依赖、改优先级、暂停/取消路线、换角色）的校验与事务性 Commit（基版本 CAS、幂等、环/深度/数量/预算/去重/目标漂移检查）；非 candidate 结果与反复失败触发**可去重**的 Manager 决策（Manager 是 BaseAgent 角色，输出 `<graph_change_proposal>`，程序做资格检查与硬限制）；无进展有限次数后换角色或停止；§29.3 优先级公式 + 等待老化（版本化参数）；§29.2 角色模板注册（Explorer/Exploiter/Simplifier/Connector/Failure Analyst + Manager）；被替代 Worker 迟到提交只记历史；Mission 判定与终结任务对 CANCELLED（被替代）任务的处理；CLI `demo --scenario dynamic-dag`。

不做：多 Mission 配额/背压（第 6 步）、模型切换（`context.model_hint` 只登记，第 6 步路由）、MCTS/Beam（原文 §7.3 可扩展项）、多层 Manager、人工介入（第 7 步）。

## 3. 设计决定

| # | 决定 | 依据 |
|---|---|---|
| D5-1 | **图变更提案**（`graph/changes.py`）：`TaskGraphChange{base_graph_version, basis:{result_id?, attempt_id?, task_id?, trigger}, rationale, operations:[…]}`，操作词汇由系统定义：`add_task{key, goal, rationale, dependencies:[已有 id 或本提案 key], success_criteria, verification_policy, allowed_tools, budget, priority, outputs, parent_task_ids, role?}`、`supersede_task{task_id, replacement_key}`（旧任务 READY/ACTIVE→CANCELLED，替代者是本提案里的 add_task；旧任务的依赖者中 BLOCKED 者在位改指向替代者）、`retarget_dependencies{task_id, dependencies}`（只对 BLOCKED 任务）、`set_priority{task_id, priority}`、`pause_task{task_id, reason}` / `resume_task`（数据标记，前沿过滤，不改状态）、`cancel_task{task_id, reason}`（READY/ACTIVE→CANCELLED）、`set_role{task_id, role}`（下一个 Attempt 用该角色模板）。Manager 只能提这些操作，Worker 只能在 Result Envelope 里 `proposed_tasks` | 原文 §15、§7.2；ORCH §7.3 |
| D5-2 | **事务性 Commit**（`commit_graph_change`）：`base_graph_version` 必须等于当前 `final_report.graph_version`，否则 `GraphChangeRejected(stale_base)`；对同一提案（规范化 JSON 哈希 + base 版本）的重复投递返回同一回执（S5-07）；整份提案原子校验：合并后的图无环（Kahn）、无缺失/自依赖、去重（规范化 goal+依赖+准则全同 → 拒绝；疑似 → 回执 `warnings` 里的合并建议）、深度 ≤ `max_graph_depth`（默认 6）、任务数 ≤ `MAX_TASKS`、每个来源 Attempt 的 add_task 累计 ≤ `max_proposals_per_agent`（默认 3）、**预算池**：Σ（非 CANCELLED 任务的 max_tokens）+ Σ（CANCELLED 任务已结算 tokens）+ 系统预留 ≤ Mission max_tokens（子任务预算来自父任务/Mission 池，被替代任务未用的额度回到池里）、新任务工具 ⊆ Mission、`verification_policy` 已部署；目标漂移：新任务必须 rationale 非空且（被某任务依赖 ∨ 替代某任务 ∨ 是终结任务的新前置）否则 `goal_drift`；任一失败整份拒绝，事件 `TaskGraphChangeRejected(reason, detail)`，正式图不变 | 原文 §15、§19.3、§19.5、§18.2；30-06/30-13；S5-03/04/07/08 |
| D5-3 | **§25 不加回边**：新任务先 BLOCKED（有依赖）/READY（无依赖）；被替代任务 READY→CANCELLED 或 ACTIVE→CANCELLED（VERIFYING 先→ACTIVE 再→CANCELLED，同 `stop_task` 的两条合法边）；其在途 Attempt CANCELLED（迟到结果只记历史 `ResultRejected(superseded)`，费用照常导入结算，S5-06）；旧任务 COMPLETED/FAILED/CANCELLED 终态不改；BLOCKED 任务的 `dependency_ids` 允许**在位**改写（数据变更、版本 +1、状态不变——"尚未执行的任务允许在其合法状态下细化/增加前置"）；READY/ACTIVE 任务要加前置只能走 `supersede_task`。`graph_version` +1，事件 `TaskGraphChanged{from, to, basis, operations, affected_tasks, receipt}`，新任务 `TaskCommitted(source=change)`，被替代任务 `TaskSuperseded{by}`；新任务 `context.supersedes_task` / `parent_task_ids` 记录血缘 | ORCH §7.3；原文 §25.1 |
| D5-4 | **Mission 判定与前沿对 CANCELLED 的处理**：`judge_mission` 要求全部**非 CANCELLED** 任务 COMPLETED；整合副本只应用 COMPLETED 任务的产物；`terminal_task` 忽略 CANCELLED；`_decide` 不再因存在 CANCELLED 任务而停（只看 Mission 状态与 FAILED）；`merge_accepted` 的祖先集合跳过 CANCELLED（被替代任务的产物不进入下游） | ORCH §7.3 "候选路线之间是否需要全部完成由 Mission 成功条件表达" |
| D5-5 | **非 candidate 结果不进验证**：`_collect_attempt` 对 `blocked/failure/no_progress/proposed_subtasks` 调 `record_outcome_result`：结果存为历史（verification_state=REJECTED, verdict=`outcome:<x>`），Attempt→RETRY_WAIT（failure.reason=`outcome_<x>`，带 summary/proposed_tasks/risks），Task 回 ACTIVE，事件 `ResultSubmitted` + `OutcomeRecorded`；随后创建**管理决策工作**（D5-6）。candidate 结果照旧验证；PASS 且带 `proposed_tasks` 的结果在 accept 后也触发管理决策（原 Worker 不能直接改图，S5-01） | 原文 §12.3、§13 |
| D5-6 | **管理决策是可去重的一次性工作**（`kind="manager"` 的 service intent，subject = `<mission>:manager:<trigger>`，trigger = 触发结果 id 或 `stall:<task>:<n>`）：同一触发只建一次；Manager 是 BaseAgent 角色（模板 `manager-v1`，包 = 触发结果摘要/outcome/proposed_tasks/risks、Verifier 反馈、受影响子图（任务 + 依赖者 + 兄弟 + 状态/尝试数）、当前 graph_version、限制（深度/每 Agent 建议数/剩余预算/剩余尝试）、已验证知识摘要、允许的操作词汇），输出 `<graph_change_proposal>`（`operations` 可为空 = 继续重试）；不在每个 token/工具心跳后重规划——只在 D5-5 的触发、`VerificationFailed` 累计 ≥ `manager_after_failures`（默认 2）、`no_progress` 时触发。Manager 调用记 Mission 账户 | ORCH §7.2 `planning/manager.py`、`event_handler.py` 行 |
| D5-7 | **无进展有限次数**（S5-02）：同一 Task 累计 `no_progress/failure` 结果 ≥ `no_progress_limit`（默认 2）后，Manager 提案若仍不改变角色/方法/结构（空操作或只 set_priority）→ 系统拒绝并 `stop_task(NO_PROGRESS)`（`MissionStopReason.NO_PROGRESS` 新增）；Manager 换角色（`set_role`：explorer/exploiter/simplifier/failure_analyst）或拆小（add_task + supersede）则继续，但每个 Task 的替代链长度 ≤ `max_supersede_chain`（默认 2）→ 不无限分裂 | 原文 §19.2；30-17 |
| D5-8 | **Allocator §29.3**（`scheduling/allocator.py`，`ALLOCATOR_VERSION="allocator-v1"`，权重原样：0.30/0.20/0.15/0.15/0.10/−0.05/−0.05）：资格检查在前（非 paused、依赖 COMPLETED、任务账户有剩余、并发上限、候选上限），再按公式排序。输入尺度是实施约定（§6.1 登记）：`mission_importance` = task.priority 归一到 [0,1]；`unlock_value` = 传递依赖者数 / 任务总数；`progress_signal` = 1 − 失败结果数/尝试数（无尝试 = 0.5）；`uncertainty` = 0.5^尝试数；`waiting_age` = min(1, (now − ready_at)/`aging_window_seconds`)（默认 300 s）；`estimated_cost` = task.max_tokens / Mission max_tokens；`duplication_score` = 疑似重复数 / 任务总数。`Task.ready_at` 在 READY 化时记录。S5-09：低优先级任务在有资源且无安全阻塞时因 `waiting_age` 上升获得执行 | 原文 §29.3、§19.4；理论 05-10 |
| D5-9 | **角色模板 §29.2**：WORKER 的搜索偏置变体 `EXPLORER`（找新路线/不同假设）、`EXPLOITER`（把当前最好路线做深）、`SIMPLIFIER`（先做特例/简化版）、`CONNECTOR`（连接分支知识）、`FAILURE_ANALYST`（分析重复失败的共同原因，产出 `analysis/…` 与建议子任务）；输出合同与工具与 Worker 相同；Attempt 用 `task.context.role`（缺省 worker）；`MANAGER` 模板无工具；权限由工具上限落实（角色不扩权）。比例起点 §29.2 只登记为配置常量 `ROLE_MIX_START`，本步不做配比调度（第 6 步） | 原文 §9.2、§29.2 |
| D5-10 | **两个 Manager 基于同一旧版本**（S5-03）：CAS 拒绝后，若被拒提案的操作不触及已生效变更的 `affected_tasks`，系统在新基版本上**自动重基**再校验一次（回执记 `rebased_from`）；触及则拒绝并重新触发一次管理决策（新 trigger 后缀 `:retry-1`，至多 1 次）；已提交的修改不丢失 | 原文 §17.3；理论 10-10 |
| D5-11 | 预算：新任务账户开在 Mission 账户下（`inherit_limits`），Manager/Critic/验证费用记 Mission 账户；被替代任务的预留结算后其剩余额度回到池（D5-2 的池公式）；不靠新 Task 清空消耗（费用事实不变） | ORCH §12.2、§7.2 `governance/budgets.py` 行 |
| D5-12 | 恢复：manager intent 与 planner intent 同路径（PENDING/CLAIMED 重派、SUBMITTED 采集）；`heal_mission` 对 CANCELLED 任务下遗留 Attempt 收口（CANCELLED）；`graph_version` 只在 Mission 记录上 CAS | 原文 §16.4 |
| D5-13 | CLI `demo --scenario dynamic-dag`（fixtures/env）；证据目录加 `graph_history.json`（每个 graph_version 的任务快照与变更依据）；报告含 graph_version、变更列表、被替代任务与新任务 | ORCH §7.4、§14.3 |
| D5-14 | 版本：simple_harness 0.9.3、agent_orchestrator 0.5.0；schema v3（`tasks` 加 `graph_version_added` 列？——不加列：任务 JSON 记 `context.graph_version`；新表 `graph_changes`（change_id, mission_id, from_version, to_version, basis, operations, receipt, created_at）） | ORCH §14.1、§12.6 |

## 4. 任务

| 切片 | 内容 | 决定性测试 |
|---|---|---|
| A 图变更 | D5-1/2/3/4/11/14：`graph/changes.py`（模型、校验、合并图）、`commit_graph_change`（CAS、幂等、事件、账户、替代/在位改写/暂停/取消/角色）、`graph_changes` 表与 schema v3、判定/前沿对 CANCELLED 的处理 | `test_graph_changes.py`（合法变更 v1→v2；环/深度/数量/预算/重复/漂移拒绝；幂等；stale base；替代 ACTIVE 任务；BLOCKED 在位改写；READY 加前置须替代） |
| B 触发与 Manager | D5-5/6/7/9/10/12：非 candidate 结果路由、manager intent、`MANAGER` 模板与 `<graph_change_proposal>` 解析、无进展限制、角色模板、重基 | `test_manager_decisions.py`（S5-01/02/03/07/08） |
| C Allocator | D5-8：公式、老化、资格检查、`ready_at` | `test_allocator_priority.py`（S5-09 + 公式确定性） |
| D 闭环与演示 | D5-13：fixtures（recorder 项目、Manager 脚本）、`test_dynamic_dag_closure.py`（S5-05/06 + §7.4 全流程）、CLI、真实模型 opt-in（deepseek-flash）、`graph_history.json` | 同左 |
| E 收尾 | review、wheel 0.9.3、CHANGELOG、testcase、program.md、journal、推送、清理 | — |

## 5. 风险

- **Manager 质量**：真实模型的提案可能反复被拒（漂移/预算）；拒绝反馈进入下一次 Manager 包（同 D3-2'），且总次数受 `max_manager_rounds`（默认 4/Mission）限制 → 超限 `stop_task(MANAGEMENT_EXHAUSTED)`。
- **替代链与预算**：替代任务反复申请预算把池耗尽 → 正常 `budget_exhausted` 路径。
- **真实模型**：flash 上 `tool_parse` 失败频繁（L4-4），影响耗时不影响语义。

## 6. 独立 review 后的修订（2026-09-11；裁决表见 journal §1）

| # | 修订 |
|---|---|
| D5-3' | **拓扑序不再等于 ordinal（R1）**：`artifacts/versioning.ancestors/merge_accepted` 改为按依赖边做 Kahn 拓扑序（ordinal 只做平局），BLOCKED 任务在位改依赖（`retarget_dependencies`）后 `ordinal` 保持提交次序而合并顺序仍正确；D4-7' 的"冲突任务是末尾叶子"不受影响（它仍在拓扑序末尾）。§7.4 的"C2"在本实现里是"C 在位改依赖到 B2"——这是纲要 §7.3 允许的"尚未执行任务的细化"，图历史（`graph_changes`、`TaskDependenciesRewritten`）可回看 |
| D5-2' | **预算池按已结算 + 在途预留计入（R3）**：被取代/取消任务的额度只在其预留结算后回到池里，不为新任务清空旧费用（ORCH §12.2）；变更路径同样做 D3-7' 的兄弟 `outputs` 冲突检查（R6）；初始图也受 `MAX_GRAPH_DEPTH=6` 限制（R19） |
| D5-4' | **暂停路线有终局（R4）**：`pause_task` 只对不会阻塞判定的路线有意义——判定与"全部完成"检查排除 paused 的 READY/BLOCKED 任务；Mission 判定通过时 paused 的 READY 任务按 §25.1 READY→CANCELLED 记 `not_needed_paused`，paused 的 BLOCKED 任务随 Mission 终止（同 D3-13'）。"停止一条路线"（R5）：`cancel_task` 只能用于没有未完成依赖者的 READY/ACTIVE 任务（§25.1 无 BLOCKED→CANCELLED 边，有依赖者时整份提案被拒 `missing_dependency`）；有 BLOCKED 依赖者的路线用 `pause_task` |
| D5-10' | **任何拒绝都反馈一次（R7）**：Manager 提案被拒（含 CAS 重叠、深度、预算、漂移）→ 记 `ManagementDecided(rejected)` 并用 `<trigger>:retry-1` 再请求一次，包内 `rejections` 带原因；再次被拒 → 按 D5-7 的无进展规则处置。`_open_conflict` 同样写 `graph_changes` 行（R8），`graph_version` 的每次递增都有账 |
| D5-8' | **§29.3 输入尺度（R9）**：`mission_importance` = task.priority / max(live priority)（冲突任务 priority 10 使其为 1.0，其余按比例），且资格检查阶段 `kind=conflict` 的任务优先于普通任务；其余尺度见 D5-8 |
| D5-7' | 无进展计数（R15）= `no_progress` / `failure` 结果 + 验证失败；`blocked` / `proposed_subtasks` 是"欠一个子任务"的进展信号不计入，但 Manager 对同一 trigger 的空提案仍受 `no_progress_limit` 约束；`max_manager_rounds`（默认 4）超限 → `stop_task(MANAGEMENT_EXHAUSTED)`（R14，进决定表与验收附加门槛） |
| D5-15 | **关闭开关（R13）**：`OrchestratorConfig.dynamic_graph=True`；False 时非 candidate 结果只记历史并按旧路径重试，不请求 Manager，`commit_graph_change` 仍可由 API 调用 |

### 6.1 本步实施约定（非原文原句；按 ORCH §13 登记）

| 项 | 约定 | 原文位置 |
|---|---|---|
| 操作词汇（add_task / supersede_task / retarget_dependencies / set_priority / pause_task / resume_task / cancel_task / set_role） | 原文只说"新增 Task、提高优先级、申请预算、建议停止路线"；本表是实现定义的封闭词汇 | §15 |
| Manager 触发时机 | 非 candidate 结果、`VerificationFailed` 累计 ≥ `manager_after_failures`、PASS 结果带 `proposed_tasks`；不在 token/心跳后重规划 | §7.2、ORCH §7.2 |
| 无进展计数、`no_progress_limit=2`、`max_supersede_chain=2`、`max_manager_rounds=4`、`max_graph_depth=6`、`max_proposals_per_agent=3` | 原文 §19.2 只给"连续多轮"；数值为本版本默认 | §19.2；30-17 |
| §29.3 输入尺度与老化窗口 `aging_window_seconds=300` | 原文只给权重；尺度是实施约定，`ALLOCATOR_VERSION` 随之变更 | §29.3 |
| 非 candidate 结果的 claims | 记为 PROPOSED→UNDER_REVIEW→REJECTED（历史），不进知识 | §13 |
| `stop_task` 是 Mission 级停止（R17） | 分支级停滞处置 = 换角色/拆小/暂停；`NO_PROGRESS` / `MANAGEMENT_EXHAUSTED` 是 Mission 级停止原因 | §19 |
| 被替代任务的在途 SDK turn（R18） | 编排层 `cancel_turn` 协作取消、`gateway.unbind` 立即撤销工具资格；迟到结果只记历史（S5-06 观察的正是这条） | ORCH §12.1 |
| 演示可观察性（R23） | 证据 `graph_history.json` 每个版本含任务快照、变更依据与被取代任务的已登记产物 id | ORCH §7.4 |
