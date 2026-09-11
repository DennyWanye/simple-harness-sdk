# 第 5 步（动态修改 Task DAG）实施计划与验收标准 · 独立评审 round 1

- 日期：2026-09-11 · 评审人：独立技术评审（只读，未改任何代码/文档）
- 评审对象：`plans/2026-09-11-agent-orchestrator/step05/plan.md`、`step05/acceptance.md`（定稿于提交 `933aaad`）
- 评审依据：Host 纲要 ORCH-BUILD-v1.0 §7（273–350 行）、§12、§13、附录 ORIGINAL-30；原文《Agent 编排层完整设计方案》§6.2、§7、§8、§12.3、§13、§15、§17、§18.6、§19、§25、§29；理论 02/03/05/06/07/10；已交付代码（第 2–4 步基线 `96abf25`）
- 评审环境说明：评审进行期间，第 5 步 **slice A 已提交**（`7981109`），slice B 正在工作区内修改（`context_builder.py` / `commit_service.py` / `assembly.py` / `role_templates.py` 为 modified）。下文中
  - 第 2–4 步代码引用一律以基线 `96abf25` 的行号为准；
  - 第 5 步 slice A 代码引用以 `7981109` 的行号为准（用于佐证"plan 的这条规定在实现里会落成什么样"）；
  - 结论仍然是**对 plan / acceptance 两份文档的评审**，不是对 slice A 的代码走查。

---

## 一、发现清单

### R1 · P0 · `retarget_dependencies`（BLOCKED 任务在位改依赖）破坏第 4 步确立的 `ordinal ≡ 拓扑序` 不变量

**发现**

plan D5-1 / D5-3 允许对 BLOCKED 任务**在位**改写 `dependency_ids`，acceptance S5-05 更把它写成必须观察的结果（"C 的依赖在位改到 B2"）。但新任务的 ordinal 只能追加在末尾（`ids.task_id(mission, len(tasks)+n)`），而被改线的老任务保留它原来的**较小** ordinal。于是会出现"ordinal 小的任务依赖 ordinal 大的任务"，`ordinal ≡ 拓扑序` 不再成立。

该不变量是第 4 步独立 review 的 P0（R2）并由 D4-7' 专门保住的：冲突任务被设计成"拓扑序末尾的叶子、从不成为任何既有任务的依赖"，正是为了不破坏它。第 5 步 plan 通篇未提及这个不变量。

**可观察的损坏**（`artifacts/versioning.py` 是唯一依赖方）：

- `ancestors()` 明确按 ordinal 排序并在 docstring 声称这是 "topological (ordinal) order"（`96abf25:artifacts/versioning.py:57-72`）；
- `merge_accepted()` 按 ordinal 顺序叠加产物，并用"后来者必须是先前者的传递依赖"来判定覆盖是否合法，否则抛 `ArtifactConflict`（`96abf25:artifacts/versioning.py:96-107`）。

按 §7.4 的演示图：A=1、B=2、C=3、D=4，新增 E=6、B2=7，C(3) 改依赖 B2(7)。此时若 C 改写了 B2 产出的任一路径（验证任务改实现文件是 D3-7' 明确支持的场景，靠 `outputs` 声明），则
1. 合并顺序按 ordinal 变成 C(3) 在前、B2(7) 在后 → **上游 B2 覆盖了下游 C**，覆盖方向反转；
2. 处理 B2 时 `existing.task_id = C` 不在 `closure[B2]`（B2 的祖先是 A、E）→ 抛 `ArtifactConflict` → `_judge` 走 `fail_mission(ARTIFACT_CONFLICT)`（`96abf25:orchestrator/event_handler.py:1586-1596` 段），或在 `_next_attempt` 里走 `stop_task(ARTIFACT_CONFLICT)`。

即：本步的核心演示路径存在把同一个 Mission 判成 `artifact_conflict` 失败的确定性通道，而 acceptance S5-05 只断言"C 的依赖在位改到 B2 / Mission 用 v2 完成"，不会暴露它。

**依据**

- `plans/.../step05/plan.md:33`（D5-1 `retarget_dependencies`）、`:35`（D5-3 "BLOCKED 任务的 `dependency_ids` 允许**在位**改写"）、`step05/acceptance.md:9`（S5-05）
- `plans/.../step04/journal.md:10`（R2 P0：动态 Conflict Task 破坏 `ordinal ≡ 拓扑序`）、`step04/plan.md:74`（D4-7' 保住该不变量的全部措辞）
- `96abf25:src/agent_orchestrator/artifacts/versioning.py:57-72`、`:82-111`
- slice A 佐证：`7981109:src/agent_orchestrator/orchestrator/commit_service.py:773-780`（新 ordinal 从 `len(tasks)` 往后追加）、`:848-858`（retarget 直接写入更高 ordinal 的新 id，老任务 ordinal 不变），且 `:777` 的注释 "ordinal ≡ order" 只覆盖**新节点之间**的相对顺序。

**建议处置（P0，必须在继续 slice B/C 之前定）**

二选一并在 plan 里显式登记：
1. **（推荐）** 把 `ancestors()` / `merge_accepted()` 的排序从"按 ordinal"改为"对当前图现算拓扑序（Kahn，ordinal 仅作并列打破）"，并在 plan 新增一条决定：`ordinal` 自第 5 步起**只是稳定标识与并列打破键，不再等价于拓扑序**；同步修订 D4-13'（分支归属"ordinal 最小的根"）与 `terminal_task()` 的"最后一个叶子"这两处同样按 ordinal 取序的地方。必须补一条决定性测试：下游任务改写上游 `outputs` 声明路径、且该下游任务是被在位改线的老任务。
2. 禁止"老任务在位改线到 ordinal 更大的新任务"，改走纲要 §7.4 原文的 C2 路线 —— 但注意 §25.1 没有 BLOCKED→CANCELLED 边，BLOCKED 的 C 无法被 `supersede_task` 替代（见 R2/R5），所以这条路目前走不通，除非同时新增"BLOCKED 任务的替代 = 不取消旧任务、只把旧任务留作孤儿"的语义（会与 §7.3"旧 Task 终态保留"的干净性冲突）。

