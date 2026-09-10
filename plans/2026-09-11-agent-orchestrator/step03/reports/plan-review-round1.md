# step03 plan 独立 review 原文（子代理 claude-opus-5，只读，2026-09-11）

> 中文摘要：P0 ×7（多候选与状态机互斥、BLOCKED→CANCELLED 新增边、owner 语义、同名产物冲突、下游改写上游作弊、accept/unblock 与 supersede 崩溃窗口）、P1 ×9、P2 ×6。结论：按原 plan 不可实现，先修 plan。处置见 journal.md §1、plan.md §7。

---

# 第 3 步 plan 独立 review（只读）

## A. 发现清单

### P0

**1. D3-5 多候选与已实现的 Task 状态机 / `create_attempt` 直接冲突（三处）**
- `commit_service.py:586-601`：`create_attempt` 显式拒绝"该 Task 已有 open Attempt"；`:583` 又要求 Task 必须 READY/ACTIVE。`candidates_per_task=2` 在当前代码下**一行都跑不通**。
- `commit_service.py:1013-1016`：`record_result` 无条件 `next_task(task, VERIFYING)`。第二个候选提交时 Task 已是 VERIFYING → `state_machines.py:_TASK[VERIFYING]={COMPLETED, ACTIVE}` → `IllegalTransition`。
- `fail_result:1243` 把 Task VERIFYING→ACTIVE；此时另一候选 PASS，`accept_result:1090` 需要 VERIFYING→COMPLETED，但 Task 已是 ACTIVE → 再次 `IllegalTransition`。
- 建议：把 **Task.status 定义为其 Attempt 集合的派生函数**（存在 VERIFYING 候选 → VERIFYING；否则存在在途候选 → ACTIVE），只有派生值变化时才 Commit 一次转换；`accept_result` 前先把 Task 归位到 VERIFYING。这样 ORCH-BUILD §5.2「某个候选进入 VERIFYING 不妨碍记录其他在途 Attempt」得到满足，且**不新增任何 §25.1 边**。同时把「一个 Task 一个 open Attempt」放宽为「open Attempt ≤ candidates_per_task」，`ordinal` 仍在事务内分配。

**2. D3-13 的 BLOCKED→CANCELLED 是新增 Task 边，违反 ORCH-BUILD §13**
- §25.1 只有 READY→CANCELLED 与 ACTIVE→CANCELLED；`state_machines.py:_TASK[BLOCKED]=frozenset({READY})` 已按原文钉死。D3-13 写「其余 READY/BLOCKED 任务 CANCELLED」，实现时必然要么抛异常要么偷偷加边。
- 建议：只取消 READY/ACTIVE 任务；BLOCKED 任务随 Mission 终态一起终止（§19「在调度控制或 Mission/分支政策中暂停，不给 Task 私自新增状态」），并在 plan 里把这条写成显式约定 + journal 登记。

**3. D3-10 的 owner 语义会直接打死 S3-04/S3-07**
- `runtime.py:299-302` `agent_id_for(owner_scope, creation_key)`：agent_id 由 **owner_scope** 派生；`runtime.py:772-778` `binding()` 对异 owner_scope 一律返回 None。
- 后果 A：`bridge.result()` → `runtime.open()` 抛 `AgentNotFound`，而 `event_handler.py:366-370` 的 `_collect` **不捕获** → 接管方直接崩。
- 后果 B：`agent_worker.py:103-110` `liveness()` 把异 owner 的 agent 当 `exists=False` → `event_handler.py:421-427` 立刻 `mark_attempt_lost`。于是「接管在途 turn 不重跑」永远退化成 LOST + 新 Attempt，S3-07 判定项自相矛盾。
- 真正需要区分的是另一个字段：`assembly.py:148` 把 `ports.owner_id = config.owner_id` 传给内核，`uow.py:2226-2228` `claim_runtime_activation` 对活 owner 抛 `UnitOfWorkConflict`，`kernel.py:2846-2855` 把冲突转成 pending wake 重试。
- 建议：明确写死 **`owner_scope="agent-orchestrator"` 两实例共用**（永不由编排 owner 派生），**`OrchestratorConfig.owner_id` 每实例不同**（同时是内核 Run 租约 owner 与编排租约 owner）；并记录接管在途 SDK turn 必须等 **SDK Run 租约 30 s**（`agents/ports.py:48`）过期，因此编排 Attempt 租约需 ≥ 2×，在 `test_s3_07` 里断言这个次序。

