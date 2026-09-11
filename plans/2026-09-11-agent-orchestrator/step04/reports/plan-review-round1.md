# 第 4 步 plan/acceptance 独立评审 · 第 1 轮

- 日期：2026-09-11
- 评审对象：`plans/2026-09-11-agent-orchestrator/step04/plan.md`、`step04/acceptance.md`
- 评审依据：Host 纲要 ORCH-BUILD-v1.0 §6（229–272 行）、§12–§14（523–650 行）、附录 ORIGINAL-30；原文设计 §10/§11/§13/§14.3–14.4/§15/§20.3/§21.3/§23.3/§25.3/§26.4–26.5；理论 04/10/13 §15；SDK main 已交付代码 `src/agent_orchestrator/`
- 评审方式：只读代码，不改代码。每条发现均给出可核实出处。

---

## P0（阻塞：与已交付代码不变量直接冲突，按现文实现必定抛异常或静默破坏不变量）

### R1 · P0 · D4-8 的"综合任务转 BLOCKED"是非法 Task 转换

**发现**：D4-8 写"冲突任务开启时若综合任务尚无在途 Attempt（READY/BLOCKED）→ 把冲突任务加入其依赖（版本 CAS，转 BLOCKED）"；acceptance S4-05 把"综合任务依赖全部叶子（含冲突任务）"列为必须观察结果。但 READY→BLOCKED 在 §25.1 里没有这条边，代码里也没有。

**依据**：
- `src/agent_orchestrator/contracts/state_machines.py:118-126`：`_TASK[TaskStatus.READY] = frozenset({TaskStatus.ACTIVE, TaskStatus.CANCELLED})`，注释写明"§25.1 exactly"。`next_task`（`orchestrator/state_machine.py:39`）会调用 `assert_task_transition`，READY→BLOCKED 直接抛 `IllegalTransition`。
- 原文 §25.3 上文的 §25.1 状态机（`agent-orchestration-layer-complete-design.md:1805-1822`）只有 `BLOCKED --> READY`，无回边。
- ORCH §13（`...phase2-zh-CN.md` 第 13 节表格）明确："§25 Task 终态无回边，但 §6 要求动态细化 → 第 5 步采用旧 Task 终态保留、新工作实体引用替代关系的方案，**不悄悄加 ACTIVE→BLOCKED/COMPLETED→ACTIVE**"。加 READY→BLOCKED 属同一类"悄悄加回边"。
- 触发路径必然发生：冲突在最后一个叶子的 accept 事务里被检出，而同一事务内 `_unblock`（`orchestrator/commit_service.py:601-623`）刚好把综合任务从 BLOCKED 置为 READY。

**建议处置**：二选一，并在 plan 里登记为实施约定：
1. 不动综合任务的状态机：综合任务在 Graph Commit 时就把"冲突任务占位依赖"表达为**门控条件**（例如 Task 上新增 `gate` 字段：`mission 内无未关闭 conflict`），Scheduler/Allocator 在 READY 任务上再做一次门控过滤，不建 Attempt 但也不改状态；事件记 `SynthesisGated`。
2. 或者把"加依赖"改成"新工作实体"：取消（READY→CANCELLED，合法边）原综合任务，提交一个依赖含 K 的**新**综合 Task，用 `supersedes_task` 关系记录替代，与 ORCH §13 第 5 步方案一致。
   注意方案 2 会改变 ordinal（见 R2），需一并处理。

### R2 · P0 · Conflict Task 取"下一个 ordinal"破坏 `ordinal ≡ 拓扑序` 不变量

**发现**：D4-7 写 Conflict Task 用"下一个 ordinal"；D4-8 又让它成为**更早 ordinal** 的综合任务的依赖。已交付代码把"ordinal 顺序 = 拓扑序"当成硬不变量用于产物合并和终结任务选择。

**依据**：
- `src/agent_orchestrator/artifacts/versioning.py:57-71`：`ancestors()` docstring 直接写"All transitive dependencies of `task_id` in **topological (ordinal) order**"，实现是 `sorted(..., key=lambda task: _ordinal(task.id))`；`_ordinal` 从 `mission-xxxx:task-N` 里取 N。
- `versioning.py:82-112` `merge_accepted()`：按 `_ordinal` 升序覆盖同路径产物，"后者覆盖前者只在存在依赖链时合法"。K 的 ordinal 大于 S，一旦两者产出同路径，K 会覆盖 S 的成果（方向反了）；若不在依赖闭包内则直接抛 `ArtifactConflict` → `_next_attempt` 里会 `stop_task(ARTIFACT_CONFLICT)` 让整个 Mission 失败（`orchestrator/event_handler.py:1288-1297`）。
- `contracts/ids.py:18-19`：`task_id = f"{mission}:task-{ordinal}"`，ordinal 就是 id 的一部分，无法事后重排。

