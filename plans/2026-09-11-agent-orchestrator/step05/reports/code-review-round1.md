# 第 5 步 · 代码评审第 1 轮（只读）

- 评审对象：`git diff 96abf25..HEAD -- src/agent_orchestrator tests/orchestrator/step02..step05`（4 个实现提交 `7981109` / `9c13d6a` / `46d47d7` / `1ed414e` + `07327ef`）
- 锚：`plans/2026-09-11-agent-orchestrator/step05/plan.md`（§3 决定表、§6 修订 D5-2'…D5-15、§6.1 实施约定）、`acceptance.md`（S5-01…S5-09 + 附加门槛）、`journal.md` §1（23 条处置）；上位纲要 ORCH-BUILD phase2 §7.1–7.4/§12/§13；原文 §6.2/§7/§8/§13/§15/§17/§19/§25/§29.3
- 评审者环境：`.venv/bin/pytest tests/orchestrator` → **127 passed / 4 skipped**（基线绿）；下列 P0-1、P1-1 由评审者写的临时脚本实际复现（脚本未入库）
- 评审期间仓库前进到 `698e6fa`（`8acf0df` journal §4、`acb6398`+`ee70909` 真实运行 1、`5b3cd71`+`698e6fa` 真实运行 2 与 `max_output_tokens_ceiling` 暴露）。相对 `07327ef` 的代码增量只有 `runtime/assembly.py` 两个配置项与真实 provider 测试，不影响以下结论
- 结论概览：**1 P0 / 5 P1 / 14 P2**

---

## P0

### P0-1 `pause_task` 作用在 ACTIVE/VERIFYING 任务上 → Mission 永久悬在 ACTIVE，无终局、无停止原因

**发现**：`pause_task` 的合法性校验只排除终态，允许对 ACTIVE / VERIFYING 任务打暂停标记（`graph/changes.py:377-385`）。但三处消费 `paused` 的口径不一致：

- Allocator 把「paused 的 ACTIVE 任务」整个排除出可分配集合（`scheduling/allocator.py:216-217`），该任务再也拿不到 Attempt；
- `_decide` 的 live 过滤只排除 **READY/BLOCKED** 的 paused 任务（`orchestrator/event_handler.py:1706-1712`），paused 的 ACTIVE 任务仍算「必须 COMPLETED 的活任务」，判定永远不成立；
- `judge_mission` 同样只排除 paused 的 READY/BLOCKED，收尾时也只把 paused 的 **READY** 任务收成 `not_needed_paused`（`orchestrator/commit_service.py:2678-2698`）。

结果：既不推进也不判定，`_decide` 也不会因为它走停止级联（`event_handler.py:1718` 现在只看 FAILED），`_has_inflight()` 为假 → `run()` 空转两轮后正常返回，Mission 停在 ACTIVE、`stop_reason=None`。

**复现**（评审者实测）：单任务 Mission，Worker 返回 `blocked`，Manager 提案 `[{"op":"pause_task","task_id": <触发任务>}]` → `run()` 结束时 `MissionStatus.ACTIVE`、任务 `ACTIVE paused=True`。这是 Manager 模板明确列出的合法操作（`runtime/role_templates.py:174`「pause_task{task_id, reason}」），真实模型极易产出。

**依据**：`src/agent_orchestrator/graph/changes.py:377-385`；`src/agent_orchestrator/scheduling/allocator.py:216-217`；`src/agent_orchestrator/orchestrator/event_handler.py:1706-1718`；`src/agent_orchestrator/orchestrator/commit_service.py:2678-2698`

**违反**：plan §1 主要矛盾「每条路径都要有终局」；D5-4'（R4 的处置只覆盖了 READY/BLOCKED）；ORCH §7.3「不能……永远不结束 Mission」。

