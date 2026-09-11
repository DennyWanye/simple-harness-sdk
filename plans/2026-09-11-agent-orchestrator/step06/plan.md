# 第 6 步：多 Mission、多模型、背压和隔离运行 · 实施计划

- 日期：2026-09-11 · 基线 SDK main `408d914`（0.9.3 / agent_orchestrator 0.5.0，第 5 步 SHIPPED）
- 原文依据：§8–10、§17–21、§23、§28 第三阶段、§29；ORCH-BUILD-v1.0 §8（8.1–8.4）、§12、§13；需求简报（独立提取，52 条规则 / 42 条术语摘录 / 13 条歧义）存 `reports/design-brief.md`
- 术语：`../program.md` §0；本步新引入/重申的定义（先查 `agent-orchestration-theory/`，出处见简报 §4）：
  - **Budget / Cost / Backpressure**：「Budget：最多允许花多少资源。Cost：已经花了多少资源。Backpressure：下游处理不过来时，让上游减速。」「Budget = 整个任务总共最多花多少；Backpressure = 当前这一刻最多能跑多快。」（理论 11）
  - **Concurrency Limit / Queue Limit**：并发上限、队列上限；「到达上限后，需要暂停低优先级任务或拒绝新任务」（理论 11-11）。原文 §18.5 的六个上限：最大运行 Agent 数 / 最大等待任务数 / 最大待验证结果数 / 单 Task 最大 Attempt 数 / 最大 Task DAG 深度 / 单 Agent 最大子任务 Proposal 数。
  - **降速动作**（原文 §18.5 六项）：降低 Worker 并发；提高 Verifier 资源；暂停低优先级任务；禁止新任务继续分裂；合并重复候选；缩小每个 Attempt 预算。
  - **分层预算**：Global → Mission → Task → Attempt；「子任务预算来自父任务，不得凭空放大」（原文 §18.2；理论 11-3/11-16）。**Reserve / Settle**（§18.3）。
  - **Allocator / Scheduler**：「Allocator = 给多少；Scheduler = 什么时候、在哪里执行」（原文 §8.3）；Scheduler 决定「在哪个 Worker 或 GPU 上运行」= 本步的执行池选择。**Starvation**：等待越久优先级越高、保留固定探索预算（理论 05-10）。
  - **Model Routing**：「简单分类 → 小模型；任务拆解、路线判断 → 强推理模型；代码 → 代码模型；最终关键候选 → 最强模型」；升级策略「便宜模型先尝试 → 失败或低置信 → 换更强模型 → 仍失败 → 拆任务、换策略或人工介入」（原文 §9.3；理论 13-3）。**Heterogeneous Agents**：由不同模型/工具/能力构成的团队（理论 13-4）。
  - **Workspace 隔离**：「多个 Agent 不应直接共享同一个可写目录」，Attempt 1/2/3 → Workspace A/B/C（原文 §20.1）；合并「多个独立 Artifact → 测试和验证 → 选择或合并 → 新的 Candidate Artifact → 再次验证 → Commit 为正式版本」（§20.3）。
  - **Tool Gateway 检查链**（原文 §21.1，顺序是原文规定的）：身份和权限检查 → 参数 Schema 检查 → 风险与政策检查 → 速率和预算检查 → 执行工具 → 记录结果与审计日志。**最小权限**（§21.2；理论 13-14）。「外部内容不能改变系统权限；密钥不进入模型上下文」（§21.3）。
  - **Multi-tenancy**：数据隔离 / 权限隔离 / 资源配额 / 公平调度 / 成本归属 / 审计日志；「一个项目不能读取另一个项目的私有知识，也不能占光全部算力」（理论 13-20）——本步"多 Mission"按此定义。
  - **Trace**：每个对象应包含 `trace_id, mission_id, task_id, attempt_id, agent_id, model_version, prompt_version, retrieval_version, allocator_version, verifier_version`（原文 §23.1）；「没有版本信息，实验往往无法复现」（理论 12-13）。
  - **术语缺口（简报 §4 末）**：理论目录无"高低水位/滞回"、"执行池/runtime profile"、"权限交集"、"Verifier 路由"、"Verifier Worker"的定义；本 plan 按 ORCH §13 把它们登记为实施约定（§6.1），不宣称是原文原句。

## 1. 主要矛盾