**4. D3-7 的产物传递规则会在 plan 自己的演示图上死锁**
- `artifacts/workspace.py` `snapshot()` 把**工作区里每一个文件**登记成 Artifact；`commit_service.py:1119-1124` 把 `stored.artifacts`（= 整棵树）原样写进 `accepted_artifacts`。
- 于是 B 和 C 的 `accepted_artifacts` 都包含 A 产出的 `textkit/__init__.py`。D 同时依赖 B、C → D3-7 的「同名冲突 → 拒绝创建 Attempt 并停止 Task（`artifact_conflict`）」在 A→(B‖C)→D 上必然触发。若 B、C 各自真的改了同一个 `__init__.py`（这正是"两个功能"最自然的写法），hash 也不同，本步又明确不做 §20.3 的合并/Synthesizer，图无法完成。
- 建议：(a) Attempt 的产物集合定义为**相对其种子输入的 diff**（新建/修改的文件），不是整棵树；(b) 同路径同 hash 不算冲突；(c) 在 Task Contract 里声明各任务的输出路径，Graph Commit 时对**兄弟任务输出路径重叠做静态拒绝**（§19.3 风格），运行期冲突只作兜底；(d) fixture 与真实模型 Planner 都必须被约束成 B/C 输出路径不相交（D3-15）。

**5. 下游任务重新打开了第 2 步已修的 P0-1 作弊路径**
- `event_handler.py:686-702` `_protected_seed` 的受保护集合**只来自 Mission seed**。上游产出的测试骨架（A 写的 `tests/`）、集成测试（D 写的）都不在 Mission seed 里 → 不受保护。D 的 Worker 可以改写 B 的实现或 A 的合同来让自己的集成测试通过，仍拿到 VERIFIED。D3-7 却写「受保护种子文件规则不变」。
- 建议：把「注入到下游工作区的上游 `accepted_artifacts`」纳入受保护集合（验收副本按冻结 hash 重新落盘，`rule_check` 对被改写者直接 FAIL），并在 S3-01 里加一条作弊回归。

**6. D3-6 解锁是 accept 之后的第二次 Commit → 崩溃窗口造成永久死锁**
- `accept_result`（`commit_service.py:1099-1150`）是一个独立事务，不碰依赖者；`recover()`（`event_handler.py:165-179`）只重绑 intent。崩在 accept 与 unblock 之间 → Task COMPLETED 而下游永远 BLOCKED，没有任何路径能救回（§19.3 死锁）。
- 建议：`unblock_dependents` 必须**在 accept_result 同一事务内**（`TaskUnblocked` 事件同事务发出）；同时 `recover()` 增加幂等的前沿重算（所有前置 COMPLETED 的 BLOCKED → READY）作为自愈；恢复矩阵新增 `after_task_completed` 崩溃点。

**7. Superseding 没有崩溃窗口设计，会变成活锁**
- 崩在 accept 与「其余候选置 SUPERSEDED」之间：候选 #2 仍是 RUNNING，Task 已 COMPLETED。它的结果到达时 `record_result` 要求 Task ACTIVE→VERIFYING，从 COMPLETED 出发非法 → 每个 cycle 抛一次，永不收敛。
- 建议：supersede 与 accept 同事务；`_collect_attempt` 对**终态 Task 的迟到结果**记为历史（`ResultRejected(reason=superseded)`，不做任何 Task 转换，符合 §12.1「旧结果保存为历史候选」）；新增崩溃点 `after_accept_before_supersede`；`recover()` 扫描终态 Task 下的非终态 Attempt 并收口。

### P1

**8. D3-5 丢弃候选结果，与理论 10-6 相悖**
理论 02/10-6 明确要求两个提交的候选**并存为 Candidate 交给 Verifier/Judge/Synthesizer，不能后写覆盖前写**；设计 §20.3 也是「选择或合并」。plan 把 RUNNING/SUBMITTED 一律取消。建议：已 SUBMITTED 的候选保留 `StoredResult`（verdict 记 `superseded`）与其 Artifact 登记，只取消尚未提交的；并明确写「本步只做选择、不做 Synthesizer 合并，合并留到第 4 步」。

**9. 僵尸执行者仍握有工作区写权限**
`runtime/tool_gateway.py:132` 的 `unbind` **全仓无人调用**（`grep -rn unbind` 只命中定义）。SUPERSEDED/LOST/TIMED_OUT 的 Agent 在协作式 cancel 生效前仍能 `workspace_write_file`/`run_tests`，违反 ORCH §12.1「先撤销它的新动作资格」。建议：在这四种终态与 accept 之后立即 `gateway.unbind(agent_id)`，先于 `cancel_turn`；S3-05 断言 supersede 之后的工具调用被拒。

