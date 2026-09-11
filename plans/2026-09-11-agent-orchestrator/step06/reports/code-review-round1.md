<!-- 独立代码评审（claude-opus-5，只读）；由主会话从代理回复落盘，内容未改；处置见 journal §3 -->

# 第 6 步实现评审（只读）

**结论：现在不能交付。** 有 2 条 P0 和 7 条 P1 必须先处理。P0 都是主循环的活性问题：一个是整个 orchestrator 崩溃，一个是 `run()` 永不退出，两个我都用脚本实测复现了。

**测试情况**
- `tests/orchestrator/step06`：34 passed，1 skipped（真实测试，需加 `--run-real-provider`）。
- `tests/orchestrator` 全量（step02–06）：全绿，只跳过 5 个真实测试。
- `demo --scenario multi-mission --provider fixtures` 能跑完：背压 RAISED/CLEARED 各出现 3 次，两个 Mission 都 COMPLETED。
- 这些测试都没有覆盖下面列的缺陷。
- 复现脚本放在 scratchpad 的 `probe/` 下，仓库没有任何改动。

---

## P0

**P0-1 服务角色路由不到可用 profile 时，整个 orchestrator 崩溃**
- **发现**：Planner、Manager、Critic 走 `_route_service`（`event_handler.py:327`）选池。目标 profile 在冷却期且没有 fallback 时，它抛出 `RoutingUnavailable`。三个调用点都没捕获：
  - `event_handler.py:637`（planner，经 `_start_planning` 和 `_planning_rejected:1155`）
  - `:1749`（manager）
  - `:1997`（critic，在异步验证任务里，经 `_raise_if_verification_crashed` 在下一个阶段边界抛出）
- `_cycle`（`:526-539`）只接 StoreBusy、CommitRejected、IllegalTransition，所以 `run()` 直接抛异常，所有 Mission 一起停。
- **实测**（`probe_route_crash.py`）：large 池不可用，threshold=1。第一个 Planner 失败后输出 `RUN CRASHED: RoutingUnavailable runtime profile large is unavailable`。
- **影响**：demo 和真实配置的 RoutingRules 都只有 `fallback={"small":"large"}`，large 没有 fallback，而 Planner、Manager、Critic 全在 large 上。真实 DeepSeek 连续两次 429 或 5xx 就会触发，这是现实场景。
- **违反**：D6-4「无 fallback → 不分配、显式等待」；D6-5' 有界等待；S6-06「其他可执行 Mission 继续」。
- **建议**：服务角色也走有界等待：
  - Planner：延后创建 intent，Mission 计入 in-flight，超时后 `fail_planning(RUNTIME_UNAVAILABLE)`。
  - Critic：转成 `ContractError`，让该层 ERROR，不能算 PASS。
  - Manager：推迟管理决策。
  - 加回归测试：large 池不可用、无 fallback 时，另一个只用 small 的 Mission 仍然 COMPLETED，`run()` 不抛异常。

**P0-2 `_deferred` 泄漏，`run()` 永不退出**
- **发现**：`_deferred`（`event_handler.py:182`）只在两处删除：`_defer_for_profile` 超时（`:2092`）和 `_next_attempt` 路由成功（`:2349`）。这两处都只在 ACTIVE Mission 的 `_decide` 里执行。
- 如果 Task 正在等 profile，而它的 Mission 被别的路径终止（运行时间耗尽、cancel、另一个 Task 的 stop 级联、echo mismatch），这个条目就永远留着。`_has_inflight`（`:522`）因此永远返回 True，`run()` 一直轮询下去。
- **实测**（`probe_deferred.py`）：Mission 设 `max_runtime_seconds=2`，small 池不可用且无 fallback。结果 Mission 已 `FAILED budget_exhausted`，但 `run()` 超过 8 秒没返回，`_deferred` 里仍有 `task-1`。
- **违反**：D6-5'「等待有界」；run-until-idle 的语义。
- **建议**：`_has_inflight` 只统计 Mission 仍 ACTIVE、Task 非终态的等待项；`_release_mission` 和 Mission 终止时清掉对应条目。加回归测试。

---

## P1

**P1-1 Global 池耗尽的归因没做（plan 明确要求）**
- **发现**：`_next_attempt` 的 `BudgetExhausted` 分支（`event_handler.py:2491-2515`）只判断 `account_id == mission_account(...)`。Global 账户 `budget:global` 耗尽会落到 else 分支，变成 `stop_task(task, …)`：
  - 归罪给一个 Task；
  - 如果耗尽的是 attempts 维度，stop reason 会变成 `max_attempts_reached`。