---

### R2 · P1 · plan 把纲要 §7.4 的演示由 "C2 验证" 改成 "C 在位改依赖"，未登记偏离与理由

**发现**

纲要 §7.4 的端到端演示明确写的是 `Commit后：新增E确认格式 → B2实现 → C2验证`（即 C 也产生新实体 C2）。plan §2 与 acceptance S5-05 把它改成"新增 E、B2，**C 改依赖 B2**"。这不是措辞差异：它正是 R1 的直接成因，且改变了"用户在图上看到什么"（§7.4 第 333 行要求用户能看到"v1→v2、变更依据、旧产物和新增工作"）。

客观上 plan 的选择有其道理（C 当时是 BLOCKED，§25.1 无 BLOCKED→CANCELLED 边，无法 supersede），但这属于 ORCH §13 第 585 行规定的"任何不同选择都要单独登记，不可宣称是原文原句"。

**依据**

- 纲要 `agent-orchestrator-incremental-build-plan-phase2-zh-CN.md:329`（"新增E确认格式 → B2实现 → C2验证"）、`:333`
- `step05/plan.md:23`（§2 范围里的演示）、`step05/acceptance.md:9`（S5-05）
- `contracts/state_machines.py:120-128`（`_TASK[BLOCKED] = {READY}`，无 CANCELLED 出边）
- ORCH §13 `...:585`

**建议处置**：在 plan 新增"本步实施约定"小节（见 R12），登记该偏离：§7.4 的 C2 在本实现里表达为"BLOCKED 的 C 在位改前置"，原因是 §25.1 不允许 BLOCKED→CANCELLED；并写明由此产生的 ordinal 后果与 R1 的处置。

---

### R3 · P1 · 预算池用"已结算 tokens"而不是"已结算 + 仍在途预留"，与 ORCH §12.2 冲突，且 plan 内部两处自相矛盾

**发现**

D5-2 的预算池公式是：`Σ(非 CANCELLED 任务 max_tokens) + Σ(CANCELLED 任务已结算 tokens) + 系统预留 ≤ Mission max_tokens`。但被替代任务（B）在被 CANCELLED 的那一刻，它的在途 Attempt 往往还没结算：迟到结果的 usage 是在之后的周期由 `_collect_attempt` → `import_usage` → `settle` 才落账（acceptance S5-06 本身就要求"usage 导入、预留结算"发生在改图**之后**）。于是 commit 时刻计算出的"CANCELLED 已结算"偏小，池被高估，新任务可以超额拿到额度。

ORCH §12.2 对此有直接规定："在途 UNKNOWN 预算保持占用，完成核对后再 Settle；**不能为了新 Attempt 腾额度而清零旧费用**"。

plan 自身也不一致：D5-11 写的是"被替代任务的**预留结算后**其剩余额度回到池"（正确），D5-2 的公式写的是"已结算 tokens"（在 commit 时刻取值，即未结算部分被当成 0 返还）。

slice A 的实现取的是 `account.settled_tokens`，佐证该歧义会被实现成后者。

**依据**

- `step05/plan.md:34`（D5-2 池公式）、`:43`（D5-11）、`step05/acceptance.md:10`（S5-06）
- ORCH `...:549`（§12.2）
- slice A 佐证：`7981109:orchestrator/commit_service.py:756-761`（`settled[task.id] = int(account.settled_tokens)`）、`7981109:graph/changes.py:500-524`
- 代码里已有现成的"在途未知"判定：`96abf25:orchestrator/commit_service.py` 的 `has_unknown_usage(attempt_id)` 用法（accept 路径）

**建议处置**：把 D5-2 的池公式改为 `Σ(非 CANCELLED max_tokens) + Σ(CANCELLED 任务的 max(已结算, 仍占用的预留)) + 系统预留`，或更简单：对仍有未结算 Attempt 的 CANCELLED 任务，按其原 `max_tokens` 全额计入，直到该任务下所有 Attempt 结算完毕（届时自然释放，下一次提案就能拿到）。同时修正 D5-11 与 D5-2 的措辞一致性，并在 S5-06 里加一条断言："B 的迟到 usage 结算前，池不把 B 的未用额度算作可用"。

---

### R4 · P1 · `pause_task` 无回收路径，会让 Mission 永不终结（违反 ORCH §7.3 的显式禁令）

**发现**

D5-1 定义 `pause_task` 为"数据标记，前沿过滤，不改状态"（这本身符合 ORCH §13 "§19 某些地方说暂停/删除 Task 而 §25 无 PAUSED → 在调度控制或 Mission/分支政策中暂停"）。但 plan 没有规定"被暂停的任务最终如何收敛"。后果链是确定的：

1. paused 任务被前沿过滤 → 不再分配 Attempt；
2. 它既不是 CANCELLED 也不是 COMPLETED → `_decide` 的"live 全部 COMPLETED"永不成立；
3. `judge_mission` 要求全部 live 任务 COMPLETED，也永不成立；
4. `_decide` 返回 False、没有任何在途工作 → `run(until_idle=True)` **静默退出**，Mission 停在 ACTIVE，既不 COMPLETED 也不 FAILED，也没有事件说明"为什么停了"。

ORCH §7.3 第 319 行对这种结局有明文禁止："不能自行给原文加一个未定义的 AND–OR 证明逻辑，**也不能仅因所有曾创建节点未完成就永远不结束 Mission**"。

**依据**