**建议处置**：让动态任务的 ordinal 不参与拓扑判断。最小改法：把 `ancestors()/merge_accepted()` 的排序键从 `_ordinal` 换成**真实拓扑序**（按 `dependency_ids` 做 Kahn 排序，ordinal 仅作 tie-break），并在 plan 里把该改动写成 D4-* 的一部分（属于第 4 步必须的既有代码改造，不能只列在"做"的清单里）。同时给动态 Task 的 ordinal 预留独立段（如系统任务从 1000 起），避免 id 语义被误读为顺序。

### R3 · P0 · D4-6 会对已分级为 VERIFIED 的 Claim 执行 VERIFIED→DISPUTED（非法边），且与知识投影自相矛盾

**发现**：D4-2 规定 accept 事务内先分级（可能得 VERIFIED 并按 D4-3 投影进 `knowledge` 表），D4-6 又规定"被接受结果的 Claim C 与已有 X 冲突 ⇔ …；X 是 VERIFIED 知识：**C → DISPUTED**"。当 C 本身也拿到了 pytest 证据被判 VERIFIED 时（两个分支各自跑探针得出相反结论，正是 S4-03 的典型形态），这条规则要求 VERIFIED→DISPUTED。

**依据**：
- `contracts/state_machines.py:157-166`：`_CLAIM[ClaimStatus.VERIFIED] = frozenset({ClaimStatus.SUPERSEDED})`，无 DISPUTED。
- 原文 §25.3（`...complete-design.md:1853-1870`）：VERIFIED 的唯一出边是 SUPERSEDED。
- plan 自己在 §0 术语里写"本步不新增任何边（ORCH §13：DISPUTED 无出边…）"，与 D4-6 的规则冲突。
- 另一处自相矛盾：D4-3 规定 `knowledge.status ∈ {VERIFIED, SUPERSEDED}`。若 C 已投影成知识又被标 DISPUTED，Claim 表与 knowledge 表状态会分叉；plan 未说明此时是否回滚投影。

**建议处置**：在 D4-2/D4-6 之间显式规定**次序与优先级**，例如：分级先算出候选等级 → 冲突检测在**投影之前**执行 → 命中冲突的 Claim 一律封顶在 DISPUTED（UNDER_REVIEW→DISPUTED 或 SUPPORTED→DISPUTED，均为现有合法边），**不投影进 knowledge 表**；并在 plan 中写死"冲突优先于分级"这一条不变量。这样也更符合理论 10-15"保留冲突并等待验证"——争议结论本来就不该先成为正式知识。

### R4 · P0 · Conflict Task 的预算绕过了 §18.2 的"子预算总额不超过父预算"检查，且可让一次合法 accept 整体回滚

**发现**：D4-7 给 Conflict Task `Budget(max_tokens=config.conflict_task_tokens, ...)`，只写了一句"（预留时受 Mission 池约束）"。两个问题：

1. **总额不变量被绕过**：图提交时 `validate_graph` 检查"每个受限维度上 Σ task 预算 ≤ Mission 预算"，而 `normalise_budgets` 把 Mission 池**按任务数均分光**，实际不留余量；事后 `open_account` 只检查 `limits.fits_within(parent.limits)`（对比 Mission 上限而非剩余），所以新任务的预算能通过单账户检查，却让 Σ 超过 Mission 池。
2. **回滚窗口**：`open_account` 在超限时抛 `BudgetError`，而该调用在 accept 事务内 → 一次本来合法的 accept 被整体回滚，结果和 Task 都退回原状，编排循环下轮会重跑同一 accept 并再次失败（活锁）。

**依据**：
- `graph/task_graph.py:268-284`（Σ 检查）与 `graph/task_graph.py:160-184`（`normalise_budgets` 整除均分 Mission 池）。
- `governance/budgets.py:122-143`：`open_account` 只与 `parent.limits` 比较，并在不满足时 `raise BudgetError`。
- `orchestrator/commit_service.py:1437-1509` `accept_result` 全程在单个 `self._store.transaction()` 内；`storage/store.py:213-225` 的事务是 `BEGIN IMMEDIATE`，异常即整体回滚。
- ORCH §12.2："同一份预留由 Mission 向 Task/Attempt 分配时不新增资金；关系与余额在一个编排事务内检查。"