**10. 并发与停滞判定互相踩踏**
`assembly.py:150-152` 里 `max_concurrent_model_calls=2`、`max_concurrent_tool_calls=config.max_concurrency` 是**全局信号量**；`event_handler.py:402-418` 的停滞判定对「存活、无 blocker、`stall_seconds=180` 内 ordinal 无推进」判 TIMED_OUT。并行 B‖C 再叠 2 个候选时，排在信号量后面的 turn 恰好符合这三条 → 假 TIMED_OUT + 取消 + 烧掉一次 attempt 额度。建议：`max_concurrent_model_calls` 由 Mission `max_concurrency × candidates_per_task` 推导；把「尚未开始的 turn / 卡在运行时信号量」排除出停滞计数；补一条决定性测试。

**11. D3-9 把 Mission 级判定悄悄收窄**
`commit_service.py:1165-1175` `judge_mission` 要求所有 Task COMPLETED，且报告固定取 `tasks[0]`。plan 取「拓扑序最后一个终结任务」的产物作验收副本，与 §12.4 / §20.3 的"整体成果"在多叶子 DAG 上不符。建议：验收副本 = **所有终结任务 `accepted_artifacts` 的并集**（真冲突则 fail closed 并说明），`judge_mission` 报告覆盖全部 Task，Mission 的 `pytest:` 准则在该合并副本上跑。

**12. D3-12 把 Mission 级预算耗尽错误地归罪于某个 Task**
`governance/budgets.py:203-221` 的 `reserve` 沿祖先链检查，先命中哪个账户就抛哪个。DAG 下 Task X 的预留可能因**其他任务吃光 Mission 池**而失败——这是 §19.4 starvation，不是 X 的失败；而 `_next_attempt` 的 `except BudgetExhausted` 一律 `stop_task(X)`。建议：按 `error.account_id` 分流——task 级 → Task FAILED；mission 级 → Mission FAILED(`budget_exhausted`) 且不给任何 Task 扣帽子；并要求预留按 Frontier 优先级次序发起，使失败可复现。

**13. D3-2 的预算校验比 plan 承认的更严，真实模型模式易连环拒绝**
`models.py:138-145` `fits_within`：父级限制了某维度而子级为 null 即判不通过。也就是说 Planner 必须为每个 Task 显式写全 Mission 限制的所有维度（含 `max_concurrency`、`max_attempts`）。再叠加「Σ max_tokens ≤ Mission」+ 整图拒绝，真实 Planner 大概率反复被拒。建议：编排层在校验**之前**做确定性归一（缺失维度按 Mission 池均分补齐），硬拒绝只保留"超额"；Planner 输入包里给出剩余池与必填字段清单；拒绝反馈必须带维度名与剩余量（S3-08 判定项）。

**14. 候选是否吃 `max_attempts` 未定**
`commit_service.py:610-616` 用 `counts_attempt=True` 预留。`candidates_per_task=2` 会把修复次数腰斩，S3-05 很容易变成 `max_attempts_reached`。建议：显式二选一——候选计入（演示预算相应放大），或探索候选 `counts_attempt=False` 并新增 `max_candidates` 维度。

**15. D3-11 漏掉迟到费用的归账**
ORCH §12.1：「已经发生的调用费用……即使迟到，也应归入实际发生的 Attempt，不因为 LOST 而丢账」。`_settle_if_known` 遇 unknown 拒绝结算，预留保持占用；若不在 supersede/LOST 之后再扫一遍导入，Mission 预留永远无法收口。建议：在 `recover()` 与 Mission 判定前对 LOST/TIMED_OUT/SUPERSEDED 的 Attempt 做一次 usage 重导入 + 结算扫描。

**16. 同一 `orchestrator.db` 上两个 Store 连接的锁竞争未定**
`storage/store.py:159-165` WAL + `timeout=5.0`，`:219` `BEGIN IMMEDIATE`，且是**同步调用**。两实例竞争时最坏阻塞事件循环 5 s，然后抛 `sqlite3.OperationalError: database is locked`——它不是 `StoreError`，全链路无人捕获。建议：plan 明确 S3-04/S3-07 是"同一事件循环两实例"还是"两进程"；把 busy 包成可重试的 `StoreBusy` 并退避重试；显式设 `busy_timeout`。

### P2

**17. D3-8 实际上没有解决 L2-3**
`workspace.py` 的 `versions` 以 (attempt, path) 为键，而每个 Attempt 只快照一次 → version 恒为 1，症状原样保留。另外 `ids.py:37-41` 的 `artifact_id` 本来就是 `f(attempt, path, hash)`，`store.py:657-673` 已是 `ON CONFLICT(artifact_id) DO NOTHING` —— D3-8 后半句「改为按 (attempt, path, hash) 幂等」是空操作。建议：版本按 **(mission, path) 血缘**递增（§20.2 的 `version: 4` 就是血缘版本），单开一张版本表；plan 里删掉那句空操作。