- `step05/plan.md:33`（D5-1 `pause_task`/`resume_task`）
- ORCH `...:319`（§7.3）；原文 §19.1 停止条件
- `7981109:scheduling/allocator.py:36-45`（`and not task.paused` 的前沿过滤）、`:91-95`
- `7981109:orchestrator/event_handler.py:1327-1339`（`live` 判定）、`7981109:orchestrator/commit_service.py:2487-2510`（`judge_mission` 要求全部 live COMPLETED）
- acceptance 九个场景中无任何 pause 场景

**建议处置**：在 plan 增加一条决定，明确 pause 的终局，至少三选一并写进 acceptance：
(a) 暂停有 TTL/轮次上限，超时后由 Manager 决策转 `cancel_task` 或 `resume_task`；
(b) 当"全部 live 且未 paused 的任务都已 COMPLETED，但仍存在 paused 任务"时，视为 Mission 停止条件（`stop_reason=paused_frontier` 或按 §19.1"所有高价值 Frontier 已处理"判定），走显式 FAILED/COMPLETED 而不是静默退出；
(c) 本步不开放 `pause_task`，推迟到第 6 步（背压/路线停用）与 `search_controller` 一起做。

---

### R5 · P1 · "建议停止某条路线"在本步实际不可达；plan 未登记该限制

**发现**

原文 §15 把"建议停止某条路线"列为 Agent 可提的 Proposal 之一，纲要 §7.1 也把"暂停路线"列为本步新功能。plan D5-1 用 `cancel_task{task_id}`（READY/ACTIVE→CANCELLED）来承载它。但：

- 被取消任务的下游必然处于 **BLOCKED**（READY 要求依赖全 COMPLETED），而 §25.1 没有 BLOCKED→CANCELLED 边，所以下游不能一起取消；
- 合并图校验会因"某任务依赖被本提案移除的任务"整份拒绝（slice A `graph/changes.py:417-422` 的 `missing_dependency`，且 `cancels` 不像 `superseded` 那样被自动改线，见 `:410-414`）；
- 于是唯一可行姿势是"在同一提案里把每个 BLOCKED 下游 `retarget_dependencies` 到别处"——语义上很别扭（把验证任务改挂到无关任务上），plan 完全没有规定这个用法，acceptance 也没有场景。

结合 R4，本步实际上**既不能真正取消一条路线，也不能安全地暂停一条路线**。而 plan §2 明确把"暂停/取消路线"写进了"做"的范围。

**依据**

- 原文 `...:999`（§15 "建议停止某条路线"）、纲要 `...:279`（§7.1 "暂停路线"）
- `step05/plan.md:25`（§2 范围）、`:33`（D5-1 `cancel_task`）
- `contracts/state_machines.py:120-128`（无 BLOCKED→CANCELLED）
- slice A 佐证：`7981109:graph/changes.py:386-396`（`cancel_task` 仅 `SUPERSEDABLE`）、`:405-422`（removed 依赖导致整份拒绝）

**建议处置**：在 plan 里明确"路线停用"的完整规则并写进 acceptance：取消一个任务时，其**传递下游**必须在同一提案内一并处理（BLOCKED 的下游按什么规则收敛？建议引入系统自动行为："取消 X 时，仅依赖 X 的 BLOCKED 传递下游随之标记 `cancelled_cascade`"）；若本步不打算解决 BLOCKED 收敛问题，则把"取消路线"移出本步范围，plan §2 与 D5-1 同步收窄。

---

### R6 · P1 · 图变更路径缺少 D3-7' 的兄弟 `outputs` 冲突检查

**发现**

D5-2 列举了变更提案的整份校验项（环/深度/数量/预算/去重/漂移/工具/验证层），**没有** `outputs` 兄弟冲突检查。第 3 步的 `validate_graph` 是有这项的（互不依赖的两个任务不能声明同一 `outputs` 路径），它保证了 `merge_accepted` 在运行时不会遇到无法确定性合并的情况。

变更路径上新增任务（尤其是 `supersede_task` 的替代者 B2，它天然会声明与 B 相同的 `outputs`）不经过这项检查，冲突会推迟到派发或 Mission 判定时才以 `ArtifactConflict` 爆出来，落成 `stop_task(ARTIFACT_CONFLICT)` / `fail_mission(ARTIFACT_CONFLICT)`，而不是一次干净的 `TaskGraphChangeRejected`。

**依据**

- `step05/plan.md:34`（D5-2 校验清单）
- `96abf25:graph/task_graph.py:210-225`（`_sibling_output_conflicts`）、`:243-245`（`validate_graph` 里的调用）
- `96abf25:artifacts/versioning.py:100-107`（运行时的 `ArtifactConflict`）
- slice A 佐证：`7981109:graph/changes.py:298-550` 的 `validate_change` 中无该检查
- 注：需同时定义"CANCELLED 任务的 outputs 不参与冲突判定"，否则 B2 与被替代的 B 会互相判冲突

**建议处置**：D5-2 的校验清单补一条"`outputs` 兄弟冲突（合并后的活任务集合，跳过 CANCELLED）"；`test_graph_changes.py` 加一条决定性用例（新任务与某个互不依赖的活任务声明同一 outputs → `TaskGraphChangeRejected(reason=artifact_conflict)`）。

---

### R7 · P1 · S5-08 的"拒绝反馈进入下一次 Manager 包"在 plan 里没有触发路径，该验收项不可决定性观察

**发现**

acceptance S5-08 要求观察到"拒绝反馈进入下一次 Manager 包"，并指定测试 `test_manager_decisions.py::test_s5_08_feedback`。但 plan 的触发规则是：管理决策是**按 trigger 去重的一次性工作**，`subject = <mission>:manager:<trigger>`，"同一触发只建一次"（D5-6）。D5-10 只为 **CAS 冲突**定义了 `:retry-1` 的二次触发。

于是 depth / proposals / budget 三种拒绝之后，**没有任何触发器**会产生"下一次 Manager 包"，除非恰好出现另一个独立触发（另一个结果、另一次验证失败）。也就是说 S5-08 的这半句在当前设计下无法被确定性地观察到。

**依据**