**建议处置**：
- 在 `normalise_budgets` 阶段为"系统任务池"（Conflict + Synthesis）显式预留一档份额（例如 Mission 池先扣 `system_task_reserve`，剩余再均分给 Planner 图），并把 Σ 检查扩展成"Planner 任务 Σ + 系统预留 ≤ Mission"。
- 规定 Conflict Task 的预算取 `min(config.conflict_task_tokens, 系统预留剩余)`，**永不抛异常**；预留不足时不开冲突任务，改为写 `ConflictOpenDeferred` 事件 + 争议 Claim 仍标 DISPUTED，Mission 走 `budget_exhausted` 停止路径，保证 accept 事务不会因预算失败而回滚。
- 在 acceptance 里补一条可决定性场景：系统预留耗尽时 accept 仍成功、冲突以事件形式可解释。

---

## P1（必改：逻辑漏洞、并发/崩溃窗口、与纲要要求不符）

### R5 · P1 · `rule_check` 无法访问知识索引，且"验证期拒绝"与 accept 事务之间存在 TOCTOU

**发现**：D4-4 要求 `rule_check` 校验 `used_knowledge` 的每个 id 是"本 Mission 的 VERIFIED 且未 SUPERSEDED"；D4-8 进一步把"综合任务引用被取代/争议知识"的防线也押在"由 D4-4 在验证期拒绝"。但现有 `rule_check` 是纯本地函数，签名里没有任何编排库句柄；而且验证发生在 accept 事务**之外**，两者之间知识可能被别的 accept 置为 SUPERSEDED。

**依据**：
- `verification/deterministic_checks.py:61-67`：`rule_check(envelope, task, *, artifacts, verification_copy, tampered)`——无 store/知识索引。
- `verification/verifier_router.py:104-111` 调用点同样不传库句柄。
- `orchestrator/event_handler.py:1028-1057`：`router.verify(...)` 跑完（其中 `code_test` 会起子进程跑 pytest，耗时可观）之后才调用 `commit.accept_result(...)`；期间同一 Mission 的其他 Attempt 完全可能完成 accept 并写入 `superseded_by`。
- 原文 §15："真正修改正式状态的只能是 Commit Service"——引用合法性作为**接受条件**，必须在 Commit 事务内复核。

**建议处置**：把 `used_knowledge` 校验做成**两段**：验证期用注入的只读索引给出可读反馈（`rule_check` 增加一个 `knowledge_index` 参数，由 router/event_handler 传入快照），accept 事务内**再校验一次**作为最终守卫；第二次校验失败时不接受结果，走 `fail_result` 而非静默通过。plan 需把 `rule_check` 的签名变更与"accept 内复核"写成显式决定。

### R6 · P1 · D4-2 的"运行目录是其前缀"会退化成"整树跑通 = 所有 Claim 都 VERIFIED"

**发现**：D4-2 规定 VERIFIED 的条件是"evidence 含 `pytest:<目标>` 且本次 code_test 实际运行并通过了该目标（**精确目标或运行目录是其前缀**）"。而当 Task 的 `success_criteria` 里没有任何 `pytest:` 项时，`code_test` 会以 `target=None`（整个验证副本根目录）运行一次；此时"运行目录是前缀"对任意路径都成立，任何 Claim 只要写个 `pytest:` 字样就能拿到 VERIFIED。

**依据**：
- `verification/deterministic_checks.py:120-127`：`if not targets: targets.append(None)`；`runs` 里记 `{"target": None, ...}`。
- ORCH §12.4：「一个代码测试通过可以支持其测试覆盖范围内的结论，**不能顺带把未经验证的产品性能承诺标为 VERIFIED**。」
- acceptance S4-02 依赖的正是"无机器验证只能 SUPPORTED"这条分界。

**建议处置**：把匹配规则收紧为"**Claim 的 pytest 目标必须落在本次实际运行过、且 PASS 的某个具体目标之内，且该目标不得为空（整树根）**"；整树运行只能支撑 Mission 级判定，不得用于 Claim 分级。plan 里把这条写成 D4-2 的显式约束，acceptance 补一条负例（整树跑通、Claim 目标未被单独运行 → 仍为 SUPPORTED）。

### R7 · P1 · schema v2 迁移与现有 Store 校验方式不兼容，且 `UNIQUE` 无法用 ALTER TABLE 添加

**发现**：D4-15 只写了"迁移列表 + 备份 + 原地升级"，没有处理两处硬约束：

1. `Store._initialize_or_validate` 要求迁移表内容**恰好等于单行** `(SCHEMA_VERSION, SCHEMA_NAME, checksum(DDL))`，否则抛 `SchemaIncompatible`。追加一行 v2 记录会直接判不兼容；只改不加又丢了迁移历史（与 ORCH §12.6"每个阶段都有显式编排 schema 迁移"相悖）。
2. SQLite 不支持 `ALTER TABLE ... ADD CONSTRAINT UNIQUE`，而 `artifacts` 是 `STRICT` 表：`UNIQUE(mission_id, path, version)` 只能靠重建表（新建 + 拷贝 + 删 + 改名）或 `CREATE UNIQUE INDEX` 实现，两条路对 `checksum(DDL)` 的影响不同。

