# 第 3 步：自动拆解并并行执行静态 Task DAG · 实施计划

- 日期：2026-09-11 · 基线 SDK main `bd308dd`（0.9.0，第 2 步 SHIPPED）
- 原文依据：§6–10、§17–20、§24–26、§28 第一阶段；ORCH-BUILD-v1.0 §5、§12、§13
- 术语：`../program.md` §0；本步新引入的定义：**Frontier** = 依赖已满足、未完成、当前可分配的 Task 集合（原文 §8.1；理论 02-8）；**Allocator** 决定给谁多少资源、**Scheduler** 决定何时/在哪里运行（原文 §8.2/8.3；理论 05）；**Graph Manager** 检查并 Commit Task DAG（原文 §24 第 3 步）；**一个 Task 可有多个 Attempt，一个 Attempt 只有一个执行者**（原文 §17.1/17.2；理论 10-5）。

## 1. 主要矛盾

Planner 一次给出的静态 DAG 必须**作为一个整体**被检查与 Commit（无环、无缺失节点、无重复、预算不超父级），此后调度只能在**依赖已通过验收**的前沿上并行推进；而并行意味着多个 Attempt、多个执行者同时在两库上写：同一 Attempt 只能有一个 owner（CAS 领取），同一 Task 的多个候选只能接受一个（先到的合法 PASS 胜出、其余取消并记账），下游任务的输入必须固定在上游**已接受产物的 hash** 上，Mission 只有在整体成果再次验收后才算成功（S3-06）。矛盾的主要方面是**"图的原子提交 + 前沿上的多执行者互斥"**。

## 2. 范围

用户只给根目标（示例 §5.3：A 合同 → B/C 两个功能并行 → D 集成测试 → E 交付说明 + Mission 级验收）。Planner（BaseAgent）提出带依赖的初始 DAG → Graph Manager 校验 → 一次 Commit → Frontier 上按并发上限并行创建 Attempt → 每个 Task 走第 2 步的验收闭环 → TaskCompleted 解锁依赖者（BLOCKED→READY）→ 下游 Attempt 的工作区注入上游已接受产物（hash 固定）→ 全部完成后对 Mission 根成功条件做独立判定。

不做：执行期改图（Worker 的 `proposed_tasks` 只保存展示）、Blackboard、多 Mission 配额、学习型 Allocator。

## 3. 设计决定