- `step05/acceptance.md:12`（S5-08）
- `step05/plan.md:38`（D5-6 的 trigger 去重规则）、`:42`（D5-10 只在 CAS 场景定义 `:retry-1`）、`:60`（§5 风险里提到"拒绝反馈进入下一次 Manager 包（同 D3-2'）"，但没有落成决定）
- 对照第 3 步的做法：Planner 被拒后由 `record_planning_rejected` + `_create_planner_intent(ordinal+1)` 显式发起下一轮（`96abf25:orchestrator/event_handler.py:836-852`、`commit_service.py:1236-1248`）

**建议处置**：把"被拒 → 同一 basis 的下一轮 Manager 决策（trigger 后缀 `:retry-N`，N ≤ `max_manager_rounds`，包内携带 `TaskGraphChangeRejected` 的 reason/detail/剩余维度）"提升为 D5-x 正式决定，与 D5-10 的 `:retry-1` 合并成一套统一的重试编号；acceptance S5-08 相应写明可观察断言（第二个 manager intent 存在、其 config 里含上一次拒绝的 reason）。

---

### R8 · P1 · `graph_version` 存在第二个写入者（第 4 步的 `_open_conflict`），会误触 stale_base 且让自动重基失去判据、`graph_history.json` 断档

**发现**

D5-2 规定 `base_graph_version` 必须等于当前 `final_report.graph_version`，D5-12 规定"`graph_version` 只在 Mission 记录上 CAS"。但第 4 步已经有一个不走 `commit_graph_change` 的 `graph_version` 写入者：冲突首次出现时，`_open_conflict` 在 accept 事务内把 `graph_version` +1（并新增一个冲突任务），只发 `ConflictOpened` / `TaskCommitted`，**不写 `graph_changes` 记录、不记 `affected_task_ids`**。

后果：
- (a) 任何冲突开启都会让在途的 Manager 提案变成 `stale_base`；
- (b) D5-10 的自动重基靠"自 base 版本以来的已生效变更的 `affected_tasks` 是否与本提案相交"来判断，对冲突造成的版本跳变**查不到任何记录**，一律判为"不相交"从而自动重基 —— 这次重基的安全性其实没有被检验；
- (c) D5-13 的 `graph_history.json`（"每个 graph_version 的任务快照与变更依据"）与纲要 §7.4 第 333 行"用户应该能在任务图和事件时间线上看到 v1→v2、变更依据"对冲突造成的版本号会缺一条记录。

**依据**

- `step05/plan.md:34`（D5-2 CAS）、`:42`（D5-10 重基判据）、`:44`（D5-12）、`:45`（D5-13）
- `96abf25:orchestrator/commit_service.py:967-1096`（`_open_conflict`），特别是 `:1071-1082`（`graph_version+1` 且 `expected_version=mission.version`）与 `:1083-1109`（只发 ConflictOpened / TaskCommitted）
- slice A 佐证：`7981109:orchestrator/commit_service.py:734-746`（重基用 `list_graph_changes(since_version).affected_task_ids`）
- 纲要 `...:333`

**建议处置**：D5-x 补一条："凡是改变正式图形态的写入（含第 4 步的冲突任务开启）都必须写一条 `graph_changes` 记录（`source=conflict`，`affected_task_ids=[新冲突任务 id, 双方来源任务 id]`）"，使 CAS、自动重基判据与 `graph_history.json` 三者口径统一；acceptance S5-03 增加一条"冲突开启造成的版本跳变不会被误判为可自动重基"的断言。

---

### R9 · P1 · §29.3 的输入尺度未定义归一化区间；并且会抹掉冲突任务的绝对优先级（与 D4-7'/S4-03 相抵）

**发现**

D5-8 把 `mission_importance` 定义为"`task.priority` 归一到 [0,1]"，但**没有说归一化区间**（除以 Mission 内当前最大值？除以一个常量上限？）。若按"除以当前最大值"，则新增/取消任意一个任务都会改变**所有**任务的打分，S5-09 断言的确定性与 `allocator-v1` 的可复算性都会被破坏。

更实质的问题：第 4 步的冲突任务是 `priority=10.0`，代码注释写明其语义是"a dispute about a delivered fact is resolved **before anything else**"。归一化之后它只占 0.30 的权重，完全可能被 `waiting_age`（0.10）+ `uncertainty`（0.15）+ `unlock_value`（0.20）等项抵消，被一个长期等待的低价值任务排到后面 —— 而纲要 §7.2 第 307 行明确："风险、权限、可用预算和队列上限先作资格检查，**不能被高分抵消**"，第 4 步 D4-7'/S4-03 也依赖"争议先解决"。

D5-8 的资格检查清单（非 paused、依赖 COMPLETED、账户有剩余、并发上限、候选上限）里没有任何"任务类别"维度。

**依据**

- `step05/plan.md:40`（D5-8）、`step05/acceptance.md:13`（S5-09 "`allocator-v1` 打分记入事件 `AllocationDecided`"）
- 原文 `...:2214-2229`（§29.3）；纲要 `...:294-307`（§7.2 公式与资格检查）
- `96abf25:planning/manager.py:72`（`priority=10.0  # a dispute about a delivered fact is resolved before anything else`）、`step04/plan.md:74`（D4-7'）
- `96abf25:scheduling/allocator.py:32-45`（当前排序是 `(-priority, ordinal)`，冲突任务据此天然排前）

**建议处置**：
1. 明确归一化区间为一个**固定常量**（如 `PRIORITY_SCALE = 10.0`，`mission_importance = min(1.0, max(0.0, priority / PRIORITY_SCALE))`），写进"本步实施约定"并绑定 `ALLOCATOR_VERSION`；
2. 在 D5-8 的资格检查里增加"类别优先"规则：`kind=conflict` 的任务优先于一切 `kind=work`（与 `kind=synthesis` 的门控并列），不参与 §29.3 排序竞争；
3. D5-8 里补上 `AllocationDecided` 事件（目前只在 acceptance S5-09 出现，决定表与 D5-13 的证据清单都没有），并说明事件里记录哪些字段（各因子取值、`allocator_version`、`now` 基准）以保证可复算。