- `_start_planning`（`:594`起）在规划阶段因 Global 耗尽而失败时，detail 里也没有 `scope`。
- 全仓 grep 不到 `scope="global"`，也没有任何测试覆盖 Global 耗尽。
- **违反**：D6-1'「Global 池耗尽 → `fail_mission(BUDGET_EXHAUSTED, detail.scope="global")`，不归罪任何 Task」（review P1-2）。journal §1 却记成「已处置」。
- **建议**：增加第三个分支，`error.account_id == GLOBAL_ACCOUNT` 时走 `fail_mission` 并带 `scope=global`，规划阶段同样处理。补 S6-01 或 S6-07 的测试，覆盖 Global 的 tokens 和 attempts 两个维度。

**P1-2 工具调用维度的预留会误判耗尽，整个 Mission 被错误终止**
- **发现**：
  - `normalise_budgets` 把 Mission 的 `max_tool_calls` 原样复制给每个 Task（`graph/task_graph.py:181`），不像 tokens 那样分摊。
  - `_next_attempt` 每个 Attempt 预留 `tool_cap = min(max_tool_calls_per_turn, task.max_tool_calls)`（`:2410-2412, 2456`），也就是 Mission 的全额。
  - 两个并行 Task（默认 `max_concurrency=2`，或 `candidates_per_task≥2`）时，第二次预留在 Mission 账户上直接 `BudgetExhausted`，按 `:2503` 走 `fail_mission`。此时实际一次工具调用都还没发生。
- **实测**（`probe_toolcalls.py`）：Mission 设 `max_tool_calls=10`，A、B 两个独立 Task。第二个 Attempt 报 `EXHAUSTED account=budget:mission-… dim=tool_calls req=10 rem=0`。
- S6-07 测试是单 Task、`max_concurrency=1`，碰不到这个问题。
- **违反**：D6-8 / S6-07「已发生费用和在途预留仍保留正确」，不应该误停。
- **建议**：参照 tokens 的做法。可选方案：
  - 在 `normalise_budgets` 里按 Task 数分摊 `max_tool_calls`；或
  - 预留取 `min(cap, Mission 剩余 ÷ 可并发数)`；或
  - Mission 维度预留不足时只是不分配，不走 `fail_mission`。
  - 加双 Task 并行的回归测试。

**P1-3 工具调用计数只存在进程内存里，重启后结算少计、上限被重置**
- **发现**：结算依据的是 `gateway.executed_calls`，它扫描内存里的 `self.calls`（`tool_gateway.py:145, 159-168`）。
  - 重启后列表是空的，崩溃前已执行的调用在重启后结算时计为 0（`commit_service.py:2494`，`_executed_tool_calls` 在 `event_handler.py:274`），Mission 的工具调用维度可以被超用。
  - 网关的每 Attempt 上限（`binding.max_tool_calls`）也随进程重置。
  - `ToolCallRejected` 的幂等键用的是 `sequence=len(gateway.calls)`（`event_handler.py:235`），重启后会和旧事件撞键。`append_event` 碰到重复键会静默返回已有记录，所以新的拒绝审计会丢失。
  - `self.calls` 无限增长，且 `executed_calls` 每次结算都是 O(n)。
- **违反**：D6-8「Settle 按网关计数（事实，不是猜测）」；程序的崩溃恢复不变量；§21.1 审计完整性。
- **建议**：计数改从各池 SDK 执行库里的持久工具调用记录读，或者把计数持久化到编排库；拒绝事件的键改用 SDK 的 `call_id`（context 里有）。加「崩溃后重启再结算」的测试。

**P1-4 已提交（SUBMITTED）的 intent 绑到未配置的池时，`run()` 永不退出，也没有 LOST 路径**
- **发现**：
  - `_collect` 先判断 `_pool_missing` 并直接返回 False（`:879-881`），`_observe_liveness` 和 stall 检测都不执行。
  - `_has_inflight`（`:524`）把这个 SUBMITTED intent 算作在途。
  - 结果是只配 large 池重启时，`run()` 空转到永远。plan 说的「租约到期按既有 LOST 路径处理」在代码里不存在。
  - 同时 `pending_dispatch` 维度会被这类永远派发不了的 intent 钉住，可能长期 RAISED。
- 现有 S6-08 测试只覆盖崩溃在 `after_agent_created`（CLAIMED 状态）且 `until_idle=False` 的情况，恰好绕开了这个问题。
- **违反**：D6-5' / S6-08。
- **建议**：`_has_inflight` 排除绑到未配置池的 intent；或者给这类 Attempt 设显式超时，转 LOST 或 `stop_task(RUNTIME_UNAVAILABLE)`，归属仍留给原池、不被换模型接管。补一个崩溃在 SUBMITTED 之后、`until_idle=True` 的测试。

