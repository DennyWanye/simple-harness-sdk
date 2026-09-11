# 第 6 步 · 执行记录

## 1. 关键裁决

独立 review（claude-opus-5，只读；原文 `reports/plan-review-round1.md`）6 P0 / 13 P1 / 7 P2，处置落在 plan §6 / §6.1 / §6.2：

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| P0-1 | P0 | Global 账户使预留的 `mission_id` 写成 "global"，`costs.json` 空 | `reserve(mission_id=…)` 显式传入（D6-1'）；S6-01 断言两 Mission 的 reservations 非空且互不包含 |
| P0-2 | P0 | `max_tool_calls` 未进继承列表，任务图提交整体失败 | `normalise_budgets` / `inherit_limits` / Global→Mission 三处补齐（D6-8'） |
| P0-3 | P0 | 回显核对写死 `config.model`，路由后 Planner 立刻 mismatch | 11 处改为取冻结 intent 的 `model`（D6-4'） |
| P0-4 | P0 | 多执行池改造面缺失（装配单例） | D6-5' 改造面清单：共享 gateway/workspaces，单 provider 兼容路径 |
| P0-5 | P0 | S6-06 有界等待到不了（`run()` 空转即退） | 等待中的 Task 计入 in-flight；亚秒测试配置（D6-5'） |
| P0-6 | P0 | S6-02 触发依赖验证异步化，却分在不同切片 | 验证异步化 + in-flight 提前到切片 A（D6-2'） |
| P1-1 | P1 | `stop_task` 是 Mission 级终止 | 措辞改正；S6-06 只承诺跨 Mission |
| P1-2 | P1 | Global 耗尽归因错 | 第三分支 `scope=global`（D6-1'） |
| P1-3 | P1 | `fits_within` 比上限不比剩余 | 措辞改正 + 超卖由 reserve 链兜住（§6.1） |
| P1-4 | P1 | `scheduler_state` 绕过 Commit / 不同事务 | `record_backpressure` 一个事务写状态 + 事件（D6-2'） |
| P1-5 | P1 | "守恒"断言不成立 | 三条可判定断言（D6-8'） |
| P1-8 | P1 | 错误分类未定义（L4-4） | 分类表（D6-4'） |
| P1-9 | P1 | 每 profile 价格表无落地 | 机制本步做；真实仍 unpriced，L2-6 归第 8 步 |
| P1-10 | P1 | `trace_id=mission_id` 与 `ids.trace_id` 冲突 | `trace_id = ids.trace_id(mission_id)`（D6-10'） |
| P1-11 | P1 | 背压事件按 Mission 投影的口径 | `scheduler_state.log` 为唯一真值（D6-2'） |
| P1-12 | P1 | 全局上限权威检查点 / demo 默认值 | `create_attempt` 事务内；demo 参数（D6-1'） |
| P1-13 | P1 | 公平轮转在现状下空操作 | 与全局上限同批（D6-1'） |
| P1-14 | P1 | fixtures 缺回显/慢 Critic/不可用 | 回显已存在；`critic_delay_seconds`、`FailingProvider`（plan §6） |
| P1-15 | P1 | RLock 可重入 + await = 静默损坏 | 持有者校验 + 不变量测试 |
| P2-1 | P2 | 遗留登记 2/8 | §6.2 逐条处置 |
| P2-2 | P2 | `DEPLOYED_LAYERS` 第二套真值 | 沿用 `STEP2_IMPLEMENTED_LAYERS`（D6-9'） |
| P2-3 | P2 | 术语/裁定三处瑕疵 | 已改（D6-3'、§6.1） |
| P2-4 | P2 | 52 条规则无对照表 | §7 对照表（收尾前补） |
| P2-5 | P2 | 角色配比归并掩盖规则 | 八角色列全，三者 null（D6-11'） |
| P2-6 | P2 | 可砍范围 | Verifier boost 砍掉；费用列 null |
| P2-7 | P2 | 测试建议 | 滞回边界、reserve 多维、S6-08 断言库里无记录、事务不变量、S6-05 授权文本 |

## 2. 执行记录