并发扩大后系统必须**仍然安全、可控、可追踪**（原文 §28 第三阶段目标）：多个 Mission 同时运行时每个 Mission 只花自己的配额、只看自己的资料；Worker 产出快于 Verifier 时上游必须减速而不是让队列无限增长；一次 Attempt 实际跑在哪个物理模型上必须是持久绑定并可从 Trace 证明的事实，而不是 `model_profile_ref` 标签；每个 Attempt 只在自己的可写目录里工作；工具权限是四方交集，任何文本都提升不了权限。矛盾的主要方面是**"受控制的并发"是分层上限 + 背压 + 物理路由三件事共同保证的，缺一件并发就只是把 `max_agents` 改大**（ORCH §8.1 原句）。次要方面：完整 Trace 版本字段、多维预算耗尽的正确停止、未部署验证器的显式阻塞。

## 2. 范围

演示（`demo --scenario multi-mission`）：同一 Orchestrator 同时提交两个 Mission（textkit 静态 DAG + recorder 动态 DAG），两个 fixtures 执行池 `small` / `large`：Worker 默认路由 `small`，Planner/Manager/Critic 路由 `large`；某 Task 在 `small` 上验证失败一次后升级到 `large`；验证并发 1 且 Critic 被放慢时触发背压；两个 Mission 都 COMPLETED；证据目录按 Mission 分开并新增 `trace.json`、`metrics.json`、`scheduler.json`（背压时间线、路由决策、执行池）。

做：Global→Mission 配额与多 Mission 公平推进（D6-1）；背压模块与滞回（D6-2）；Allocator 接背压信号、探索额度（D6-3）；模型路由 + 多执行池 + 持久绑定 + 恢复分区 + 升级/降级（D6-4/D6-5）；Workspace 记录进 Artifact、只读依赖产物（D6-6）；权限四方交集 + §21.1 检查链 + 速率/费用检查 + 审计（D6-7）；预算多维耗尽（tokens / 工具调用 / 运行时间）（D6-8）；Verifier 路由：未部署的必需层显式阻塞、分层版本（D6-9）；Trace / Metrics / 密钥扫描（D6-10）；角色配比登记（D6-11）；CLI 与真实多模型报告（D6-12）。

不做：真实修改动作与人工审批（第 7 步）；多层 Manager / 组级管理（ORCH §8.3 明确不设前置）；GPU 时间维度（本部署无 GPU 账户）；跨进程多执行者压力测试（L3-5，登记不变）；Replay / A-B（第 8 步）。

## 3. 设计决定