**依据**：
- `storage/store.py:185-211`（尤其 209-211 行 `if rows != [(...)]: raise SchemaIncompatible`）。
- `storage/schema.py:17` `SCHEMA_VERSION = 1`；`schema.py:145-156` `artifacts` 表已有 `UNIQUE(attempt_id, path, version)`，声明为 `STRICT`。
- ORCH §12.6："部署新版本前备份、一致性检查、副本迁移、旧场景回归，再小范围启用。"

**建议处置**：在 D4-15 里写明迁移契约：迁移表保留**全部历史行**，校验改为"最高版本行 == 当前 SCHEMA_VERSION 且其 checksum 匹配"；`artifacts` 的唯一性用 `CREATE UNIQUE INDEX ux_artifacts_mission_path_version` 实现（可 ALTER 之外单独创建，且写进 v2 DDL）。acceptance 的 `test_schema_migration.py` 要覆盖"v1 库升级后 checksum 校验通过 + 旧场景回归"。

### R8 · P1 · D4-11 的 `block` 策略在现有运行循环里不可观察，且停止原因不是合法枚举值

**发现**：三个子问题：

1. **不可观察**：`block` 下"本轮不建 Attempt、Task 保持 READY 下轮重试"，但 `_next_attempt` 返回 False → `_decide` 返回 False → `_cycle` 无进展；`run()` 在**连续两轮无进展且无在途 turn** 时直接 return。`max_retrieval_failures` 默认 3 永远到不了，Mission 停在 ACTIVE、`run()` 静默结束。acceptance S4-07"连续超限 → Task FAILED `retrieval_unavailable`"无法被决定性测试观察到。
2. **停止原因非法**：`stop_task` 的 `stop_reason` 形参类型是 `MissionStopReason`，枚举里没有 `retrieval_unavailable`；新增成员会改动 ORCH §13"第 2 步固定可序列化状态与停止 reason"的公开契约，D4-15/D4-18 未登记。
3. **副作用未写进验收**：`stop_task` 会连带 `fail_mission` + `_cascade_stop`，即 Task FAILED 必然导致 **Mission FAILED**；acceptance S4-07 只写 Task，没写 Mission 结局。

**依据**：`orchestrator/event_handler.py:225-244`（`idle_rounds >= 2: return`）、`event_handler.py:248-255`（`_has_inflight` 只看 SUBMITTED intent）、`contracts/state_machines.py:28-37`（`MissionStopReason` 成员表）、`orchestrator/commit_service.py:1636-1686`（`stop_task` 内 `fail_mission` + `_cascade_stop`）。

**建议处置**：把"检索失败"计为一次**有进展的循环**（写 `RetrievalUnavailable` 事件即视为 progressed，或计数器持久化到 Task/Mission 的 `final_report`/新列并在下轮基于它推进），使计数可跨轮累计；在 `MissionStopReason` 增加 `RETRIEVAL_UNAVAILABLE` 并在 D4-18 的公开 API 快照里登记；acceptance S4-07 补"Mission FAILED(retrieval_unavailable)"这一必须结果，以及计数器在进程重启后仍然成立（崩溃恢复）。

### R9 · P1 · Conflict Task 的 `verification_policy` 没有 `critic_review`，与"派 Critic/Arbiter"不符

**发现**：D4-7 规定 Conflict Task 的 `verification_policy=[format_check, rule_check, code_test]`。仲裁结论是系统里可信度要求最高的一类结论，却是唯一没有独立 Critic 的路径。

**依据**：
- 原文 §14.4（`...complete-design.md:961-975`）：「创建 Conflict Task → **派 Critic / Arbiter** → 执行外部验证 → Commit 结论」。
- ORCH §6.2 `verification/*` 行："冲突 Claim 均标 DISPUTED，创建 Conflict Task，**派 Critic/Arbiter 并外部核查**，再 Commit"。
- 原文 §10.2："Verifier 应尽量独立，不应只看到原作者的自我解释"；理论 10-7"一致不等于正确，证据和外部验证更重要"。

**建议处置**：Conflict Task 的 `verification_policy` 加上 `critic_review`（该层已部署：`verification/verifier_router.py:112-130` + `event_handler._run_critic`），并在 D4-10 的 critic 模板里明确"仲裁场景下 Critic 看到双方证据但不看任何一方作者自述"。acceptance S4-03 补一条：仲裁结果的验证层记录里同时存在 `critic_review` 与 `code_test` 的 PASS。