| 切片 | 提交 | 内容 | 测试 |
|---|---|---|---|
| A/1 | `45328bf` | 背压状态机（水位/滞回）、schema v4、`Budget.max_tool_calls` 账本维度 | `test_backpressure_state.py` 5 |
| A/2 | 本提交 | Global 账户（`budget:global`，Mission 继承未命名维度）、`reserve(mission_id=…)`、工具调用维度的预留/结算/网关上限（`tool_calls_for` 计数器）、运行时间维度（Mission/Task）、规划期池耗尽的显式停止、Mission 轮转、事务持有者校验（P1-15） | `test_multi_mission.py` 2（S6-01）、`test_governance.py` 5（S6-07） |
| A/3 | 本提交 | 背压落地：每轮观测 → `record_backpressure`（状态 + 各 Mission 事件同一事务，`log` 为唯一真值）→ Allocator 闸门（并发减半、只放行冲突/饥饿档 + 1 个探索槽）、预留缩减、Manager 不得 add_task（`ChangeLimits.admit_new_tasks`）、全局运行上限在 `create_attempt` 事务内；验证改为有界 asyncio 任务集合（`verifier_workers`，计入 in-flight，崩溃在下一阶段边界抛出）；fixtures 慢 Critic / 可配置 critic 脚本；证据 `scheduler.json` | `test_backpressure.py` 3（S6-02 + 闸门/准入单测） |
| B | 本提交 | `runtime/model_router.py`（RuntimeProfile / RoutingRules / 升级阶梯 / 降级 / 错误分类表）；`RuntimePools`（每 profile 独立 AgentRuntime 与 `execution-<profile>.db`，gateway/workspaces 共享，关闭时所有池一起停）；路由冻结进 intent（`runtime_profile_id` / `model` / `routing`）+ `ModelRouted` 事件；回显核对取 intent 冻结的 model；profile 健康（`RuntimeProfileUnavailable`、冷却、有界等待 → `runtime_unavailable`）；intent 绑定的池未配置时不派发不采集；`UnavailableProvider` fixture | `test_model_router.py` 8（S6-03 / S6-06 ×2 / S6-08 + 路由单测 4）；编排全套 160 passed |
| C | 本提交 | `governance/policies.py`（DeploymentPolicy、Mission ∩ Task ∩ Role ∩ Deployment 冻结进 intent）；Tool Gateway 按 §21.1 顺序（身份/权限 → 参数 Schema → 风险与政策：越界、拒绝前缀、只读上游输入 → 速率 → 执行 → 记录），每次拒绝经 Commit 写 `ToolCallRejected`（只记参数键与路径，不记内容）；`Artifact.workspace`（§20.2）；**行为变更**：上游输入在下游工作区只读，第 3 步 P1-6 测试的"改写上游合同在提交时被拒"改为"在网关就被拒"（更严），种子受保护文件仍走第 2 步的篡改检测 | `test_workspace_isolation.py` 2（S6-04）、`test_governance.py` +3（S6-05）；第 3 步 P1-6 测试更新 |
| D | 本提交 | 未部署验证层：提交期统一 reason `verification_policy_undeployed`，运行期 ERROR → `stop_task(verifier_unavailable)` 不重试；每层记录 `verifier_version`；`observability/trace.py`（trace_id = `ids.trace_id(mission)`，每个 Attempt 一个 span：profile / 请求与回显模型 / prompt / context / retrieval / allocator / router / 各验证层版本）；`observability/metrics.py`（健康、按角色与 profile 的 tokens，unpriced 下费用为 null 并注明、验证通过率、知识复用、§9.2 八角色配比）；`observability/secrets.py` + 证据写入前扫描（模式 + 环境变量里的密钥值，命中即拒写且不回显值），`assert_no_secrets` 扩到值 | `test_observability.py` 5（S6-09 + 验证路由）；编排全套 170 passed |
| E | 本提交 | CLI `demo --scenario multi-mission`（fixtures：两个执行池 small/large、两个 Mission、慢 Critic；env：两个 profile 同为 `SH_MODEL`）；证据按 Mission 写 `missions/<id>/` + 汇总 `multi-mission.json`（写前扫描密钥）；fixtures `demo_multi_mission_profiles`；真实测试 `test_real_provider_multi_model.py`（opt-in）；第 2 步 CLI "未实现场景"探针改为第 7 步的 `approval-action`；版本 simple_harness 0.9.4 / agent_orchestrator 0.6.0，CHANGELOG、testcase、program.md | `test_multi_mission_closure.py` 1；编排全套 171 passed / 5 skipped |
| R1 | 本提交 | 代码 review round 1 处置（§3）：服务角色执行池不可用的有界等待、`_deferred` 修剪、Global scope 归因与 Mission 合同继承、工具调用分摊与可预留余量、持久 `tool_calls` 表、未配置执行池不占 in-flight、artifact 密钥扫描与 JSON 脱敏、`held_reservations` / `ReservationHeld`、`only_in_own_pool`、错误精确分类、Critic 健康与回显、fallback 健康、metrics 口径、未绑定拒绝审计、运行时间检查位置、`context_rejected`、demo 升级 | `test_step06_review_fixes.py` 14；编排全套 184 passed / 5 skipped |