**P1-5 证据里的 artifacts 没做密钥扫描**
- **发现**：`write_evidence` 用 `shutil.copy2` 直接复制 Worker 写出的文件（`observability/evidence.py:93-98`），没有经过 `guard_text`。
- 测试只扫 `.json/.jsonl`，而且只查 `sk-` 模式（`test_observability.py:111-114`、`test_multi_mission_closure.py:78-81`）。
- **违反**：D6-10「对写出的每个文件做模式扫描」；S6-09「证据目录 grep 密钥模式 = 0」。
- **建议**：复制前按文本扫描，二进制文件跳过并记录；测试改为遍历全部文件，并覆盖 `SECRET_PATTERNS` 里的所有模式。

**P1-6 L3-3 的处置没有落地，也没登记**
- **发现**：全仓 grep 不到 `held_reservations` 和 `ReservationHeld`，`costs_report` 只加了 `global` 字段。
- **违反**：plan §6.2 表格中 L3-3 的处置，以及 D6-8'。
- **建议**：要么实现（Mission 终态时发 `ReservationHeld`，`costs.json` 列出仍被 UNKNOWN 占住的预留），要么在 journal 里明确登记偏离并改 §6.2。

**P1-7 真实测试在两池同模型下证明不了物理路由，与验收不符且没登记**
- **发现**：
  - `SH_MODEL=deepseek-flash` 时 small 和 large 回显同一个模型名，`echoed_models == [model]` 区分不了池（`test_real_provider_multi_model.py:91-94`）。
  - `if attempt["echoed_models"]` 为空就跳过。
  - 最终断言只要求 `any(COMPLETED)`（`:103`）。
  - 没断言升级，也没断言池分离。
- acceptance 的附加门槛写的是「deepseek-flash 为小模型、deepseek-v4-pro 为升级目标」。用户已要求真实测试统一用 flash，这属于合理偏离，但必须登记。
- **建议**：
  - 在 plan、acceptance、journal 登记这项偏离。
  - 同模型下改为证明池分离：每个 Attempt 的 `agent_id` 只出现在 `execution-<profile>.db` 里，另一个池的库里 `liveness.exists == False`（S6-08 测试已有现成写法）。
  - 服务 intent 的 profile 全部断言为 large；报告里必须出现 `runtime_profile_id`、回显与 `retry_of`。

---

## P2

1. **`classify_turn_error` 按字符串子串匹配，分类表和实现对不上**
   - 位置：`model_router.py:30-36, 127-138, 221-227`。
   - 对整个错误 JSON 做子串匹配：任何含 `provider_` 的内容都算 `provider_error`；`provider_cancelled`（我们自己取消 turn 也可能出现）算作不可用。
   - `_counts_for_escalation` 把所有 `turn_failed`（除 unavailable 外）都计入升级，包括 empty_response 重试耗尽。D6-4' 的分类表规定 empty_response 两边都不算。
   - 建议：按 `error_code` 字段精确匹配，并去掉 `provider_cancelled`。
2. **Critic 路径不影响 profile 健康，也没有回显核对**
   - 内联采集（`:2043-2066`）不调用 `_note_turn_health`；回显核对只有 planner、attempt、manager 三处（`:1166/1225/1838`）。
   - large 池的主要负载就是 Critic，它的物理路由没有被证明，它的故障也不会触发冷却。
3. **fallback 目标自己的健康状态不检查**（`model_router.py:204-211`）：可能降级到一个同样处于冷却期的池。
4. **metrics 口径不准**（`metrics.py`）
   - judge 的 subject 是 `…:judge:N`，`_service_role` 判成 `"service"`，所以 judge Critic 的 tokens 没有算进 critic，也不计入 `critic_turns`；而 `critic_turns` 又把 FAILED 重试也算进去了（`:34-40, 93-95`）。
   - `peak_observed` 只取状态转换时的观测值（`:88-92`），不是真实峰值。
   - 背压转换次数是全局数字，却写进了每个 Mission 的 metrics。
5. **拒绝审计不完整**
   - `tool_not_bound` 不写审计（`tool_gateway.py:181` 要求有 `attempt_id`），而解绑后僵尸 turn 的写入恰恰是最敏感的一类拒绝，违反 D6-7「每次拒绝」。
   - `ToolCallRejected.path`（`commit_service.py:279`）不截断，模型可以把内容塞进 path 字段写进事件。
6. **背压投影和队头阻塞**
   - RAISED 期间才创建的 Mission 只会收到 Cleared，收不到 Raised。
   - 多实例时，`_verify` 很快返回 False 的结果会每轮都占住 `verifier_workers=1` 的唯一名额，形成队头阻塞。
7. **异步验证收尾**
   - `_raise_if_verification_crashed`（`:499-513`）只抛第一个异常，其余已完成任务的异常被静默删掉。
   - `__aexit__` 用 `suppress(BaseException)`（`:288`）。
   - `run_pytest` 被取消时不 kill 进程组（`tool_gateway.py:126-135`），会留下孤儿 pytest 进程。