### R10 · P1 · 缺 §10.2 要求的 **Verifier** 可见性模板

**发现**：D4-10 给出 worker / explorer / critic / arbiter / synthesizer 五种模板，唯独没有 **Verifier** 模板；而 explorer 模板本步没有消费者（plan §2"不做"里已写明 Explorer 角色留到第 5 步）。

**依据**：
- 原文 §10.2（`...complete-design.md:691-700`）四条默认可见性里明确含"Verifier 应尽量独立，不应只看到原作者的自我解释"。
- ORCH §6.2 `context/context_builder.py` 行逐字写："Worker、Explorer、Critic、**Verifier** 采用不同可见性模板"。
- ORIGINAL-30-11"Agent 默认不会把未验证 Claim 当成事实"绑定 S4-02。

**建议处置**：把 `verifier` 加入模板集合（可与 critic 模板共享底座但**必须剔除提交者的 summary/自述**，只留产物、测试输出、准则），并在 acceptance S4-02/S4-03 里加一条可决定性断言：Critic/Verifier 包中不含 `envelope.summary` 与提交者 `confidence`。explorer 模板若本步无消费者，建议降级为"仅注册不启用"并在 journal 登记，避免范围蔓延。

### R11 · P1 · 综合任务已 COMPLETED 之后出现冲突/取代时，没有再验收路径

**发现**：D4-8 只处理"综合任务尚无在途 Attempt"和"已在途"两种情形；第三种情形——综合任务**已经 COMPLETED**、之后才有叶子的迟到 accept 触发冲突/取代——没有处理。plan §5 风险条"由 D4-4 在验证期拒绝引用被取代知识，综合重试时拿到新知识"对这一情形不成立：已 COMPLETED 的 Task 不会再有 Attempt，§25.1 也没有 COMPLETED→ACTIVE。

**依据**：
- `contracts/state_machines.py:118-126`：`_TASK[COMPLETED] = frozenset()`。
- ORCH §13 明确禁止"悄悄加 COMPLETED→ACTIVE"；§12.1"Task 已 COMPLETED → 不重跑；**新要求必须形成新的明确工作对象**"。
- 另外 D4-4 只拒绝 SUPERSEDED / 非 VERIFIED 的引用；一条被标 `disputed_by` 但仍 VERIFIED 的知识可以合法通过校验，所以"综合基于争议知识完成"是可达状态。

**建议处置**：明确第三种情形的处置并写进 acceptance：或者（a）判定为本版本的显式边界——冲突只能在综合任务开始前开启，否则整个 Mission 以可解释原因失败（因为仲裁任务本身未 COMPLETED，`judge_mission` 会阻塞，需要一个确定的出口）；或者（b）按 ORCH §13 的方案建**新的综合 Task 实体**。无论哪种，plan 需要说明 `judge_mission` 要求"每个 Task 都 COMPLETED"（`commit_service.py:1523-1526`）时这条路怎么收敛。

### R12 · P1 · 数据模型/schema 迁移清单不完整：Claim 缺 `stance`，Task 缺 `kind`/`context`

**发现**：D4-6 的冲突判定要用 `X.stance`，其中 X 可以是"已接受结果的 Claim"（不是知识）；D4-7/D4-8/D4-16 要用 `task.kind`（conflict/synthesis）与 `task.context`。但 D4-15 的迁移清单只写了"`claims` 加 `key` 列"，没有 `stance`；`Task` 数据类与 `tasks` 表也没有 `kind`/`context`。

**依据**：
- `contracts/models.py:654-672` `Claim` 字段表（无 key/stance）；`storage/schema.py:135-143` `claims` 表列。
- `contracts/models.py:245-268` `Task` 字段表（无 kind/context）；`TaskProposal.from_json`（`orchestrator/commit_service.py:388-404`）显式拒绝未知字段。
- plan D4-15 迁移清单原文只列"新表 `knowledge`、`summaries`、`conflicts`；`claims` 加 `key` 列；`artifacts` 加 UNIQUE"。

**建议处置**：补全迁移清单与契约变更清单（`claims.stance`、`tasks.kind`、`tasks.context`），并说明这些字段进入 `Task.to_json()` 后对 `commit_id`/`proposal_hash`/`context_version` 哈希的影响（是否破坏第 2/3 步已有回执的可重放性），在 D4-18 的公开 API 快照里一并登记。

### R13 · P1 · Conflict Task 的产物契约未定义，可能触发 `ArtifactConflict` 或 `rule_check` 必失败

**发现**：D4-7 给了 Conflict Task 的 `success_criteria=["arbitration:<key>"]`、工具和预算，但没有规定它**产出什么文件**。现有 `rule_check` 会对"没有提交任何产物"直接判 FAIL；而任何产物都会进入 Mission 判定时的整合合并。