**建议处置（三选一，建议取 a）**：
- a. `validate_change` 把 `pause_task` 限制为 **READY / BLOCKED** 任务（「暂停一条尚未执行的路线」才是 D5-4' 的语义），对 ACTIVE/VERIFYING 任务要停只能 `cancel_task` / `supersede_task`；
- b. 若要保留对 ACTIVE 的暂停，则 commit 时先把它按 §25.1 收成 CANCELLED（同 `_cancel_task_entity`），不要留一个「既不跑也不算完」的中间态；
- c. 至少把 `_decide` / `judge_mission` 的 live 过滤与 Allocator 的资格口径统一（三处共用一个 `is_live_for_judgment(task)` 判定函数），并给「无可分配任务且无在途」加一个显式的 Mission 停止原因，杜绝静默悬挂。
- 无论取哪条，补一条决定性测试：`pause_task` 后 Mission 必须在有限步内进入 COMPLETED 或 FAILED。

---

## P1

### P1-1 `retarget_dependencies` 指向同一提案里被替代的旧任务 id → 提交后的图与校验过的图不一致，依赖者永久 BLOCKED

**发现**：`validate_change` 构造合并图时，把 retarget 出来的依赖也过一遍 supersede 映射（`graph/changes.py:419-423`：`deps = [superseded.get(d, d) for d in deps]`），所以「retarget C → [B]」＋「supersede B → B2」在校验期被看成「C → B2」，合法通过。但 commit 阶段只把依赖里属于**本提案 key** 的项翻译成新 id（`orchestrator/commit_service.py:850`：`key_to_id.get(d, d)`），旧任务 id `B` 不在 `key_to_id` 里，原样写回；紧接着的「跟随替代」补偿循环又显式跳过了出现在 `validated.retargets` 里的任务（`commit_service.py:874-878`）。

结果：C 的 `dependency_ids` 仍指向已 CANCELLED 的 B。`_unblock` 要求全部依赖 COMPLETED（`commit_service.py:1044-1047`），B 永远不会 COMPLETED → C 永久 BLOCKED → `judge_mission` 永远拒绝 → 与 P0-1 同类的静默悬挂。

**复现**（评审者实测）：§7.4 菱形图上提交 `[add B2(deps=[A]), supersede B→B2, retarget C→[B]]`，提交成功，`C.dependency_ids == (B.id,)`、`B.status == CANCELLED`。

Manager 的包里 `affected_subgraph` 就是原样列出 C 的当前 `dependencies:[B]`（`event_handler.py:1279-1292`），模型照抄旧 id 做 retarget 是最自然的写法。

**依据**：`src/agent_orchestrator/graph/changes.py:419-423`；`src/agent_orchestrator/orchestrator/commit_service.py:848-858`、`870-878`

**建议处置**：commit 的 retarget 解析改为与校验同一套映射——`resolved = tuple(key_to_id.get(superseded_new_id.get(d, d), superseded_new_id.get(d, d)) for d in deps)`，即先过 supersede 映射再过 key→id 映射；或者更保险，在 `validate_change` 里直接拒绝「retarget 的依赖里出现本提案要 supersede/cancel 的任务 id」并要求 Manager 写替代者的 key。补一条决定性测试覆盖这两种写法。

### P1-2 D5-5 / D5-6 / §6.1 登记的三个 Manager 触发只实现了一个，`manager_after_failures` 是死配置

**发现**：`_request_management` 全仓只有**一个**调用点——非 candidate 结果路径（`event_handler.py:983-1000`）与被拒后的 `:retry-1`（`event_handler.py:1568`）。计划明确登记的另外两个触发没有落地：

- 「`VerificationFailed` 累计 ≥ `manager_after_failures`（默认 2）」（plan D5-6、§6.1「Manager 触发时机」）：`manager_after_failures` 只在 `runtime/assembly.py:82` 定义、在 `assembly.py:174` 打进 config 快照，**没有任何读取点**（全仓 grep 仅这两处）。
- 「PASS 且带 `proposed_tasks` 的结果在 accept 后也触发管理决策」（plan D5-5 尾句、S5-01 的一半语义）：accept 路径不触碰 `proposed_tasks`（`event_handler.py` 中 `proposed_tasks` 仅出现在 1362 行的 Manager 包构造里）。