| # | 决定 | 依据 |
|---|---|---|
| D6-1 | **多 Mission 与分层配额**：`OrchestratorConfig.global_budget: Budget | None`——非空时开 `budget:global` 账户，所有 Mission 账户以它为父（`open_account` 的 `fits_within` 校验：Mission 预算不得超过 Global 剩余；Global 预留/结算沿链累计，已有 `_chain` 逻辑）；Mission 之间**不挪用**：每个 Mission 只在自己的账户链上 Reserve/Settle（现状），S6-01 断言两账户互不影响、一个 Mission 池耗尽（`budget_exhausted`）另一个照常完成。**公平推进**：`_cycle` 对活跃 Mission 按 `created_at` 轮转起点（每轮起点后移一位），分配阶段 Mission 级并发 `max_concurrency` 不变，新增**全局运行上限** `max_running_attempts`（默认 = `max_concurrency × 4`）由背压模块统一裁决。**隔离**：工具网关绑定只到本 Attempt 工作区（现状）、知识检索只查本 Mission（`KnowledgeIndex.load(store, mission.id)` 现状）、证据目录 `evidence/<mission_id>/`（多 Mission 时写多份）；测试用第二个 Mission 的 Worker 试图读第一个 Mission 的文件路径（`../<other>`）→ `workspace_error` | 原文 §18.2、§17.5；理论 11-3、13-20 |
| D6-2 | **背压模块** `scheduling/backpressure.py`：`BackpressureLimits{max_running_attempts, max_pending_dispatch（PENDING/CLAIMED/AGENT_CREATED 的 attempt intent 数）, max_pending_verifications（PENDING/RUNNING 验证的结果数）, max_graph_depth, max_attempts_per_task, max_proposals_per_agent}`——后三项引用第 5 步已有旋钮，本模块是六个上限的**单一登记处**（原文 §18.5 六项一一对应）。观测 `observe(store) → Observation{running, pending_dispatch, pending_verifications, …}`；状态 `BackpressureState{level: NORMAL|RAISED, dimensions: {dim: {observed, high, low}}, since}` 持久化在新表 `scheduler_state(key, json, updated_at)`（schema v4），由 Commit Service 写。**高低水位与滞回（实施约定，无原文原句，§6.1 登记）**：`high = limit`，`low = floor(limit × low_watermark_ratio)`（默认 0.5）；观测 ≥ high → RAISED（事件 `BackpressureRaised{dimension, observed, high, low}`）；只有观测 ≤ low 才 CLEARED（`BackpressureCleared`）——中间区间保持上一状态，这就是"恢复有滞回"。事件写进每个活跃 Mission 的时间线（events 表 `mission_id NOT NULL`；同一次状态变化在各 Mission 的 idempotency_key 带 mission 前缀）。评估点：每个 `_cycle` 的分配阶段之前一次 | 原文 §18.5；理论 11-9/11-11；ORCH §8.2 backpressure 行 |
| D6-3 | **Allocator 接背压**（`allocate(..., pressure: BackpressureState)`）：RAISED 时——① "降低 Worker 并发"：有效并发 = `max(1, floor(concurrency_limit × reduced_concurrency_ratio))`（默认 0.5）；② "暂停低优先级任务 / 停止低价值扩展"：只允许 tier 0（冲突）与 tier 1（饥饿档）任务获得新 Attempt，公式档任务不分配——这是**调度层抑制，不改 Task 状态**（ORCH §13：不新增 PAUSED）；③ "禁止新任务继续分裂"：`_collect_manager` 在 RAISED 时把含 `add_task` 的提案记为 `rejected(reason=backpressure)` 并按 D5-10 反馈一次（Manager 可改为 set_role/set_priority）；④ "缩小每个 Attempt 预算"：RAISED 时 `attempt_reserve_tokens × reduced_reserve_ratio`（默认 0.5，下限 4000）；⑤ "提高 Verifier 资源"：`verifier_workers`（默认 2 = 原文 §29.1 "2 个 Verifier Worker"）在 RAISED 时提升到 `verifier_workers_boost`（默认 4）；⑥ "合并重复候选"：登记不做（第 5 步去重校验已阻止重复任务入图；候选级合并属综合任务）。**探索额度与老化**：RAISED 期间饥饿档不受影响（老化保持）；`exploration_slots`（默认 1）：即使 RAISED，仍允许最多 1 个"从未尝试过的任务"（uncertainty = 1）获得 Attempt——这是理论 05-10 "保留固定探索预算"的最小形式；"多样性配额"（理论 13-23）本步不做，登记为同一机制的后续扩展。**§29.3 公式不改项**（简报歧义 2）：背压作为公式之外的闸门实现，"下游积压惩罚"以 tier 过滤体现，不给 §29.3 加系数 | 原文 §8.2、§18.5、§19.4、§29.2 末条；理论 05-10 |
| D6-4 | **模型路由** `runtime/model_router.py`：`RuntimeProfile{profile_id, provider, model, price_table?, default_max_output_tokens?, max_output_tokens_ceiling?, tier: int（越大越强）, capabilities: {roles?: set, task_kinds?: set}}`；`RoutingRules{default: profile_id, by_role: {role: profile_id}, by_task_kind: {kind: profile_id}, escalate: {profile_id: profile_id}, escalate_after_failures: int（默认 1）, fallback: {profile_id: profile_id}}`；`ModelRouter.route(*, task, role, previous_attempts, profile_health) → RoutingDecision{profile_id, model, reason}`：顺序 = by_role（Planner/Manager/Critic/Synthesizer/Arbiter 等系统角色）→ by_task_kind → default；**升级**：上一 Attempt 在 profile P 上以 `verification_failed / outcome_failure / provider_error` 结束且该 Task 在 P 上的失败数 ≥ `escalate_after_failures` → `escalate[P]`（reason=`escalate:<P>→<Q>:<failure>`），沿链最多到最强档；**降级**：P 处于不可用冷却期（D6-5）→ `fallback[P]`（reason=`fallback:unavailable:<P>`），无 fallback → 不分配、显式等待。决策**冻结进 dispatch intent config**（`runtime_profile_id`, `model`, `routing`）并发事件 `ModelRouted{attempt_id, profile_id, provider_kind, model, reason}`；Attempt 记录新增 `runtime_profile_id`（JSON 字段，缺省 "default"）；采集时沿用 D10' 的回显核对：回显 model 必须等于 profile 的 model，否则 `model_echo_mismatch` 停止——"实际 provider/model 改变而不是只改标签"由回显证明（S6-03） | 原文 §8.2、§9.3；理论 13-3；ORCH §8.2 model_router 行 |
| D6-5 | **多执行池与持久绑定** `runtime/agent_worker.py`：`RuntimePools{profile_id → AgentBridge}`，每个 profile 一个 `AgentRuntime`，**各自的 `execution-<profile_id>.db`**（`default` 池沿用 `execution.db` 路径，旧证据目录可直接打开）；所有 bridge 调用按 intent 的 `runtime_profile_id` 选池（缺省 `default`）；`recover()` 逐池 `recover_pending_turns`，且**只重绑属于本池的 intent**——另一池永不打开它的 agent（S6-08：intent 的 profile 与当前配置里的池集合不符 → 该 intent 不被采集/派发，事件 `RuntimeProfileUnavailable{profile, reason=not_configured}`，Attempt 保持在途直到租约到期按既有 LOST 路径处理，绝不换模型静默接管）。**不可用检测**（S6-06）：同一 profile 连续 `profile_failure_threshold`（默认 2）次 turn 因 provider 不可达类错误失败（错误分类 `provider_unavailable`：连接/超时/5xx/`ProviderUnavailableError`）→ `scheduler_state.profile_health[P] = {unavailable_until: now + profile_cooldown_seconds（默认 60）}` + 事件 `RuntimeProfileUnavailable{profile, until, failures}`；冷却期内路由按 D6-4 降级或等待；等待有界：Task 等待超过 `profile_wait_seconds`（默认 300）→ `stop_task(RUNTIME_UNAVAILABLE)`（新 `MissionStopReason`）；其他 profile 上的 Mission 不受影响。fixtures：`FailingProvider(kind="unavailable", times=N)` | ORCH §8.2 表后段（分开 SDK 存储/明确恢复分区/持久绑定）；原文 §17.1、§17.6 |
| D6-6 | **Workspace 与产物**：`Artifact.workspace`（= `<attempt_id>`，§20.2 字段）落进记录与证据；两个候选 Attempt 写同名文件各在 `workspaces/<attempt>/`，各自 Artifact 不同 hash，只有通过验证并 accept 的才进 `accepted_artifacts`，另一份是历史（S6-04）；**只读依赖产物**（ORCH 用语）：上游 accepted artifacts 注入下游工作区后列入 `protected`（第 3 步已有）——本步在网关层拒绝对 protected 路径的 `workspace_write_file`（`workspace_error: protected input`），除非 Task `outputs` 声明改写该路径（第 3 步 D3-7' 的例外保留）。合并只经"新候选 → 再验收"（§20.3）：本步不新增合并器，登记 | 原文 §20.1–20.3 |
| D6-7 | **权限交集与检查链** `governance/policies.py` + `runtime/tool_gateway.py`：`DeploymentPolicy{allowed_tools, max_tool_calls_per_attempt（默认 48）, max_file_bytes, denied_path_prefixes}`（部署政策，配置项，默认 = 全部四个工具）；有效工具 = Mission.allowed_tools ∩ Task.allowed_tools ∩ Role.tool_names ∩ Deployment.allowed_tools，**只取交集、任何一方都不能放宽**（简报歧义 10 的登记），在派发时算好冻结进 intent（`allowed_tools`）；网关 `execute` 按 §21.1 顺序：① 绑定/身份（未绑定 run → `tool_not_bound`）② 权限（不在交集 → `tool_not_allowed`）③ 参数 Schema（按 `TOOL_SCHEMAS` 检查 required/type/additionalProperties → `invalid_arguments`）④ 风险与政策（路径逃逸、protected、denied 前缀、文件大小 → `workspace_error`/`policy_denied`）⑤ 速率与预算（本 Attempt 工具调用数 ≥ `min(deployment.max_tool_calls_per_attempt, task.budget.max_tool_calls)` → `tool_rate_limited`；Mission 工具调用维度耗尽 → 同）⑥ 执行 ⑦ 审计：每次拒绝写事件 `ToolCallRejected{attempt, tool, reason}`（经 Commit），成功调用计入 `tool_calls` 计数（账本按 Attempt 结算）。**"Prompt 和模型置信度不能授权"**：交集来自合同与配置，网关不读消息文本；测试里 Worker 信封/消息声称"已获授权使用 X"仍被拒 | 原文 §21.1–21.3；理论 13-14/13-15 |
| D6-8 | **预算多维耗尽**（S6-07）：`Budget` 新增 `max_tool_calls`（原文 §18.1 "工具调用次数"维度；`fits_within` 同规则）；账本新增 `settled_tool_calls`/`reserved_tool_calls`（Reserve 按 `min(deployment.max_tool_calls_per_attempt, …)`，Settle 按网关计数）；`max_runtime_seconds`：Mission/Task 级按 `now − created_at`（Mission）/ 首个 Attempt 起（Task）判断，超过 → 不再分配、`stop_task/fail_mission(BUDGET_EXHAUSTED, dimension=runtime)`；tokens 维度沿用。三个维度耗尽时：无新 `AttemptCreated`；`costs.json` 里 reserved 与 settled 之和守恒、不重复相加（`usage_ref` 唯一导入）。Global 账户（D6-1）同样参与 `reserve` 链检查 | 原文 §18.1–18.3、§17.4 |
| D6-9 | **Verifier 路由**：`DEPLOYED_LAYERS = {format_check, rule_check, critic_review, code_test}`；`verification_policy` 引用未部署层（`formal_check`, `human_review`）→ Graph/Task/Change Commit 时拒绝（`verification_policy_undeployed`，不假通过也不静默降级）；运行期若仍遇到（旧库）→ 该层 ERROR 且 `stop_task(VERIFIER_UNAVAILABLE)`（新 stop reason），**不进入重试**（重试不会让验证器出现）；`human_review` 的部署在第 7 步（简报歧义 6 的裁定）。每条 `VerificationLayerRecorded` 与 `verification.json` 带 `verifier_version`（`verifier-v1`；critic 层用 Critic 模板版本）。Verifier 并发：`verifier_workers`（默认 2）个验证同时进行（`_cycle` 把待验证结果交给有界 asyncio 任务集合；同一结果只有一个验证任务；Commit 调用不跨 await 持有事务，现有约束不变） | ORCH §8.2 verifier_router 行、§12.4；原文 §14.1、§29.1 |
| D6-10 | **Trace / Metrics / 密钥**（S6-09）`observability/trace.py`：`trace(store, mission_id)` 对每个 Result 给出 `trace_id`（= mission_id，简报歧义 7 的裁定：trace 根是 Mission，span 是 Attempt）、`mission_id/task_id/attempt_id/agent_id`、`model_version`（= `runtime_profile_id` + 请求 model + 回显 model）、`prompt_version`、`context_version`、`retrieval_version`（= 检索版本 `retrieval-v1`，与 `context_version` 分开：前者是检索算法版本，后者是包的内容指纹）、`allocator_version`、`verifier_version`（各层）；`metrics(store)`：并发/队列长度（运行、待派发、待验证）峰值、验证通过率、每角色/每 profile 的 tokens 与费用、知识复用次数（`KnowledgeUsed`）、Mission 完成时间、超时/LOST 数；证据目录新增 `trace.json`、`metrics.json`、`scheduler.json`（背压时间线 + 路由决策 + profile 健康）。**密钥**：`write_evidence` 对写出的每个文件做模式扫描（`sk-[A-Za-z0-9_-]{20,}`、`Bearer …`、配置里的 `api_key`/`APIKEY` 字段名）命中即抛错不落盘（S6-09 "无密钥"）；`assert_no_secrets` 从字段名扩展到值模式 | 原文 §23.1–23.3；理论 12-13 |
| D6-11 | **角色配比**：`ROLE_MIX_START`（Explorer 20 / Exploiter 40 / Critic 20 / Synthesizer 10 / Verifier 10，原文 §29.2）登记为配置常量并在 `metrics.json` 报告"观测到的角色分布 vs 起点"；动态调整只落地"积压时减少 Worker、增加 Verifier"（D6-3 ①⑤）；Connector/Simplifier/Failure Analyst 无原文份额（简报歧义 5）→ 归入 Exploiter 份额统计，登记 | 原文 §29.2；ORCH §8.3 |
| D6-12 | **CLI 与真实报告**：`demo --scenario multi-mission`（fixtures 两池 small/large、两个 Mission、放慢的 Critic 触发背压）；真实多模型小规模负载报告：DeepSeek 官方端点两 profile（`deepseek-flash` = small、`deepseek-v4-pro` = large），Worker → flash、Planner/Manager/Critic → pro、`escalate: flash→pro`；报告记录每个 Attempt 的 profile、回显模型、费用（unpriced 记账，L2-6 登记不变）与是否发生升级 | ORCH §8.4 |
| D6-13 | 版本：simple_harness 0.9.4、agent_orchestrator 0.6.0；schema v4（`scheduler_state` 表；`budget_accounts` 加 `reserved_tool_calls/settled_tool_calls` 列；`artifacts` JSON 加 `workspace`）；旧库升级保留历史 | ORCH §14.1 |