**依据**：
- `verification/deterministic_checks.py:74-75`：`if not envelope.artifacts: problems.append("no artifacts were submitted")`。
- `orchestrator/event_handler.py:1435-1443`：`_judge` 里 `merge_accepted` 抛 `ArtifactConflict` 即 `fail_mission(ARTIFACT_CONFLICT)`。
- `versioning.py:97-104`：独立分支同路径不同内容即冲突。

**建议处置**：在 D4-7 里给 Conflict Task 固定 `outputs`/产物路径命名（例如 `arbitration/<key>.md` + 探针测试文件放在 `arbitration/` 前缀下），并确保该前缀不与综合任务的 `COMPARISON.md`、种子的 `contract/`、`impls/`、`tests/` 冲突；acceptance 补一条断言：仲裁产物进入整合副本后 Mission 判定仍只由 `pytest:tests/test_comparison.py` 与 `file:COMPARISON.md` 决定。

---

## P2（建议：术语出处不实、登记缺失、可观察性与范围问题）

### R14 · P2 · 多处把本版本的实施约定写成"原文依据"，违反 ORCH §13 的登记纪律

**发现与依据**（逐条核对）：
- D4-1 标注"依据 原文 §13、§26.5"，但 §26.5 的 claim schema（`...complete-design.md:1945-1961`）只有 id/content/type/status/source_task/source_attempt/evidence/dependencies/verifier_results/confidence_metadata/supersedes——**没有 `key`、`stance`**。`contradicts` 只能对应 §12.3 的"冲突报告"（plan 已引，可保留）。
- plan §0 术语把状态机写成"SUPPORTED→{VERIFIED, REJECTED, DISPUTED}（原文 §14.3、§25.3）"。§25.3 原图（1853-1870 行）里 SUPPORTED **没有出边**；SUPPORTED→VERIFIED 只能由 §14.3 的链式图推出，SUPPORTED→REJECTED/DISPUTED 是本项目第 2 步实现时的扩展（`contracts/state_machines.py:166-168`）。plan 说"本步不新增任何边"对**代码**是成立的，但对**原文**不成立。
- D4-9 的权重 3/2/1/0.5/0.5、`used_by` 封顶 3、`max_knowledge_items=12`，原文 §10.1（669-690 行）只给了因子清单，没有任何权重。

**依据（纪律条款）**：ORCH §13 开头："以下不是替换原文，而是防止实现时出现两套不兼容语义。**任何不同选择都要单独登记，不可宣称是原文原句**。"

**建议处置**：在 plan 里新增一小节"本步实施约定（非原文原句）"，把上述三项（Claim 扩展字段、SUPPORTED 出边、Retrieval 权重与阈值）逐条登记，并注明 `RETRIEVAL_VERSION` 随权重变更而变（30-29 要求 Retrieval 有版本号，plan 已做，保持一致即可）。

### R15 · P2 · Provenance 七问少了一问；知识记录无"修改者"

**发现**：plan §0 的 Provenance 条列了 6 个问题（谁提出、来自哪个 Task/Attempt、基于哪些知识、经过什么验证、被哪些任务使用、是否已被取代），漏掉原文 §11.2 的"**由谁修改过？**"；D4-3 的知识字段表也没有对应字段。

**依据**：`...complete-design.md:753-766` §11.2 共七问。

**建议处置**：要么补一个 `amended_by`/`revisions` 字段（知识本身不可变时可记录"由哪条仲裁/取代结论改动过"，正好可与 D4-7 的 `resolves`/`confirmed_by`、D4-5 的 `superseded_by` 合并表达），要么在 plan 里显式登记"本版本知识不可变，'由谁修改过'由 supersedes/resolves 链回答"。

### R16 · P2 · "Judge"与代码里的 `judge_mission` 同名异义

**发现**：plan §0 引入"Judge 选一个最好的候选"（理论 04-7），而已交付代码的 `CommitService.judge_mission` 指的是**Mission 根成功条件判定**（ORCH §12.4 / 原文 §19.1），两者完全不同；D4-14 又说"`judge_mission` 写入 `final_report.lineage`"，同一段文字里两种 Judge 并存。

**依据**：理论 `04_tree_search_blackboard_memory.md:138-141`；`orchestrator/commit_service.py:1514-1522` 的 docstring"Mission-level success judgment, independent of the Task PASS"。

**建议处置**：plan 里加一句消歧："理论 04-7 的 Judge（选冠军）本版本**不实现**；代码中的 `judge_mission` 是 Mission 成功判定，二者不是同一概念。"术语纪律是 program.md §2 第 8 条的硬要求（"术语用错视为缺陷"）。