**连带后果**：`no_progress_count` 把 `verification_failed` 计入（`commit_service.py:2419-2428`），但 `_enforce_no_progress` 只能从 Manager 收集路径到达（`event_handler.py:1450/1534/1576`）。所以「反复验证失败 → 换角色/拆小/Failure Analyst」这条原文 §19.2 的主线在本步**根本不会触发**，`NO_PROGRESS` 也只对 `no_progress`/`failure` 结果生效。§29.2 的 `failure_analyst` 角色因此没有任何自动进入路径。

**依据**：`src/agent_orchestrator/orchestrator/event_handler.py:983-1000`、`1450`、`1534`、`1576`；`src/agent_orchestrator/runtime/assembly.py:82`、`174`；`src/agent_orchestrator/orchestrator/commit_service.py:2419-2428`

**建议处置**：在 `fail_result` 之后的收集点（`_verify` 判 FAIL 分支）按 `no_progress_count(task) >= manager_after_failures` 触发 `_request_management(trigger=f"stall:{task_id}:{n}")`（plan D5-6 已给出该 trigger 形状），并在 accept 分支对带 `proposed_tasks` 的 PASS 结果触发一次。各补一条决定性测试。若确定本步不做，必须回写 plan §6 与 acceptance，并把 `manager_after_failures` 从配置里删掉，不要留死旋钮。

### P1-3 `_collect_manager` 先结算 intent 再提交变更，崩溃窗口会静默丢掉整个管理决策（与 `_collect_plan` 相反）

**发现**：`_collect_manager` 在解析出提案后立刻 `_settle_intent(intent, "SETTLED")`（`event_handler.py:1515-1516`），之后才调用 `commit_graph_change`（`event_handler.py:1547`）。对照 planner：`_collect_plan` 是**先 commit 成功再 settle**（`event_handler.py:897-918`）。

崩溃落在这两步之间时：intent 已 SETTLED → `recover()`/`_cycle` 不会再收集它；变更事务回滚 → 图不变；`ManagementDecided` 也没写。而 trigger 已经被 `get_intent_for_subject(subject)` 去重（`event_handler.py:1318-1321`），同一 trigger 永不再开决策。任务回到旧计划重试，用户在事件时间线上看到一个「请求了管理决策但没有任何结论」的空洞。

**依据**：`src/agent_orchestrator/orchestrator/event_handler.py:1515-1516` vs `897-918`

**违反**：plan D5-12「manager intent 与 planner intent 同路径」；原文 §16.4。

**建议处置**：把 `_settle_intent(SETTLED)` 移到 `commit_graph_change` 成功之后（拒绝分支已经是 `record_management_decided(rejected)` 再处置，可保持）；`commit_graph_change` 本身的幂等回执（`commit_service.py:720-726`）保证重放安全。补一条 fault-injection 测试（仓内已有 `self._fault(...)` 机制）覆盖「commit 前崩溃 → 重启后同一提案仍被提交一次」。

### P1-4 S5-09 验收要求的 `AllocationDecided` 事件不存在；「在 `aging_window` 内获得执行」实际是「等满一个窗口之后」

**发现**：
- acceptance S5-09 写「`allocator-v1` 打分记入事件 `AllocationDecided`」。全仓没有任何 `AllocationDecided` 事件（grep 无命中）。打分实际只冻结进 Attempt 的 dispatch intent config（`event_handler.py:1938-1945`），测试也是从 intent config 断言的（`tests/orchestrator/step05/test_allocator_priority.py:216-220`）。事件时间线上看不到分配决策。
- acceptance S5-09 写「低优先级 READY 任务因 `waiting_age` 上升**在 `aging_window` 内**获得 Attempt」。实现的饥饿保护是 `tier = 1 if waiting >= 1.0`（`scheduling/allocator.py:163`），即**等满一整个窗口**才提升；窗口内只靠 0.10 的权重，压不过高优先级。测试自己把这一点写死了：`now=250/300` 时低优先级任务**拿不到**、`now=301` 才拿到（`test_allocator_priority.py:171-180`）。journal §2 切片 C 的描述（「等待满窗口的饥饿保护」）与 acceptance 文本不一致。