## 4. 任务

| 切片 | 内容 | 决定性测试 |
|---|---|---|
| A 配额与背压 | D6-1/D6-2/D6-3/D6-8/D6-13：global 账户、公平轮转、背压模块与滞回、Allocator 闸门、多维预算、schema v4 | `test_multi_mission.py`（S6-01）、`test_backpressure.py`（S6-02 + 滞回单测）、`test_governance.py::test_s6_07_*` |
| B 路由与执行池 | D6-4/D6-5：`model_router.py`、`RuntimePools`、intent/Attempt 的 profile 绑定、回显核对、升级/降级/不可用/恢复分区 | `test_model_router.py`（S6-03/06/08 + 路由规则单测） |
| C 隔离与权限 | D6-6/D6-7：`Artifact.workspace`、protected 写拒绝、`DeploymentPolicy`、检查链、速率、审计事件 | `test_workspace_isolation.py`（S6-04）、`test_governance.py::test_s6_05_*` |
| D 验证与可观测 | D6-9/D6-10/D6-11：未部署层拒绝/阻塞、`verifier_version`、Verifier 并发、`trace.py`/metrics/密钥扫描、角色配比登记 | `test_observability.py`（S6-09 + 扫描）、`test_verifier_routing.py` |
| E 闭环与演示 | D6-12：fixtures 双池 provider、放慢 Critic、CLI、真实两模型 opt-in 测试与报告 | `test_multi_mission_closure.py`（demo 证据）、`test_real_provider_multi_model.py`（opt-in） |
| F 收尾 | review、wheel 0.9.4、CHANGELOG、testcase、program.md、journal、推送、清理 | — |