| # | 决定 | 依据 |
|---|---|---|
| D3-1 | Planner 输出 `<task_graph_proposal>`：`{"tasks":[{"key","goal","rationale","dependencies":[key…],"success_criteria","verification_policy","allowed_tools","budget","priority"}]}`；`key` 是提案内的临时标识，Commit 时按拓扑序分配 `task_id = <mission>:task-<n>`，`dependency_ids` 换成正式 id | 原文 §6.3、§26.2 |
| D3-2 | `graph/task_graph.py` + `dependency_checker.py` + `deduplicator.py`：整份提案原子校验——缺失/自依赖/环（Kahn）/重复（`key` 重复、规范化 goal 重复）/每个 Task 预算 ≤ Mission 预算且 **Σ max_tokens ≤ Mission max_tokens**（有限维度逐项）/allowed_tools ⊆ Mission/≥1 个无依赖根/≥1 个无依赖者的终结任务；任一失败整份拒绝（`GraphRejected`，事件 `TaskGraphRejected`，正式图不变，Planner 可重提 ≤ MAX_PLANNING_ATTEMPTS） | 原文 §19.3、§24 第 3 步；S3-02/S3-08 |
| D3-3 | 一次 Commit 写全部 Task（根 READY、其余 BLOCKED）+ `graph_version=1` 的 receipt；`TaskCommitted` 每任务一条 + `TaskGraphCommitted` 一条；Mission PLANNING→ACTIVE | 原文 §15、§17.3 |
| D3-4 | Frontier = READY 且无未完成前置的 Task；Allocator（`scheduling/allocator.py`）：候选按 `priority` 降序、`ordinal` 升序；受 Mission `max_concurrency`（默认 = config.max_concurrency）限制同时在途 Attempt 数；每 Task 同时候选数 `candidates_per_task`（默认 1；S3-05 场景配 2） | 原文 §8；理论 05 |
| D3-5 | 多候选：同一 Task 的多个 Attempt 各自独立执行者/工作区；第一个通过验证的候选被接受，其余 RUNNING/SUBMITTED 候选 → `SUPERSEDED`（协作式 cancel_turn、导入 usage、结算/保留预留、事件 `AttemptSuperseded`）；候选间不共享工作区 | 原文 §17.2、§8.3 |
| D3-6 | 依赖解锁：`accept_result` 之后由 Commit `unblock_dependents(task)`：所有前置 COMPLETED 的 BLOCKED 任务 → READY（事件 `TaskUnblocked`） | 原文 §25.1 |
| D3-7 | 产物传递（`artifacts/versioning.py`）：为下游 Attempt 建工作区时注入每个直接前置的 `accepted_artifacts` 文件（路径→内容，来源 hash 冻结进 intent config `inputs`），受保护种子文件规则不变；Context 包 §10 第 3 项 = 直接依赖的 `{task_id, goal, accepted_artifacts:[{path,hash}]}`；同名冲突（两个前置产出同一路径）→ 拒绝创建 Attempt 并停止 Task（`artifact_conflict`） | 原文 §20.3；ORCH §5.2 |
| D3-8 | `artifact.version` 按 (attempt, path) 递增（工作区快照按已登记版本 +1）；`upsert_artifact` 冲突改为按 (attempt, path, hash) 幂等 | 第 2 步遗留 L2-3 |
| D3-9 | Mission 级判定在**全部 Task COMPLETED** 后进行，验收副本取自**终结任务**（无依赖者的任务；若多个则按拓扑序最后一个）的已接受产物；`pytest:` 准则在该副本上重跑；自由文本准则由判定时的 Critic 裁定；不满足 → Mission FAILED `mission_criteria_unmet`（局部全过、整体不过，S3-06） | ORCH §12.4 |
| D3-10 | 多 Scheduler：两个 `Orchestrator` 实例（不同 owner）可同时驱动同一对库；互斥完全靠 `claim_intent` 的 CAS + 活租约；SDK 侧每实例一个 `AgentRuntime`（各自 owner_id，同一 `execution.db`）；两个实例都 `recover()` 时只有活租约 owner 能继续同一 Attempt | 原文 §17.1、§17.6；S3-04 |
| D3-11 | 租约丢失（S3-07）：owner 租约到期后由新 owner 接管继续同一 turn（不重跑）；若执行者已不可见（turn 不存在）→ LOST → 新 Attempt（retry_of）；旧执行的迟到结果只作历史（intent 已关闭，Attempt 非 RUNNING 时结果不被接受）；已 COMPLETED 的 Task 永不重跑 | 原文 §16.4；ORCH §12.1 |
| D3-12 | Task 预算不足（S3-08）：图校验阶段 Σ 预算超父级 → 整图拒绝并说明维度；运行期某 Task 的 Attempt 预留失败 → 该 Task FAILED（`budget_exhausted`）并 Mission FAILED（本步无重新分配） | 原文 §18.2 |
| D3-13 | Task 停止不再直接把 Mission 判 FAILED 的时机：任一 Task FAILED → Mission FAILED（stop_reason 继承），其余 READY/BLOCKED 任务 CANCELLED，在途 Attempt SUPERSEDED/CANCELLED（同 cancel 清理） | 原文 §19.1 |
| D3-14 | `proposed_tasks` 只保存在 Result Envelope 与事件里（`ResultSubmitted.payload.proposed_tasks`），不入图 | ORCH §5.1 |
| D3-15 | CLI 新增 `demo --scenario static-dag`：fixtures 演示固定结构 A→(B‖C)→D→E（`textkit` 小项目：A 写合同 `textkit/__init__.py` 与测试骨架、B 实现 `slugify`、C 实现 `word_count`、D 集成测试、E 写 `DELIVERY.md`）；真实模型模式由 Planner 自己出图 | ORCH §5.3 |

## 4. 任务

| 切片 | 内容 | 决定性测试 |
|---|---|---|
| A 图与 Commit | D3-1/2/3/6/12/13：`graph/*`、`TaskGraphProposal` 解析、`commit_task_graph`、`unblock_dependents`、`stop_task` 的级联、`GraphRejected` | `test_task_graph.py`（合法图、环、缺失、重复、预算和、根/终结）、`test_commit_graph.py` |
| B 调度与传递 | D3-4/5/7/8/10/11：`scheduling/allocator.py`、`_decide` 改为 Frontier 分配、多候选与 SUPERSEDED、`artifacts/versioning.py`、Context §10 第 3 项、版本递增 | `test_static_dag_closure.py`（S3-01/03/05/06/08）、`test_multi_scheduler.py`（S3-04/07） |
| C 演示与真实模型 | D3-15、evidence、`test_real_provider_static_dag.py`（opt-in） | `test_cli_static_dag.py` |
| D 收尾 | 独立 review、wheel、journal、0.9.x 版本（0.9.0 → 0.9.1，同 wheel 双包） | — |

## 7. 独立 review 后的修订（2026-09-11；裁决表见 journal §1；本节覆盖上文对应决定）