## 3. 代码 review 处置

独立代码 review（claude-opus-5，只读；原文 `reports/code-review-round1.md`）2 P0 / 7 P1 / 12 P2。处置提交见 §2 末行；决定性测试 `tests/orchestrator/step06/test_step06_review_fixes.py`：

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| P0-1 | P0 | Planner / Manager / Critic 的执行池冷却且无 fallback 时 `RoutingUnavailable` 逃出主循环，整个 orchestrator 崩溃 | **修复**：Planner 有界等待（`_deferred_planning`，超时 `fail_planning(runtime_unavailable)`）；Critic 转成该层 ERROR（不会 PASS）；Manager 推迟决策（Task 自身重试照常）；测试 2 条 |
| P0-2 | P0 | 等待执行池的 Task 所在 Mission 被别的路径终止后，`_deferred` 残留让 `run()` 永不退出 | **修复**：`_prune_deferred` 只保留 Task 未终态且 Mission 仍 ACTIVE 的等待；测试 1 条 |
| P1-1 | P1 | Global 池耗尽被归罪给某个 Task（attempts 维度还会变成 `max_attempts_reached`） | **修复**：第三分支 `fail_mission(budget_exhausted, scope=global)`，规划阶段同样带 scope；另发现并修复：有 Global 预算时 Mission 合同本身也继承未命名维度（否则图校验放行、开户再拒，未捕获异常）；测试 2 条 |
| P1-2 | P1 | Mission 的 `max_tool_calls` 被原样复制给每个 Task，并行时第二次预留误判耗尽而终止 Mission | **修复**：`normalise_budgets` 按任务数分摊；预留不超过链上可预留余量，只有"已花光"才算耗尽，余量全在途时只是暂不分配；测试 1 条 |
| P1-3 | P1 | 工具调用计数只在进程内存，重启后少计、上限重置、拒绝审计键撞键 | **修复**：新表 `tool_calls`（schema v4 内，按 SDK call id 至多一次），结算与网关上限都读持久计数；`ToolCallRejected` 的键改为 `run_id:call_id`；测试 1 条（崩溃后另一进程结算出同样的数） |
| P1-4 | P1 | 绑到未配置执行池的 SUBMITTED intent 让 `run()` 空转 | **修复**：`_has_inflight` 与 `pending_dispatch` 都不计这类 intent；它留给原池，原池重启后原样采集；测试 1 条 |
| P1-5 | P1 | 证据里的 artifacts 未做密钥扫描 | **修复**：复制前扫描，带密钥的 artifact 不复制并列在 `withheld_artifacts`；二进制文件列出为未扫描；测试改为扫描全部文件、全部模式 |
| P1-6 | P1 | L3-3 的 `held_reservations` / `ReservationHeld` 未落地 | **修复**：`costs.json` 列出被 UNKNOWN 占住的预留；结算被拒时发一次 `ReservationHeld`（按 subject 幂等）；测试 1 条 |
| P1-7 | P1 | 同模型两池时真实测试证明不了物理路由，偏离未登记 | **修复 + 登记**：demo 汇总给每个 Attempt 加 `only_in_own_pool`（另一池执行库里查无此 Agent），真实测试断言它、断言全部服务 intent 在 large 池、要求两个 Mission 都完成；acceptance、plan §6.1 已登记"只用 flash" |
| P2-1 | P2 | 错误分类按子串匹配 | **修复**：只认 `error_code` / `code` / `kind` 字段的精确值；`provider_cancelled`、`provider_empty_response` 两边都不算；`turn_failed` 只有 provider 类错误才计入升级 |
| P2-2 | P2 | Critic 不影响 profile 健康、无回显核对 | **修复**：Critic 采集也记健康、做回显核对（不符 → 该层 ERROR） |
| P2-3 | P2 | fallback 目标自己的健康不检查 | **修复**：fallback 目标也在冷却 → 等待；测试 1 条 |
| P2-4 | P2 | metrics 口径 | **修复**：judge Critic 计入 critic，只数 SETTLED；`scheduler_state.peaks` 记录真实峰值；背压转换次数按本 Mission 的事件数 |
| P2-5 | P2 | 未绑定调用的拒绝不审计、path 不截断 | **修复**：未绑定 run 按 agent_id 找回 intent 后照样审计；path 截到 200 字符 |
| P2-6 | P2 | RAISED 期间新 Mission 收不到 Raised；多实例队头阻塞 | **登记**（§5） |
| P2-7 | P2 | 异步验证收尾：只抛第一个异常、`run_pytest` 取消时留孤儿进程 | **修复** 后者（取消时杀进程组）；前者登记 |
| P2-8 | P2 | Global 账户配置漂移被静默忽略；Global 的运行时间变成各 Mission 各自计时 | **登记**（§5） |
| P2-9 | P2 | 运行时间检查在判定之前 | **修复**：移到判定之后；规划阶段运行时间检查登记 |
| P2-10 | P2 | 密钥守卫让循环崩溃 / 证据整份放弃 | **修复**：包构建被拒 → 该工作可见地停止（`context_rejected`：Worker 停 Task、Planner 停规划、Manager 推迟、Critic 该层 ERROR）；Planner 包补上密钥检查；证据 JSON 改为脱敏写入并报告 `redactions`（不再整份拒写）；测试 1 条 |
| P2-11 | P2 | demo 与闭环测试偏弱 | **修复**：demo 第一次 Critic 判 FAIL，产生一次 small → large 升级；闭环测试断言升级、背压升起并清除、两个 Mission 的 Attempt 交替 |
| P2-12 | P2 | 其他（Critic/Planner 工具调用不计入维度、等待无持久事件、httpx 客户端未关闭、大小写不敏感文件系统） | 最后一项**修复**（只读判定按 casefold）；其余登记（§5） |