## 5. 风险

- **验证并发**：`_verify` 内部多次 Commit 且中间有 await（pytest 子进程、Critic）；两个验证并发时事务不交叠（每次 Commit 是短事务），但 `_critic_verdicts`/`_client_ids` 等内存表要按 result_id 键控（已是）。若 SQLite 忙 → `StoreBusy` 由 `_cycle` 重试（现状）。
- **多池恢复**：`AgentRuntime.recover_pending_turns` 只恢复本库的 turn；orchestrator 侧按 intent profile 分区即可，不需要 SDK 改动。
- **真实模型**：pro 作为 Planner 更贵；报告规模控制在 1 个 Mission、≤ 6 个 Attempt。

## 6. 独立 review 后的修订（2026-09-11；裁决表见 journal §1）

- **D6-1'（Global 账户）**：`BudgetLedger.reserve` 增加显式 `mission_id`（P0-1）；`create_mission` 里 Mission 未命名的维度从 Global 继承（`inherit_limits`，P0-2）；`fits_within` 比的是父的**上限**而非剩余，超卖由 `reserve` 链兜住（P1-3，§6.1 登记）；Global 池耗尽 → `fail_mission(BUDGET_EXHAUSTED, detail.scope="global")`，不归罪任何 Task（P1-2）。全局运行上限的权威检查点在 `create_attempt` 事务内（跨 Mission 统计 OPEN Attempt，P1-12），`_cycle` 的轮转与它同批落地（P1-13）；demo 用 `--max-concurrency 2` + `max_running_attempts 3`。
- **D6-2'（背压状态）**：`CommitService.record_backpressure(observation)` 在**一个事务**里写 `scheduler_state` + 各活跃 Mission 的 `BackpressureRaised/Cleared` 事件（P1-4）；`scheduler_state.backpressure.log`（有界 200 条）是变更序列的唯一真值，`scheduler.json`/`metrics.json` 由它统计，events 只是各 Mission 时间线的投影（P1-11）。S6-02 的触发维度定为 `pending_verifications`，因此**验证异步化（有界 asyncio 任务集合 `verifier_workers`）与 `_has_inflight` 计入验证任务提前到切片 A**（P0-6）；D6-3 ⑤ "RAISED 时提升 Verifier 并发"砍掉（P2-6）。
- **D6-3'（Allocator 措辞）**："公式档 = §29.3 打分档（tier 2）"，不等同于"低优先级"；"合并重复候选"（§18.5/§20.3，候选 Artifact 级）本步不做，归第 8 步（P2-3）。
- **D6-4'（路由改造面）**：`self._config.model` 的 11 个使用点（Planner/Worker/Manager 的回显核对 3 处、`model_profile_ref` 4 处、`model=` 2 处、`_reservation` 价格表、`AgentBridge(unpriced)`）全部改为**取自冻结在 dispatch intent 里的 `runtime_profile_id / model / price`**；回显核对的 expected 是 intent 里的 `model`（P0-3，重启后换配置也不误判，S6-08）。错误分类表（P1-8）：`provider_unavailable`（连接/超时/5xx/`ProviderUnavailableError`）→ profile 健康（降级）；`verification_failed / outcome_failure / envelope_invalid / turn_failed:provider_protocol_error(tool_parse 等)` → 升级计数；`empty_response` → 都不算（SDK 同 turn 内重试）。每 profile 价格表接进 `_reservation` 与每池的 estimator（P1-9）；真实报告仍 unpriced（未注入 DeepSeek 价目，L2-6 改归第 8 步"评测成本效率"时注入）。
- **D6-5'（执行池改造面）**：`AssembledOrchestratorRuntime` → `RuntimePools{profiles: {id: (runtime, bridge, execution db)}, gateway, workspaces}`，**gateway 与 WorkspaceManager 共享单例**；`Orchestrator(config, provider)` 保留单 provider 兼容路径（= 单 `default` profile），多 profile 用 `Orchestrator(config, providers={id: provider}, routing=RoutingRules)`（P0-4）。S6-06 有界等待（P0-5）：不可用 profile 无 fallback 时，Task 不分配但**计入 in-flight**（`run()` 不退出），冷却期满再试；等待累计 ≥ `profile_wait_seconds` → `stop_task(RUNTIME_UNAVAILABLE)`；测试用亚秒配置（cooldown 0.2 s / wait 0.6 s）。`stop_task` 是 Mission 级终止（级联取消其余任务），S6-06 的"其他可执行 Mission 继续"只承诺**跨 Mission** 隔离（P1-1）。
- **D6-8'（预算维度）**：新增维度必须同步四处：`fits_within`、`normalise_budgets`、`inherit_limits`、Global→Mission 继承（P0-2）。S6-07 的"守恒"改为三条可判定断言：同一 `usage_ref` 重复导入返回 0；账户 `settled_tokens` = 该 Mission 全部 `imported_usage` 之和；结算后 `reserved_* ≥ 0` 且开放预留 = 未 SETTLED 的 reservation 之和（P1-5）。L3-3（UNKNOWN 用量让预留永不释放）：本步给 `costs.json` 加 `held_reservations` 列表并在 Mission 终态时发 `ReservationHeld{subject, reason=unknown_usage}` 事件——对账通道是"可见并可追"，不是自动放行（登记）。
- **D6-9'（验证层常量）**：不新建 `DEPLOYED_LAYERS`，沿用 `STEP2_IMPLEMENTED_LAYERS`（`contracts/models.py`）；拒绝 reason 改为 `verification_policy_undeployed`；运行期未部署层 ERROR → `stop_task(VERIFIER_UNAVAILABLE)` 不重试（P2-2）。`verifier_workers` = 验证的 asyncio 并发度，是实施约定（§6.1 登记）。
- **D6-10'（Trace）**：`trace_id = ids.trace_id(mission_id)`，与事件信封同值（P1-10）；`metrics.json` 在 unpriced 部署下费用列显式 `null` 并注明（P2-6）。
- **D6-11'（角色配比）**：配比表按 §9.2 八个角色列全，Connector / Simplifier / Failure Analyst 记 `null`（P2-5）。
- **fixtures（P1-14）**：`RoleScriptedProvider` 已回显自己的 `model`（`ProviderResponse.model`），两池用不同 model 名，回显核对在 fixtures 上是真实的；`critic_delay_seconds` 让 Critic 变慢（provider 内 `asyncio.sleep`，远小于 `critic_wait_seconds`）；`FailingProvider(kind="unavailable", times=N)` 抛连接类错误。
- **Store 事务（P1-15）**：`transaction()` 记录持有者 `asyncio.current_task()`，另一任务进入未关闭的事务 → `StoreError`；配不变量测试。