8. **Global 账户细节**
   - `open_account` 用 `ON CONFLICT DO NOTHING`（`budgets.py:143`），重启后换了 `global_budget` 配置会被静默忽略。
   - Global 的 `max_runtime_seconds` 经 `inherit_limits` 变成每个 Mission 各自计时，已不是 Global 维度。
9. **`_runtime_exhausted` 的位置**（`:2202`）：在 judge 之前检查，所有 Task 已完成但时钟刚好到点的 Mission 会被判失败；规划阶段（还没有 Task）不做运行时间检查。
10. **密钥守卫是直接失败，不是拒绝单个对象**
    - `assert_no_secrets` 新增的值扫描抛 `ValueError`，`build_worker_package`（`:2378`）没捕获，会让整个循环崩溃。知识内容或反馈里出现 `Bearer xxxxxxxxxxxxxxxx` 这类普通示例就会触发。
    - `guard_text` 对 `bearer_token` / `sk-` 的误报会让整份证据中途放弃写入。
    - 建议：包构建失败转为停掉该 Task；证据改为脱敏后写入。
11. **demo 和闭环测试偏弱**
    - plan §2 规定演示里要有一次 small → large 升级，实际 fixture 全部一次通过（我跑的这次两个 Mission 都只有 small 上的 attempt-1）。
    - closure 测试没断言确实发生了背压、没断言有上限、也没断言两个 Mission 交替推进。A→D 串行链让同时运行的 Attempt 最多只有 2 个，`max_running_attempts=3` 实际没被压到。
12. **其他**
    - Critic、Planner 的工具调用不计入工具调用维度。
    - Task 进入等待没有持久事件（只写了进程内的 `_note`），等待时钟随重启重置。
    - env 路径创建的两个 `httpx.AsyncClient` 没有关闭。
    - macOS 大小写不敏感的文件系统上，写 `Analysis.md` 可以绕过对 `analysis.md` 的只读保护。

---

## 逐项核对（没问题的部分）

1. **内核不变量**
   - `scheduler_state` 只经 `commit_service.py:239/329/361` 写入；`ToolCallRejected` 和 profile 健康都经 Commit Service 写。唯一写入者成立。
   - `state_machines.py` 只新增了两个 stop reason，§25 状态机没有加回边。
   - 两库补偿仍成立：每个池的 `creation_key` / `input_id` 幂等，加上 intent 冻结 profile。
   - `usage_ref` 由 `ON CONFLICT(usage_ref) DO NOTHING` 保证至多导入一次，并有测试断言；预算两层没有混用。
   - `Store.transaction()` 的持有者校验不会误伤：SDK 的工具 handler 是同一事件循环里的 async 调用（`consumer_adapter.py:275-285`），不在线程里；`on_rejected` 的事务是同步的，没有事务跨 await 的路径。
2. **背压**：滞回实现正确（`backpressure.py:163-200`）。`changes` 持久化且单调递增，重启后不会撞键。饥饿有上界：靠老化进入 tier 1（`allocator.py:164`）加 1 个探索槽。风险见 P1-4 和 P2-6。
3. **路由与执行池**：dispatch、collect、collect_after_stop、cancel、release、import_usage、critic 内联采集、recover 都走 `bridge_for`。回显按 intent 冻结的 model 核对，正确。升级新建带 `retry_of` 的 Attempt，正确。问题在 P0-1、P0-2、P1-4 和 P2-1/2/3。
4. **预算**：问题见 P1-1、P1-2、P1-3、P1-6。
5. **隔离与权限**
   - 网关按 §21.1 的顺序检查（`tool_gateway.py:202-283`），权限是四方交集并冻结进 intent。
   - 只读上游输入的判定：「受保护的上游文件 − 受保护的种子文件」，种子与上游重名时仍按上游产物保护；种子受保护文件继续走第 2 步的篡改检测。逻辑成立。
   - 在 SDK 工具执行路径里开事务是安全的（见第 1 条）。
6. **可观测**：每个 Attempt span 都带 prompt、context、retrieval、allocator、router 以及各验证层的版本，满足 S6-09。欠缺在 P1-5 和 P2-4。
7. **切片 E**：确实是两个 Mission 同时运行、背压真实触发、证据按 Mission 分目录。欠缺在 P1-7 和 P2-11。
8. **测试**：S6-01 到 S6-09 都有对应测试，但 P0-1、P0-2、P1-1、P1-2、P1-3、P1-4、P1-5 这些场景一个都没覆盖。

---

## 交付前必须处理

- **改代码**，每条都要带回归测试：P0-1、P0-2、P1-1、P1-2、P1-3、P1-4、P1-5。
- **实现或登记偏离**：P1-6。
- **登记偏离并加强测试**：P1-7。
- **P2**：建议在收尾（切片 F）时一并处理，或逐条登记遗留。