**依据**：`src/agent_orchestrator/scheduling/allocator.py:163`、`179`；`src/agent_orchestrator/orchestrator/event_handler.py:1940-1945`；`tests/orchestrator/step05/test_allocator_priority.py:164`、`171-183`、`216-220`

**建议处置**：补 `AllocationDecided` 事件（每轮 `allocate` 后 emit 一次 `plan.to_json()`，含 `allocator_version` 与每个候选的 parts），或把 acceptance S5-09 改写成「打分冻结进 Attempt 的 dispatch intent（可回放）」并在 §6.1 登记；`aging_window` 的语义二选一并统一到 acceptance / plan / journal 三处。

### P1-5 附加门槛「真实模型演示记录」尚未闭合：run1 没有触发任何 Manager 路径

**发现**：两次真实运行都没有产生图变更，`graph_version` 均为 1：

- 运行 1（`reports/real-dynamic-dag-run1.md`）：Planner 把格式确认前置了，没有 Worker 返回 blocked，Manager 路径未被触发；
- 运行 2（`reports/real-dynamic-dag-run2.md`）：Worker **确实**返回了 blocked，`OutcomeRecorded → ManagementRequested` 走通，但 Manager 的 turn 因 flash 把 8192 输出上限全花在推理上返回空（`provider_empty_response`）→ `ManagementDecided(unusable)`，任务回落到自身重试。报告自述「运行 3 重跑以获得『Manager 提案 → 改图 → 同一 Mission 完成』的真实证据」。

acceptance 附加门槛要求「真实模型（deepseek-flash）演示记录（至少『执行中改图后同一 Mission 完成、已完成任务未重跑』）」，该门槛**尚未闭合**。当前 `commit_graph_change` 的完整闭环**只被 fixture 脚本验证过**（`testing/fixtures.py:1268-1305` 的 `recorder_manager_change` 是写死的提案），真实模型能否产出通过整份校验的 `<graph_change_proposal>` 仍无证据。另外 opt-in 测试本身不强制发生改图：`test_real_provider_dynamic_dag.py` 只在「若发生了变更」时才检查被替代任务。

**顺带**：运行 2 同时给 P1-2 提供了真实证据——task-3 与 task-5 各有一次 `verification_failed`（见该报告的 Attempt 列表），事件序列里出现了 `VerificationFailed`，但**没有**对应的 `ManagementRequested`；`ManagementRequested` 只出现在 task-1 的 blocked 那一次。

**依据**：`plans/.../step05/reports/real-dynamic-dag-run1.md`、`real-dynamic-dag-run2.md`；`plans/.../step05/acceptance.md` 附加门槛；`tests/orchestrator/step05/test_real_provider_dynamic_dag.py`

**建议处置**：完成运行 3（已放宽 `max_output_tokens_ceiling` 到 32768，`5b3cd71`），把「`graph_version ≥ 2`」+「已 COMPLETED 任务的 attempt 数在改图前后不变」作为该运行记录的显式判定写进 `journal §4`。若 flash 上 Manager 仍不稳定，应记录为已知限制并在 plan §5 风险表补一行，而不是把门槛默认通过。

---

## P2