---

### R10 · P2 · 提案内多操作的适用顺序与互斥未定义，影响幂等回执的确定性（S5-07）

**发现**

D5-1 把"旧任务的依赖者中 BLOCKED 者在位改指向替代者"写在 `supersede_task` 的括号里（隐式的系统行为），同时又提供了显式的 `retarget_dependencies`。plan 没有规定：
- 同一提案里对同一 task 同时出现 `supersede_task` 与 `retarget_dependencies` 时怎么办；
- 显式 retarget 与 supersede 的自动改线冲突时谁赢；
- `supersede_task` 之后对**旧** task 再 `set_role` / `set_priority` 是否合法；
- 各类操作的应用顺序。

S5-07 要求"同一提案两次投递 → 同一回执、一次 `TaskGraphChanged`"，这要求提案的应用结果必须是**操作集合的确定性函数**；顺序/互斥未定义就等于把这一点交给实现顺序。

**依据**：`step05/plan.md:33`（D5-1）、`step05/acceptance.md:11`（S5-07）；slice A 佐证：`7981109:orchestrator/commit_service.py:848-894`（显式 retarget 先落，随后自动改线用 `task.id not in validated.retargets` 排他）—— 这是一个隐含的优先级规则，应该在 plan 里写出来。

**建议处置**：D5-1 补一张"操作应用顺序与互斥"小表（新增 → 显式 retarget → 自动跟随替代 → priority/pause/role → supersede/cancel 收敛 → 统一 `_unblock`），并声明"对同一 task 的互斥操作组合整份拒绝（`conflicting_operations`）"。

---

### R11 · P2 · 目标漂移规则 plan 与其落地不一致，且 "终结任务的新前置" 与 D4-8' 直接冲突

**发现**

D5-2 的漂移规则写的是：新任务必须 rationale 非空且（被某任务依赖 ∨ 替代某任务 ∨ **是终结任务的新前置**）。但终结任务在有综合任务时就是 `kind=synthesis`，而第 4 步 D4-8' 规定综合任务的 `dependency_ids` **永不修改**（这是第 4 步 review R1 的 P0 处置）。这两条不能同时成立。

落地时该分支被改成了"`parent_task_ids` 非空"（即"细化了某个既有任务"）。这是一个合理的替代，但含义不同：一个无人依赖、只声明了 `parent_task_ids` 的新任务是一个**新叶子**，它不会成为综合任务的依赖，产物进不了综合结果，却仍然必须 COMPLETED 才能通过 `judge_mission`。

**依据**：`step05/plan.md:34`（D5-2）、`step04/plan.md:73`（D4-8'）、`step04/journal.md:9`（R1）；slice A 佐证：`7981109:graph/changes.py:365-369`（禁止 retarget synthesis）、`:490-499`（漂移规则实际按 `parent_task_ids`）

**建议处置**：D5-2 把第三个分支改写为"`parent_task_ids` 非空（细化某个既有任务）"，并补一句说明新叶子的归宿：要么在 plan 里接受"新叶子的产物只进入 Mission 判定的整合树、不进入综合任务"，要么禁止新增无人依赖的叶子。

---

### R12 · P2 · 缺"本步实施约定"登记节（ORCH §13），一批参数与新枚举没有出处登记

**发现**

第 4 步 plan 有 `§6.1 本步实施约定（非原文原句；按 ORCH §13 登记）`。第 5 步 plan 只有 §1–§5，没有这一节，但 D5-8 里却写了"（§6.1 登记）"——指向一个本文件不存在的小节。

未登记的项至少有：`max_graph_depth=6`、`max_proposals_per_agent=3`、`no_progress_limit=2`（原文 §19.2 的举例是"连续 5 轮"）、`manager_after_failures=2`、`max_supersede_chain=2`、`aging_window_seconds=300`、`max_manager_rounds=4`、`ALLOCATOR_VERSION` 的七个输入尺度、`MissionStopReason.NO_PROGRESS` 与 `MANAGEMENT_EXHAUSTED` 两个新枚举、以及"`graph_version` 存在 Mission `final_report` 而非独立列"这个选择。

ORCH §13 第 585 行："以下不是替换原文，而是防止实现时出现两套不兼容语义。**任何不同选择都要单独登记，不可宣称是原文原句。**" §29 相关约定另见 ORCH §13 第 596 行（"§29 角色数量/比例/优先级是推荐起点…必须经负载和质量测试"）。

**依据**：`step05/plan.md:40`（"（§6.1 登记）"）、`step04/plan.md:86` 起的 §6.1 表；ORCH `...:585`、`...:596`

**建议处置**：新增 `§6 本步实施约定` 表，逐项登记上述参数/枚举/结构选择与原文位置；D5-8 的引用改指本节。

---

### R13 · P2 · 缺 §14.1 要求的"关闭开关"

**发现**：纲要 §14.1 第 605 行要求每一步的完成包同时提供"版本迁移**和关闭开关**"。第 4 步有 `knowledge_sharing` 开关（`CommitService(conflict_tasks=...)`，`96abf25:orchestrator/commit_service.py:179`）。第 5 步 plan 的 D5-13/D5-14 只提了 schema v3 与 CLI，没有动态改图的关闭开关；acceptance 的附加门槛也没提。

**建议处置**：D5-14 增加 `dynamic_graph`（或 `graph_changes`）开关：关闭时 Manager 触发退化为"只记录 proposed_tasks、不建 manager intent"，并在 acceptance 的附加门槛里加一条"关闭开关后第 3/4 步场景回归不变"。

---

### R14 · P2 · `max_manager_rounds` / `MANAGEMENT_EXHAUSTED` 只出现在"风险"里，没有进决定表与验收