### R17 · P2 · Blackboard 四层里的 Raw Logs 没有落点决定

**发现**：plan §0 定义了四层（Raw Logs / Candidate Claims / Verified Knowledge / Summaries），切片 A 也列了 `memory/blackboard.py`，但 D4-1…D4-18 里**没有任何一条决定描述 Raw Logs 层**——谁写、存什么、如何"只引用不直接喂"、如何满足脱敏要求。

**依据**：ORCH §6.2 `memory/blackboard.py` 行要求"Raw Logs 引用、Candidate Claims、Verified Knowledge、Summaries 四层"；ORCH §13："§11 Raw Logs'全部原始过程'与 §21 密钥不得泄漏 → 记录已授权、可公开持久化的过程及产物/账本引用；**不收集隐藏推理，不将密钥写进可检索日志**"。

**建议处置**：补一条 D4-*：Raw Logs 层本版本 = 对既有 `events` / `results` / `artifacts` / SDK turn 回执的**只读引用视图**（不新建原始日志表、不落隐藏推理），Context Builder 只给引用不内联；并在 acceptance 里加一条断言：上下文包内不出现任何密钥/凭证字段。

### R18 · P2 · D4-13 的"分支"定义在多根祖先下不确定

**发现**："分支 = 根任务子树（任务按其根归属）"。综合任务依赖全部叶子，其祖先闭包含**所有**根，归属未定义 → 摘要内容与 `version`（内容 hash）会随实现细节漂移，破坏"确定性压缩"。

**依据**：`graph/task_graph.py:287-300` 的 `ancestors_of` 返回的是集合；`versioning.ancestors` 同理。

**建议处置**：把归属规则写成确定式，例如"按拓扑序最小的根归属；祖先含多个根的任务单独归入 `global` 分支"，并在 acceptance 里用固定图断言摘要 `version` 稳定。

### R19 · P2 · 动态 Conflict Task 以"有依赖但直接 READY"落库，偏离既有约定

**发现**：D4-7"依赖 = 争议 Claim 的来源 Task（都已 COMPLETED → 立即 READY）"。现有代码的约定是"有依赖即 BLOCKED，靠 `_unblock` 转 READY"。

**依据**：`orchestrator/commit_service.py:517`（`status=TaskStatus.READY if not dependencies else TaskStatus.BLOCKED`）；`contracts/state_machines.py:115-117` 注释"A brand-new Task with no dependencies is committed directly as READY（`[*]→BLOCKED` 边是空依赖的情形）"。

**建议处置**：要么统一为"先 BLOCKED，同事务内调用 `_unblock` 转 READY"（不引入新约定，且事件时间线上能看到 `TaskUnblocked`），要么在 plan 里登记新约定并说明为何不走 `_unblock`。

### R20 · P2 · D4-15"在 `record_result` 事务内重分配产物版本"的幂等性未规定

**发现**：`record_result` 对 `(attempt_id, turn_id)` 幂等，重复投递会直接返回已存结果；但若版本分配逻辑放在事务内、又在幂等分支之前/之后执行，重投递是否会二次抬升版本没有写明。

**依据**：`orchestrator/commit_service.py:1275-1280`（幂等短路）与 `commit_service.py:1296-1298`（`upsert_artifact` 循环）；`artifacts/workspace.py:100-129` 当前在快照时按 `next_versions` 赋临时版本；`event_handler.py:877` 是快照调用点（事务外读，正是 L3-2 的竞态来源）。

**建议处置**：在 D4-15 里写明"版本分配只在幂等短路之后、且对同一 `(attempt_id, path, content_hash)` 返回同一版本"，并让 `test_artifact_versions.py` 覆盖"同一 turn 重复投递版本不变"这一断言（现在 acceptance 只要求"同 Task 两候选同路径版本不同"）。

### R21 · P2 · D4-2 把 VERIFIED 唯一绑定到 `code_test`，排除了其他"可靠规则"

**发现**：原文 §14.3 / 理论 04-9 定义 VERIFIED = "通过机器验证**或可靠规则**"，而 D4-2 把 VERIFIED 的充要条件写成 `code_test` + pytest 目标匹配，`rule_check`（确定性规则）与 `formal_check` 都不算。

**依据**：理论 `04_...md:182-208`；ORCH §12.4"验证是按政策选择的分层体系"。

**建议处置**：保留本版本的收紧选择（简单、可决定性强），但按 ORCH §13 登记为实施约定，并说明第 5/6 步开放 `formal_check` 时如何扩展，避免后续被当成原文语义。

### R22 · P2 · acceptance 的可观察性与交付口径缺口