### 6.2 接手的遗留 → 本步处置（review P2-1）

| 遗留 | 处置 |
|---|---|
| L2-4 `run_tests` 无网络隔离 | 推迟到第 7 步（真实动作沙箱）；本步 `DeploymentPolicy` 不含网络，登记 |
| L2-6 真实运行 unpriced | 机制（每 profile 价格表）本步做；DeepSeek 价目注入归第 8 步 |
| L2-7 每 Attempt token 预留非硬上限 | 不变：硬上限靠 SDK per-turn limits + `hard_cap_micros`（需价格表）；登记 |
| L3-3 UNKNOWN 出站调用的对账通道 | `held_reservations` + `ReservationHeld` 事件（可见可追），不自动放行 |
| L3-4 老化 + 背压上限 | 第 5 步 aging + 本步 D6-2/D6-3 → **关闭** |
| L3-5 跨进程锁压力测试 | 不做（§2） |
| L4-3 成本维度无系统预留 / `assert_no_secrets` 只查字段名 | 后半句本步做（值模式扫描）；前半句登记不变 |
| L4-4 `tool_parse` 频繁 turn FAILED | 计入升级计数（D6-4' 分类表）：flash 反复 tool_parse → 升级到 pro |

### 6.1 本步实施约定（非原文原句；按 ORCH §13 登记）