**发现**：`max_manager_rounds`（默认 4/Mission）与 `stop_task(MANAGEMENT_EXHAUSTED)` 只写在 §5 风险段，不在 D5-1..D5-14 任何一条决定里，acceptance 九条也没有覆盖。而它是本步"有限次数"的最后一道兜底（30-17），也是新增的 `MissionStopReason` 值（D5-7 只声明了新增 `NO_PROGRESS`）。

**依据**：`step05/plan.md:39`（D5-7 只新增 NO_PROGRESS）、`:60`（§5 风险）；`contracts/state_machines.py:28-38`（枚举定义处）；ORIGINAL-30-17（纲要 `...:749`）

**建议处置**：把 `max_manager_rounds` 与 `MANAGEMENT_EXHAUSTED` 提升为 D5-7 的一部分，并在 S5-02 的判定里加一条"管理轮次耗尽后 Mission FAILED(`management_exhausted`)"。

---

### R15 · P2 · 无进展计数只算 `no_progress/failure`，`blocked` / `proposed_subtasks` 可绕过 30-17 的"有限次数"

**发现**：D5-7 的计数口径是"同一 Task 累计 `no_progress/failure` 结果 ≥ `no_progress_limit`"。而 D5-5 把 `blocked` 与 `proposed_subtasks` 也路由到"非 candidate 结果 → 管理决策"。一个反复返回 `blocked` 的 Worker 不会触发 D5-7 的降级/停止，只受 Task `max_attempts` 与（未入决定表的）`max_manager_rounds` 兜底。30-17 的措辞是"**连续无进展**时会降级或停止"，`blocked` 显然属于无进展。

**依据**：`step05/plan.md:37`（D5-5）、`:39`（D5-7）；纲要 `...:749`（ORIGINAL-30-17）；原文 §19.2

**建议处置**：D5-7 的计数口径改为"同一 Task 的全部非 candidate 结果（blocked / failure / no_progress / proposed_subtasks）累计"，acceptance S5-02 的用例相应用 `blocked` 也跑一遍。

---

### R16 · P2 · D5-5 的"Task 回 ACTIVE"与"非 candidate 不进验证"自相矛盾；非 candidate 结果里的 claims 归宿未定义

**发现**

(1) `record_result` 是"Attempt RUNNING→SUBMITTED，Task ACTIVE→VERIFYING"。D5-5 既然规定非 candidate 结果**不进验证**，那 Task 就不会先进入 VERIFYING，也就谈不上"回 ACTIVE"——正确表述应是"Task 保持 ACTIVE，不发生状态转换"。按字面实现会多出一次无意义的 ACTIVE→VERIFYING→ACTIVE 往返（虽然两条边都合法，但会污染时间线与 S5-01 的断言）。

(2) `record_result` 会把 `envelope.claims` 一律建成 `PROPOSED` Claim。D5-5 没有说非 candidate 结果的 claims 怎么处理——它们不会进验证，也就永远停在 PROPOSED，既不会 VERIFIED 也不会 REJECTED。原文 §14.3/§25.3 对 PROPOSED 的唯一出边是 UNDER_REVIEW。这会在 `final_report` / Blackboard 里留下一批悬空 Claim。

**依据**：`step05/plan.md:37`（D5-5）；`96abf25:orchestrator/commit_service.py:1850-1885`（claims 建 PROPOSED、Task ACTIVE→VERIFYING）；`96abf25:verification/deterministic_checks.py:125-126`（当前非 candidate 是被 `rule_check` 判 FAIL 的路径）；原文 §25.3

**建议处置**：改写 D5-5 的措辞为"Task 保持 ACTIVE、不转 VERIFYING"；并补一句 claims 规则（建议：非 candidate 结果不建 Claim，`claims` 原文只保存在结果 JSON 与 `OutcomeRecorded` 事件里，避免污染 Claim 状态机）。

---

### R17 · P2 · `stop_task` 是 Mission 级停止，plan 用它承载"分支级停滞处置"，读者会误解

**发现**：D5-7 与 acceptance S5-02 用 `stop_task(NO_PROGRESS)`。但 `stop_task` 的既有语义是"Task ACTIVE→FAILED **并把整个 Mission 置 FAILED + `_cascade_stop`**"。而原文 §19.2 / 纲要 §7.2 说的是"暂停或终止**分支**"。在 S5-02 的场景里（卡住的任务在关键路径上）结果一样，但 plan 应显式说明"本步只有 Mission 级停止，分支级停止靠 pause/cancel（且见 R4/R5 的限制）"，否则容易被读成"能只停一条路线"。

**依据**：`96abf25:orchestrator/commit_service.py:2241-2300`（`stop_task`：`fail_mission` + `_cascade_stop`）；原文 §19.2；纲要 §7.2；`step05/plan.md:39`、`step05/acceptance.md:6`

**建议处置**：D5-7 加一句语义说明；或引入分支级的 `fail_task_branch`（本步不建议，属范围蔓延）。

---

### R18 · P2 · 被替代任务的在途 SDK turn 未主动撤销（与 ORCH §12.1 的"先撤销新动作资格"有落差），且与 S5-06 的观察需求存在张力

**发现**：D5-3 只规定"其在途 Attempt CANCELLED（迟到结果只记历史，费用照常导入结算）"，没有规定是否主动取消对应的 SDK turn。ORCH §12.1 第 536 行："旧执行仍可能产生外部副作用 → **先撤销它的新动作资格，并核对在途动作**；新 Attempt 可排队但不得重复未知业务动作。" 现有代码里已有 `_cancel_turn` / `_release_attempt(cancel=True)` 能力。不撤销意味着被放弃路线继续烧 token（纲要 §7.2 的"减少 B 的 Agent"）。

同时要注意张力：S5-06 要观察"迟到提交只记历史"，就需要那次 turn 真的返回结果。