**18. 去重规则过脆**：整图因"规范化 goal 重复"被拒绝，理论 10-13 反对过度自动合并，反向的过度拒绝同样有害。建议只在 `key` 重复、或（规范化 goal + 依赖集 + success_criteria）三者全同时拒绝，其余发 `TaskGraphDuplicateSuspected` 事件继续。

**19. §19.4 优先级老化、§18.5 背压上限（最大待验证结果数、最大图深度）被静默丢弃**。要么加一项老化项，要么在 journal 里作为显式推迟（第 6 步）登记，不要不作声。

**20. `MAX_PLANNING_ATTEMPTS` 引用了不存在的旋钮**：`assembly.py:49-67` 的 `OrchestratorConfig` 里没有。S3-02 要求"重提一次后可通过"，还需要一个把拒绝原因喂回 Planner 的反馈包，否则第二次提案通过与否是运气。建议加 `max_planning_attempts: int = 2` + `planning_rejected` 反馈字段。

**21. Context Builder 的两处硬编码必须改**：`context_builder.py` 的 `absent_by_design: ["dependencies", …]` 与 `build_planner_package` 的 `constraint: "exactly one Task"`。Planner 包还需带上 Mission 预算池与 DAG 约束（见 P1-13）。工作量小，但 plan §4 切片 B 没列。

**22. `graph_version` 没有落脚点**：D3-3 只把它写进 receipt。§17.3 要求 DAG 主版本可作后续 Proposal 的 `base_version`。建议存到 Mission 行上并在 Commit 时做 CAS。

---

## B. plan 应显式记录的决定

1. **Task 状态的定义**：Task.status 是其 Attempt 集合的派生函数；列出多候选场景下实际用到的 §25.1 边全集，并声明"accept 只从 VERIFYING 发生、不新增边"；BLOCKED 任务在 Mission 停止时不做 CANCELLED（§13 约定）。
2. **Owner 模型**：`owner_scope` 为常量、两实例共用；`OrchestratorConfig.owner_id` 每实例唯一（同时是内核 Run 租约 owner）；接管在途 turn 需等 SDK Run 租约 30 s，编排 Attempt 租约 ≥ 2×；S3-04/S3-07 是同进程双实例还是双进程。
3. **产物范围与传递**：Attempt 的 Artifact = 相对种子输入的 diff；同路径同 hash 不冲突；兄弟任务输出路径在 Graph Commit 做静态互斥检查；注入下游的上游产物进入受保护文件集合（验收副本按冻结 hash 重落盘）。
4. **原子性与恢复**：accept + unblock + supersede 三者同一事务；新增崩溃点 `after_task_completed` / `after_accept_before_supersede`；`recover()` 负责前沿重算与终态 Task 下遗留候选的收口；终态 Task 的迟到结果只记历史。
5. **预算归属**：谁负责把 Mission 池切成 Task 预算（编排归一 vs Planner 提案）；候选是否计入 `max_attempts`；mission 级 vs task 级 `BudgetExhausted` 的归罪分流；LOST/SUPERSEDED 的迟到费用重导入时机。
6. **Mission 级验收对象**：所有终结任务 `accepted_artifacts` 的并集作为验收副本；`judge_mission` 报告覆盖全部 Task；多叶子时的冲突处理是 fail closed。

---

## C. 结论

**不能按现状作为第 3 步的唯一版本实现。** 方向、章节映射与 §5.4 八条验收的对应关系是对的，切片划分也合理，但有五处会在实现第一天就撞墙的硬伤：多候选与 `create_attempt`/§25.1 直接互斥（P0-1）、D3-13 造了一条 §25.1 不存在的边（P0-2）、D3-10 的 owner 语义会让 S3-07 的"接管不重跑"永远退化成 LOST（P0-3）、D3-7 的同名冲突规则在 plan 自带的 A→(B‖C)→D 演示上必然触发（P0-4）、以及解锁/supersede 的两个崩溃窗口会分别造成永久死锁与活锁（P0-6/7）。另有一条安全回归：下游任务重新打开了第 2 步已修的"Worker 改写自己的验收测试"路径（P0-5）。这些都是设计层面的缺口而非编码细节，必须先改 plan（配合上面 6 条显式决定），其中 P0-3 与 P0-4 建议各写一个可丢弃的 spike 先验证（双实例 owner 语义、B/C 产物集合的实际内容）再定稿。改完之后，工作量仍在一个版本可交付的范围内。