### P2-1 `affected_task_ids` 不含被隐式改指向的依赖者与 `unblocked` 任务，重基的账不完整
`validated.affected_task_ids` 只汇总提案显式点名的任务与新 key（`graph/changes.py:563-572`），落进 `graph_changes` 行的也是这一份（`commit_service.py:961`）。但一次 supersede 会隐式改写 BLOCKED 依赖者的依赖（`commit_service.py:870-894`），`_unblock` 还会把若干任务转 READY——这些任务不在 `affected_task_ids` 里。`_commit_graph_change` 的重基判据正是 `touched & change.referenced_task_ids()`（`commit_service.py:736-746`），因此一个旧基版本的提案可能被判定为「不相交」而自动重基。目前危害被「重基后对当前图重新整份校验」兜住（依赖 CANCELLED 任务会在 `graph/changes.py:426-437` 被拒），但账本身是不完整的。**建议**：把隐式改写的依赖者与 `unblocked` 并入 `affected_task_ids`，或在 §6.1 登记「affected 只记显式操作，安全性由重基后的再校验保证」。

### P2-2 `ancestors` / `merge_accepted` 并未按 D5-4 跳过 CANCELLED 任务
D5-4 写「`merge_accepted` 的祖先集合跳过 CANCELLED（被替代任务的产物不进入下游）」，但 `artifacts/versioning.py:57-75、108-130` 里没有任何 CANCELLED 过滤，实际靠「supersede 时把依赖者改指向替代者」间接保证祖先集合里不出现旧任务。一旦 P1-1 那类改指向失效（或将来出现别的遗留边），被替代任务的产物就会重新进入下游合并。**建议**：在 `ancestors` 的 `visit` 里显式跳过 `TaskStatus.CANCELLED`，与 D5-4 文本对齐（成本极低，且是纵深防御）。

### P2-3 `graph_history` 的历史版本快照用的是任务的**当前**依赖边
`graph_history` 的成员判定按 `context.graph_version <= version` 做得对，但 `task_view` 直接读 `task.dependency_ids`（`observability/graph_history.py:25-40`），而改指向是在位覆盖的。于是 v1 的快照里 C 显示依赖 B2 而不是 B——ORCH §7.4 要求用户看到的「v1→v2」在边这一维上失真。闭环测试只断言了成员集合（`test_dynamic_dag_closure.py:155-161`），没有覆盖边。**建议**：从 `graph_changes` 行的 operations 反推每个版本的边，或在 `task_view` 里注明 `dependencies_current` 并额外给出该版本的边。

### P2-4 冲突任务与 step-2 单任务没有 `context.graph_version`，单任务路径也没有 `ready_at`
`conflict_task` 的 context 不含 `graph_version`（`planning/manager.py:78-85`），`graph_history` 因此把它算进 v1（`graph_history.py:31`），即使它是在 vN 才被系统开出来的（`commit_service.py:1453-1470` 明确把 graph_version +1）。step-2 的单任务提交路径建 READY 任务时没有写 `ready_at`（`commit_service.py:396-408`），该路径的 `waiting_age` 恒为 0。**建议**：两处补上（`ready_at=self._store.now`、`context={"graph_version": graph_version}`）。

### P2-5 `ValidatedChange.is_noop()` 是死代码，D5-7 的「空提案/只 set_priority」判定在 `_collect_manager` 重复实现且口径不同
`is_noop()`（`graph/changes.py:278-288`）全仓无调用点；实际判定写在 `event_handler.py:1525`（`not change.operations or all(op == "set_priority")`），两边口径不同：`is_noop()` 把 `resumes` 也算作「不算改变」，而 `_collect_manager` 会把「只有 pause_task / resume_task」的提案当成真正的改变继续提交（与 P0-1 直接相关）。**建议**：删掉死代码或让 `_collect_manager` 改用 `validate_change` 的结果判定，口径唯一。

### P2-6 D5-7'「空提案受 `no_progress_limit` 约束」对 `blocked` 触发不成立
`no_progress_count` 只数 `outcome_no_progress` / `outcome_failure` / `verification_failed`（`commit_service.py:2419-2428`），`blocked` / `proposed_subtasks` 按设计不计。于是「Worker 反复 blocked + Manager 反复空提案」这条路上 `no_progress_limit` 永远不触发，只有 `max_manager_rounds=4` 兜底（`event_handler.py:1322-1335`）。终局是有的（MANAGEMENT_EXHAUSTED），但与 D5-7'/R15 的文本「空提案仍受 `no_progress_limit` 约束」不符。**建议**：要么给「同一 trigger 的空提案」单独计数，要么把 D5-7' 的文本改成「由 `max_manager_rounds` 兜底」。