**依据**：ORCH `...:536`（§12.1）；`step05/plan.md:35`（D5-3）、`step05/acceptance.md:10`（S5-06）；`96abf25:orchestrator/event_handler.py:733-778`（`_cancel_turn` / `_release_attempt`）

**建议处置**：D5-3 明确取舍（建议：supersede/cancel 时**不**主动杀 turn，但把该 Attempt 标记为"无新动作资格"并在下一个周期按 `_collect_after_stop` 的同一路径收口；理由与 S5-06 的可观察性写进 plan）。

---

### R19 · P2 · `max_graph_depth` 只在变更路径生效，对第 3 步的初始图没有对应限制

**发现**：D5-2 要求"深度 ≤ `max_graph_depth`（默认 6）"，检查的是**合并后的整图**；但第 3 步的 `validate_graph` 对初始静态图没有深度检查。一个深度为 7 的 v1 图会让此后**任何**变更（哪怕只是 `set_priority`）都被 `depth` 拒绝。

**依据**：`step05/plan.md:34`（D5-2）；`96abf25:graph/task_graph.py:228-306`（无深度检查）；slice A 佐证：`7981109:graph/changes.py:291-295`、`:435-439`

**建议处置**：二选一并登记——(a) 深度检查改为"变更后的深度不得 > max(max_graph_depth, 变更前深度)"；(b) 同步给 `validate_graph` 加深度上限（会改变第 3 步的回归口径，需在 plan 里说明）。

---

### R20 · P2 · 去重签名把"提案 key"与"任务 id"混在同一空间比较，对依赖新节点的重复任务失效

**发现**：D5-2 的去重规则是"规范化 goal + 依赖 + 准则全同 → 拒绝"。但一个新节点的 `dependencies` 可能写的是**本提案的 key**，而既有任务的依赖是**任务 id**；两者在同一个签名里比较时永远不相等，于是"goal 与准则完全相同、依赖指向本提案新任务"的重复节点只会被判成"疑似"而不是"拒绝"。理论 10-13 / 纲要 §7.2 对 `deduplicator` 的要求是"**规范化/明确 ID 去重**；语义疑似重复只提出合并建议"——这里正是"明确 ID"这一半失效。

**依据**：`step05/plan.md:34`（D5-2）；`96abf25:graph/deduplicator.py:33-57`（签名 = `(frozenset(deps), sorted(criteria))`）；纲要 `...:288`；slice A 佐证：`7981109:graph/changes.py:449-458`（items 里既有 id 形式也有 key 形式的依赖）

**建议处置**：D5-2 明确"去重在**解析后的 id 空间**上做（先把提案 key 映射成将要分配的 id，再比较）"，或至少登记此限制。

---

### R21 · P2 · plan 未写明"变更提交后必须无条件重跑 `_unblock`"，这是新任务/改线任务能否离开 BLOCKED 的唯一通路

**发现**：现有 `_unblock(mission_id, unblocked_by=X)` 只重估"依赖里包含 X"的 BLOCKED 任务，且只在某个任务 accept 之后被调用；`unblocked_by=None` 的全量重估只在 `heal_mission` 里出现。图变更会产生两类"依赖当场即已全部 COMPLETED"的 BLOCKED 任务（新增任务的依赖全是已完成任务；在位改线后的新依赖集全是已完成任务），如果不在同一事务里做一次全量 `_unblock`，它们将**永远停在 BLOCKED**（没有后续 accept 会提到它们的依赖）。

plan D5-3 只写了"新任务先 BLOCKED（有依赖）/READY（无依赖）"，没有写这条。第 4 步 D4-7' 对冲突任务是明确写了的（"先以 BLOCKED 落库、同事务内经 `_unblock` 转 READY"）。

**依据**：`96abf25:orchestrator/commit_service.py:656-678`（`_unblock` 的过滤条件）、`:680-691`（只有 `heal_mission` 传 `unblocked_by=None`）；`step04/plan.md:74`（D4-7'）；`step05/plan.md:35`（D5-3）
（slice A 已经实现了 `_unblock(mission_id, unblocked_by=None)`：`7981109:orchestrator/commit_service.py:947`；本条是 **plan 文档漏写**，请回写而不是重新实现。）

**建议处置**：D5-3 补一句"变更事务的最后无条件执行一次全量 `_unblock`，并把 `unblocked` 写进回执与 `TaskGraphChanged` 事件"，与 D4-7' 的措辞对齐。

---

### R22 · P2 · §7.2 的两项能力被静默降级，未登记

**发现**
- §7.2 `planning/search_controller.py` 要求"支持有界展开、路线停用、**Best-of-N** 与探索/利用调整"。plan 用 `set_role`（explorer/exploiter）覆盖了探索/利用，但没有任何操作可以调整某个任务的 `candidates_per_task`（Best-of-N 的抓手，第 3 步已有该配置）。
- §7.1 要求 Manager 可提出"换角色/**模型**"。plan 把换模型降级为 `context.model_hint` 只登记、第 6 步路由（这与 ORCH §8.2 把 `runtime/model_router.py` 放第 6 步是一致的，属合理取舍）。

两项都没有作为显式偏离登记。ORCH §13 第 597 行只豁免了"复杂 MCTS 不提前加入"，没有豁免 Best-of-N。

**依据**：纲要 `...:279`（§7.1）、`...:286`（§7.2 search_controller 行）、`...:597`（§13）；`step05/plan.md:25`、`:27`、`:41`

**建议处置**：在"本步实施约定"里登记这两项偏离；若成本可控，建议把 `set_candidates{task_id, n}`（上限受 `max_concurrency`/预算资格检查）补进 D5-1 的操作词汇，这是 §7.2 明列的能力且实现代价很低。

---

### R23 · P2 · 演示的可观察性只覆盖"新图"，未覆盖 §7.4 第 333 行要求的"旧产物"