- 高低水位与滞回：`high = limit`、`low = floor(limit × 0.5)`，中间区间保持上一状态。原文只有静态上限（§18.5）与"到达上限后暂停低优先级或拒绝新任务"（理论 11-11）。
- 执行池 / runtime profile：一个 profile = 一个 `AgentRuntime` + 独立 `execution-<profile>.db` + 一个 provider/model + 可选价格表；`default` 池沿用 `execution.db`。理论无此词，最近似"异构 Agent"（理论 13-4）与 §29.1 拓扑。
- 权限交集：Mission ∩ Task ∩ Role ∩ Deployment，四方都只能收紧；原文只有 Role 档（§21.2）与最小权限原则。
- 背压作为 §29.3 公式之外的闸门（tier 过滤 + 并发缩减），不给公式加"下游积压惩罚"系数。
- 探索额度 = RAISED 期间保留 1 个"从未尝试任务"的槽位；多样性配额（理论 13-23）未做。
- `trace_id` = mission_id；`retrieval_version` 与 `context_version` 是两个字段。
- 模型升级 = 新建带 `retry_of` 的 Attempt（§25 不改旧 Attempt 的 model），S6-03 的"保留先前 Attempt 与费用"由此保证。
- 未部署验证层：提交期拒绝 + 运行期 `VERIFIER_UNAVAILABLE` 停止，不重试；`human_review` 第 7 步。
- 降速动作六项中"合并重复候选"不做（去重校验已在第 5 步）；角色配比只登记与观测，不做配比调度。
- 事件表 `mission_id NOT NULL`：全局信号（背压、profile 健康）写进每个活跃 Mission 的时间线，`scheduler_state` 表存当前值。
- 上游输入只读（执行中登记，行为变更）：下游工作区里上游 Task 交付、而本 Task 未在 `outputs` 声明的路径，写入在网关就被拒（`protected_input`）；第 3 步"改写上游合同在提交时被拒"的测试相应改为"在网关被拒"。Mission 种子里的受保护文件仍按第 2 步的篡改检测处理（验证副本重建 + rule_check），不在网关拒绝。
- S6-08 的崩溃点选"Agent 已在小池创建、尚未提交输入"：崩溃若落在工具调用进行中，SDK 把它记为 UNKNOWN 出站效果并保持阻塞（S2-08 的既定语义，L3-3），那是另一件事；关闭时所有执行池一起停，避免先关一个池时另一个池继续推进 turn。
- 真实报告只用 deepseek-flash（用户指示）：两个执行池同模型，`small` 输出上限 16384、`large` 32768；报告中的"升级"是换池与输出上限，不是换模型名。