### P2-7 未写 `budget` 的 `add_task` 会按 `default_share` 吃掉整个剩余预算池
`default_share = max(1, remaining // len(nodes))`（`graph/changes.py:552`），没有显式 `max_tokens` 的新任务直接拿到剩余池的等分，`total_new ≈ remaining` 仍然过校验。之后任何新的 `add_task` 都会被 `budget` 拒绝，`stop_task` 也难以再要额度。fixtures 里的提案都写了预算（`testing/fixtures.py:1286、1300`），所以测试覆盖不到；真实模型省略 `budget` 是常见写法。**建议**：给缺省份额加上限（如 `min(default_share, mission_pool // max_tasks)` 或一个配置常量），或直接要求 `add_task` 必须带 `budget`（`NewTaskNode.from_json` 的 `missing` 集合里加上）。

### P2-8 `dynamic_graph=False`（D5-15 关闭开关）没有任何测试
`event_handler.py:1315-1317` 是唯一实现点。关掉后非 candidate 结果仍会走 `record_outcome_result`（Attempt→RETRY_WAIT、Task 留 ACTIVE），随后只能靠 `budget.max_attempts` 收敛；`_enforce_no_progress` 完全不可达。**建议**：补一条决定性测试（关闭开关后不产生 manager intent、Mission 仍有终局）。

### P2-9 `max_supersede_chain` 只有单测，且用人造 context 绕过了真实递增路径
`test_graph_changes.py:340-363` 用 `replace(deep, context={"supersede_depth": 2})` 人工构造深度。真实递增发生在 commit 的新任务上（`commit_service.py:800-803`），端到端「B→B2→B3 第三次被拒」没有覆盖。acceptance S5-02 把「替代链 ≤ max_supersede_chain」列为必须观察项。**建议**：补一条两次 supersede 后第三次被拒的端到端测试。

### P2-10 `terminal_task` 仍按列表序取最后一个叶子，没有用 D5-3' 的拓扑序
D5-3' 把 `ancestors`/`merge_accepted` 改成按边的拓扑序，但 `terminal_task`（`planning/manager.py:157-165`）依旧是 `leaves[-1]`（即 ordinal 最大的叶子）。改图会给新任务分配更大的 ordinal，一个新增的旁支叶子可能因此抢走 `terminal_task_id`。现有测试恰好落在 D 上（`test_graph_changes.py:110-112`）没有暴露。**建议**：`terminal_task` 也走 `topological(...)`[-1]，或在 §6.1 登记「终结任务只按 ordinal，改图不改它」。

### P2-11 Allocator 的资格检查缺 D5-8 写的「任务账户有剩余」
plan D5-8 的资格清单是「非 paused、依赖 COMPLETED、**任务账户有剩余**、并发上限、候选上限」。`allocate` 的 eligible 只做了前两项与后两项（`scheduling/allocator.py:216-217`、`231-243`），预算下沉到 `_next_attempt` 的 `BudgetExhausted` 处理。行为上没问题（超预算会变成 stop），但「资格检查在前，不被高分抵消」这条原文 §8.2 的约束在分数排序阶段不成立。**建议**：把账户剩余并入 eligible，或在 §6.1 登记这一下沉。

### P2-12 ORCH §7.2 的模块落点与实现不一致且未登记
纲要 §7.2 把「事务性应用 Task DAG Proposal；记录变更前版本、依据 result_id、受影响任务与依赖」放在 `graph/task_graph.py`；实现放在 `graph/changes.py` + `orchestrator/commit_service.commit_graph_change`。`planning/search_controller.py` 未建（原文允许「初期与 Manager 合并」，plan §0 已登记，这一半没问题）。plan §6.1 的登记表里没有这条模块落点偏差。**建议**：在 §6.1 补一行登记（与 R22 同样的处理方式）。

