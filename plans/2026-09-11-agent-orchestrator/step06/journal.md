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