| # | 修订 |
|---|---|
| D3-5' | **Task.status 是其 Attempt 集合的派生函数**：存在 SUBMITTED/VERIFYING 候选 → VERIFYING；否则存在 PENDING/CLAIMED/RUNNING 候选 → ACTIVE；否则保持。Commit 只在派生值变化且为 §25.1 合法边时转换；`accept_result` 若 Task 仍 ACTIVE 先 ACTIVE→VERIFYING 再 →COMPLETED（同事务，两条合法边）。`create_attempt` 约束改为"open Attempt 数 < candidates_per_task"。候选**计入** `max_attempts`。已 SUBMITTED 的落选候选保留其 StoredResult（verification_state=REJECTED，verdict=superseded）与产物登记，只取消尚未提交的（理论 10-6：候选并存，本步只选择，合并留第 4 步） |
| D3-13' | Mission 停止时只取消 READY/ACTIVE/VERIFYING 的 Task 及其在途 Attempt；**BLOCKED 任务不做转换**（§25.1 无 BLOCKED→CANCELLED 边；随 Mission 终态终止）；Mission 级 `budget_exhausted` 不归罪任何 Task |
| D3-10' | `owner_scope="agent-orchestrator"` 常量、两实例共用；`Orchestrator(owner=…)` 同时作为编排租约 owner 与 SDK runtime `owner_id`（每实例唯一）；接管在途 SDK turn 须等 SDK Run 租约（默认 30 s）过期，编排 Attempt 租约默认 ≥ 2×；S3-04/S3-07 = 同一事件循环内两个实例（各自 Store 连接与 AgentRuntime）；`Store` 把 `database is locked` 包成 `StoreBusy`，循环遇到即跳过本轮 |
| D3-7' | Attempt 的 Artifact 集合 = **相对其初始输入（Mission 种子 + 上游注入）的 diff**；下游工作区 = 按拓扑序应用全部祖先的 accepted_artifacts（并行分支对同一路径给出不同 hash → `artifact_conflict`，同 hash 不冲突）；TaskNode 可选 `outputs: [路径]`，Graph Commit 对共享依赖者的兄弟任务做输出路径重叠的静态拒绝；**注入下游的上游产物进入受保护集合**（验收副本按冻结 hash 重落盘，被改写者 `rule_check` FAIL；上游文件要改须新 Task，第 5 步） |
| D3-6' | `accept_result` = 接受 + 解锁依赖者 + 替代其余候选 **同一事务**；新增崩溃点 `after_task_completed`；`recover()` 幂等自愈：前沿重算（前置全 COMPLETED 的 BLOCKED → READY）、终态 Task 下遗留非终态 Attempt 收口（SUPERSEDED）、LOST/TIMED_OUT/SUPERSEDED 的 usage 重导入与结算；终态 Task 的迟到结果只记历史（`ResultRejected(reason=superseded)`） |
| D3-9' | Mission 级验收对象 = 从 Mission 种子按拓扑序应用**全部 Task** 的 accepted_artifacts 得到的整合副本（冲突 fail closed）；`judge_mission` 报告覆盖全部 Task |
| D3-12' | `BudgetExhausted` 按 `account_id` 分流：task 账户 → Task FAILED（继而 Mission FAILED）；mission 账户 → Mission FAILED(`budget_exhausted`)；预留按 Frontier 优先级次序发起 |
| D3-2' | 校验前先做确定性归一：Task 缺失的预算维度按 Mission 池均分补齐（`max_tokens`/`max_cost_micros` 平均，`max_attempts`/`max_concurrency` 继承）；硬拒绝只保留超额/环/缺失/形状；拒绝反馈带维度名与剩余量并喂回 Planner 下一次提案（`planning_rejected`）；`max_planning_attempts` 进 `OrchestratorConfig`（默认 2） |
| D3-4' | `max_concurrent_model_calls` = max(config, `max_concurrency × candidates_per_task`)；停滞判定只对 `state=running` 的 turn 计时 |
| D3-8' | `artifact.version` 按 **(mission, path) 血缘**递增 |
| D3-16 | 去重：只在 `key` 重复、或（规范化 goal + 依赖集 + success_criteria）全同时拒绝 |
| D3-17 | Attempt 进入 LOST/TIMED_OUT/SUPERSEDED/CANCELLED 或所在 Task COMPLETED 时立即 `gateway.unbind(agent_id)`（先于 cancel_turn） |
| D3-18 | `graph_version` 存 Mission `final_report.graph_version`（随 mission.version CAS）；§19.4 老化与 §18.5 背压上限本步不做，登记推迟第 6 步 |