## 4. 证据

- **确定性测试**：`tests/orchestrator`（step02–06）171 passed / 5 skipped（skip 均为真实模型 opt-in）；step06 共 35 条（34 条确定性 + 1 条真实模型 opt-in；S6-01…S6-09 各有决定性测试，另含背压状态机、路由规则、网关顺序、验证路由单测与 CLI 演示闭环）。
- **真实模型（deepseek-flash ×2 执行池）**：运行 1（`reports/real-multi-mission-run1.md`）两个 Mission 同时运行都 COMPLETED，82.6 s；一次真实升级（parse_kv 的 task-2 在 small 池验证失败 → large 池重试通过，`retry_of` 保留旧 Attempt）；背压两次升起与清除；全部回显 `deepseek-flash`；密钥检查：真实密钥值逐字节比对全部文件未出现，词边界模式 0 命中。
- **SDK 全量回归**（HEAD `6e87e6f`，脚本 scratchpad `regress/run.sh`）：58 failed / 2211 passed / 10 skipped / 15 errors，红集 73 条 = 基线，**0 新红**。
- **wheel 0.9.4**：（待回填）

## 5. 遗留

| # | 事项 | 归属 |
|---|---|---|
| L6-1 | 真实运行只用 deepseek-flash（用户指示），两个执行池同模型：路由、升级、回显核对都已在真实端点上跑通，但"换到不同模型名"只由 fixtures（small / large 两个模型名）证明 | 若以后允许第二个真实模型，再补一次跨模型名运行 |
| L6-2 | 真实运行仍按 unpriced 记账：每个 profile 的价格表机制已做，DeepSeek 价目未注入，`metrics.json` 的费用列为 null 并注明 | 第 8 步（成本效率评测）注入价目 |
| L6-3 | 降速动作里"提高 Verifier 资源"未做（评审 P2-6 砍掉），"合并重复候选"未做；角色配比只观测不调度；多样性配额未做 | 第 8 步评测后再决定 |
| L6-4 | 在途 UNKNOWN 出站调用（L3-3）：本步只做到可见（关闭时所有执行池一起停、证据可追），没有自动对账放行 | 既定语义；第 7 步真实动作审计时再议 |
| L6-5 | `run_tests` 子进程仍无网络隔离（L2-4）；部署政策只有工具与路径两个维度 | 第 7 步沙箱 |
| L6-6 | 背压只观测运行中、待派发、待验证三维；Task 深度、单 Task Attempt 数、单 Agent 建议数三个上限在图变更与预算层执行，只在注册表登记 | 既定设计 |
| L6-7 | GPU 时间、搜索次数、Agent 数量三个预算维度本部署无对应资源；运行时间由事件时间推出，Attempt 记录无单独字段 | 视部署需要 |
| L6-8 | 指标里新思路数、剪枝率、结果重复率、误报率、污染率未统计 | 第 8 步 |
| L6-9 | 代码 review 登记项：RAISED 期间才创建的 Mission 只收到 Cleared；多实例时验证名额可能被别的实例的结果占住；异步验证只抛第一个异常；Global 账户换配置被静默忽略、Global 的运行时间按 Mission 各自计时；规划阶段不做运行时间检查；Critic / Planner 的工具调用不计入维度；等待执行池没有持久事件（重启后等待时钟重置）；env 路径的 httpx 客户端未显式关闭 | 视第 7/8 步需要 |