**发现**：
1. S4-03 的"Arbiter 只给意见 → `rule_check` FAIL **重试**"要求冲突任务至少允许 2 次 Attempt，但 D4-7 的 `config.conflict_task_attempts` 默认值未定，acceptance 未把"≥2"写成前提，测试可能因预算耗尽走到 Task FAILED 而不是重试。
2. 附加门槛只写了"安装 wheel 后跑 `tests/orchestrator` 与 `demo --scenario knowledge-sharing`"，未写 ORCH §6.3 拟交付命令的完整形式（`--provider fixtures --evidence-dir evidence/s4`），也未点名 ORCH §14.3 要求的 7 个证据文件（`baseline.json`、`events.jsonl`、`final_state.json`、`artifacts/`、`verification.json`、`costs.json`、`test-report.json`）。
3. ORCH §14.1 要求每一步的完成包含"版本迁移和**关闭开关**"；D4-11 只有检索失败策略开关，没有整层知识共享的关闭开关（第 2、3 步同样没有，属沿袭性缺口）。

**依据**：ORCH §6.3 末尾拟交付命令行；ORCH §14.3 证据目录清单；ORCH §14.1。

**建议处置**：acceptance 的附加门槛补齐上述三项；`conflict_task_attempts` 默认值写进 D4-7 并在 acceptance 里作为 S4-03 的前提条件声明。

### R23 · P2 · 轻度范围蔓延两处

**发现**：
1. explorer 可见性模板本步无消费者（plan §2"不做"已把 Explorer 角色推到第 5 步）——见 R10 的处置建议。
2. `CLI mission lineage` 子命令：ORIGINAL-30-27 在附录里同时挂在第 4、8 步且验收 ID 是 **S8-01**，ORCH §14.3 的 CLI 最小规范里"第 8 步增加 replay/evaluate"。本步做血缘数据与 `lineage.json` 是必要的（S4-01 要求血缘可回溯），但**新增 CLI 子命令**可以推迟。

**依据**：`...phase2-zh-CN.md:759`（`ORIGINAL-30-27 | ... | 4, 8 | S8-01`）；ORCH §14.3 CLI 表。

**建议处置**：本步保留 `final_report.lineage` + 证据目录 `lineage.json`（满足 S4-01），把 `mission lineage` 子命令移到第 8 步，或在 plan 里注明"提前交付、第 8 步扩展为贡献归因"。

---

## 总体结论

方向正确、覆盖面完整：ORCH §6.3 的 8 条必须结果（S4-01…S4-08）在 acceptance 中**逐条有对应的可决定性判定入口**，没有遗漏；对"只有 VERIFIED 是正式知识""不是多数票""外部内容是数据不是指令""摘要不改可信状态""来源都通过不等于合成通过"等关键语义的把握与原文一致；范围控制总体克制（明确把向量检索、LLM 摘要、Worker 自由改图、多 Mission 配额排除在外），符合 ORCH §6.2"本版本只开放系统定义的 Conflict Task 和固定综合任务模板"。

但**按现文直接实施会撞上四处已交付代码的硬不变量**：R1（READY→BLOCKED 非法转换）、R2（动态 ordinal 破坏"ordinal ≡ 拓扑序"，同时污染产物合并与终结任务/血缘起点）、R3（VERIFIED→DISPUTED 非法边，且与知识投影自相矛盾）、R4（Conflict Task 预算绕过 §18.2 总额检查并可让一次合法 accept 整体回滚）。这四条是 P0，必须在动工前改定；其中 R1 与 R2 会同时影响 acceptance S4-05 的表述（"综合任务依赖全部叶子（含冲突任务）"这句本身就是 P0 的载体），R3 会影响 S4-03 的状态断言。

P1 里最值得重视的是 R5（`rule_check` 拿不到知识索引，且"验证期拒绝"与 accept 之间存在 TOCTOU——这正是"Commit Service 唯一写入 + 同事务"不变量的边界）、R6（"运行目录是前缀"会把整树跑通退化成全量 VERIFIED，直接违反 ORCH §12.4）、R8（`block` 策略在现有 `run()` 循环里不可观察，S4-07 现写法测不出来）。R7 的 schema 迁移与 Store 校验方式不兼容，会在"v1 库升级到 v2"这条附加门槛上直接失败。

建议处置顺序：先定 R1/R2/R3/R4 的方案（它们互相耦合，且都指向"动态入图的系统任务如何与静态图不变量共存"这一个主要矛盾），把结论回写成新的 D4-7'/D4-8'；再修 R5/R6/R7/R8 并同步改 acceptance 的判定语句；R9/R10 补齐纲要明列但缺失的 Critic/Verifier 环节；R12/R13 补契约与产物路径；P2 各条以"登记实施约定 + 补断言"为主，不改设计方向。