**发现**：§7.4 第 333 行："用户应该能在任务图和事件时间线上看到 v1→v2、变更依据、**旧产物**和新增工作，不只是最后一张看似全新的图。" D5-13 规定报告含"graph_version、变更列表、被替代任务与新任务"，`graph_history.json` 含"每个 graph_version 的任务快照与变更依据"——都没有"被替代任务已产出的旧产物/迟到结果"这一维。

**依据**：纲要 `...:333`；`step05/plan.md:45`（D5-13）；`step05/acceptance.md:15`（附加门槛）

**建议处置**：`graph_history.json` 或最终报告中，对每个 CANCELLED（被替代）任务列出其 Attempt、`ResultRejected(superseded)` 的 result 引用与已登记 artifacts，使"旧产物"在证据里可见。

---

### 二、已核查通过的项（未发现问题）

1. 术语定义与理论/原文一致：Frontier（plan:10 vs 理论 02-8 第 194–199 行、原文 §8.1）、Allocator/Scheduler 分工（plan:11 vs 原文 §8.2/8.3 第 590–593 行）、Manager/Planner 分工（plan:8 vs 原文 §7.1/7.2、理论 07-1/07-2）、Proposal/Commit（plan:9 vs 原文 §15、理论 10-14）、Retry ≠ 重复同一 Prompt（plan:13 vs 理论 06-5）、角色=搜索偏置（plan:14 vs 原文 §9.2）。
2. §29.3 公式权重原样保留（0.30/0.20/0.15/0.15/−0.05/−0.05 与 0.10），未擅自增删项；下游积压惩罚正确地留给第 6 步（纲要 §7.2 第 307 行）。
3. 未把 `judge_mission` 与理论 04-7 的 Judge 混用，也未把 Judge 与 Synthesizer 混用（第 4 步 §6.1 已登记，第 5 步未破坏）。
4. §25 无回边：D5-3 用的四条边（BLOCKED→READY、READY→CANCELLED、ACTIVE→CANCELLED、VERIFYING→ACTIVE→CANCELLED）都在 §25.1 图上；未出现 READY→BLOCKED、COMPLETED→ACTIVE、ACTIVE→BLOCKED。对 VERIFYING 任务的处理沿用了 `stop_task` 已有的两段式，与 ORCH §13 第 592 行一致。
5. "已完成任务只被引用、永不重跑"（ORIGINAL-30-05、纲要 §7.3 第 316 行）在 D5-3/D5-4 与 S5-05 中被正确表达。
6. Worker 不能直接改图（S5-01）与 ORCH §12.5 第 575 行、原文 §15 一致；Manager 无工具（D5-9）也符合"角色不扩权、权限由工具上限落实"。
7. 环检测每次加边必做（D5-2、S5-04）符合原文 §19.3 与 ORIGINAL-30-06。
8. 幂等（规范化哈希 + base 版本 → 同一回执）符合原文 §17.4 与既有 `commit_id`/receipt 机制。
9. 未引入 MCTS、未做多层 Manager、Manager 与 Search Controller 合并 —— 符合纲要 §7.2 第 286 行与 §13 第 597 行；未见明显的范围蔓延（`model_hint`、`ROLE_MIX_START` 都只登记不实现，做法正确）。
10. 迟到结果只记历史（D5-3、S5-06）与既有 `record_late_result` / `_collect_after_stop` 路径一致（`96abf25:orchestrator/commit_service.py:1903-1925`、`event_handler.py:601-656`），且费用仍归账，符合 ORCH §12.1 第 541 行。
11. acceptance 九条与纲要 §7.4 的 S5-01～09 一一对应，无遗漏、无擅自增删场景编号。

---

## 三、总体结论

**结论：有条件通过（Conditionally Approved）——必须先处置 R1，建议同时处置 R2–R9，再进入后续切片。**

- 方向与边界判断正确：主要矛盾抓住了"图变更必须是带基版本的事务性 Proposal/Commit、且 §25 不加回边"，范围切得克制（不做 MCTS、不做多层 Manager、不做模型路由），与 ORCH §7.2/§7.3/§13 的口径一致；acceptance 与 §7.4 的九条 MUST 严格对齐。这份 plan 的整体质量高于"能跑通"的门槛。
- **一个 P0 阻断项**：R1（在位改依赖破坏 `ordinal ≡ 拓扑序`）。这是第 4 步花了专门决定（D4-7'）保住的不变量，第 5 步在 plan 里完全没有提及，且其损坏路径（下游改写上游产物 → 覆盖方向反转 / 伪 ArtifactConflict → Mission 被判 `artifact_conflict` 失败）就落在本步的核心演示上。必须在 plan 里给出明确决定（推荐：现算拓扑序、ordinal 降级为稳定标识）并补决定性测试。
- **七个 P1**：R2（§7.4 演示被改写未登记）、R3（预算池口径与 ORCH §12.2 冲突、plan 内部矛盾）、R4（pause 无终局 → Mission 静默悬挂，违反 §7.3 明文禁令）、R5（"停止路线"实际不可达但写在范围内）、R6（缺 outputs 兄弟冲突检查）、R7（S5-08 的验收项没有触发路径，不可决定性观察）、R8（`graph_version` 第二写入者导致重基判据与图历史断档）、R9（§29.3 输入尺度未定义 + 抹掉冲突任务绝对优先级）。其中 R4、R7、R9 会直接影响验收的可判定性，建议与 R1 一并在本轮处置。
- **十四个 P2** 主要是文档纪律与表述一致性（缺 §6 实施约定登记节、缺关闭开关、操作顺序未定义、D5-5 措辞自相矛盾、两项 §7.2 能力静默降级、证据缺"旧产物"维度等），可在切片 E 收尾前统一回写。
- 附带提示：评审期间 slice A 已提交、slice B 在途。R21 属于"代码已做、plan 漏写"，请回写 plan 而不是重做；R3、R6、R9、R11 在 slice A 的实现里已按 plan 的（有问题的）字面落地，修 plan 的同时要一并改代码。