### P2-13 `duplication_score` 用的是精确 normalise 后的 goal 相等，不是「疑似重复」
plan D5-8 写「`duplication_score` = 疑似重复数 / 任务总数」，实现用 `normalise_goal` 完全相等分组（`scheduling/allocator.py:141、160`），而 `graph/deduplicator.find_duplicates` 另有 `suspected` 的语义。两处「重复」口径不同。**建议**：统一用 `find_duplicates` 的 suspected 集合，或在 §6.1 把尺度定义改成「规范化 goal 完全相同」。

### P2-14 journal §4 的测试计数与实测不符
journal §4 记「step02–05 126 passed / 4 skipped」，评审者实测 `127 passed / 4 skipped`。**建议**：以最终代码为准回写（若因本轮处置改动代码，一并重算）。

---

## 总体结论

**结论：条件通过（Conditionally Approved）——P0-1 与 P1-1 必须修复并补测试后方可收尾；P1-2 / P1-3 / P1-4 / P1-5 需要「修复」或「明确降级并回写 plan/acceptance」二择一。**

做得扎实的地方（不必改）：

- **事务性与原子性**：`_commit_graph_change` 全程单事务，所有 `GraphChangeRejected` 都在任何写入之前抛出，拒绝事件在事务外补写，正式图确实不变（`commit_service.py:698-978`；`test_graph_changes.py:163-187` 验证 graph_version 与任务数不变）。
- **幂等回执**：`change_id = commit_id(proposal_hash, base_version)` 先查回执后做一切（`commit_service.py:720-726`），S5-07 双投递同回执、一次 `TaskGraphChanged`，验证充分。
- **§25.1 不加回边**：所有状态写入都过 `next_task`/`assert_task_transition`（`orchestrator/state_machine.py:39-42`），`_cancel_task_entity` 的 VERIFYING→ACTIVE→CANCELLED 两条合法边、in-flight 结果置 REJECTED、Attempt 收成 CANCELLED、迟到结果走既有 `record_late_result(superseded)` 都正确（`commit_service.py:989-1029`；S5-06 闭环断言到 reservation 已 SETTLED）。
- **预算池（R3）**：`committed` 取 `settled + reserved`（`commit_service.py:757-761`），与 D5-2' 一致；S5-08 的 `remaining=` 与维度都进了拒绝详情，反馈确实回到下一次 Manager 包（`test_manager_decisions.py:267-269` 断言的是模型真正看到的 `rejections`，不是事件）。
- **§29.3 权重原样**、输入尺度可复算、打分冻结进 intent（`allocator.py:32-41`、`153-164`；`test_allocator_priority.py:96-143` 逐项断言 parts 与总分，确定性充分）。
- **测试整体不偷渡结论**：`recorder_manager_change` 从 `affected_subgraph` 里反查 A 的 id 而不是写死（`fixtures.py:1273-1275`）；S5-05 用「变更前后的 attempt 数 / provider 调用数 / `BudgetReserved` 事件序号」三重证据证明已完成任务未重跑（`test_dynamic_dag_closure.py:108-131`），强度足够；S5-06 断言到 `ResultRejected(superseded)` + intent SETTLED + reservation SETTLED，是真判定不是形状检查。

主要风险集中在**「暂停 / 改指向两类操作缺终局保证」**（P0-1、P1-1，两条都能让 Mission 静默悬在 ACTIVE，恰好落在 plan §1 声明的主要矛盾上），以及**「计划登记的触发时机只落地了三分之一」**（P1-2，使 §19.2 的停滞处置主线与 `failure_analyst` 角色在本步不可达）。这两类都不是实现细节的瑕疵，而是「正式计划的每一次改变都可拒绝、可重放、且每条路径有终局」这条不变量上的缺口，建议在本步收尾前处置。
