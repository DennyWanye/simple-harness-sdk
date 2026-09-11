<!-- 独立评审（claude-opus-5，只读）；由主会话从代理回复落盘，内容未改；处置见 journal §1 与 plan §6 -->

# 第 6 步实施计划独立评审（只读）

评审对象：`plans/2026-09-11-agent-orchestrator/step06/plan.md`（D6-1…D6-13、§6.1）+ `acceptance.md`（S6-01…S6-09）+ `reports/design-brief.md`。
基线：SDK main `408d914`，但**工作树已经开始实现切片 A**（`models.py` / `budgets.py` / `schema.py` / `store.py` 已改，`scheduling/backpressure.py` 与 `tests/orchestrator/step06/test_backpressure_state.py` 已新建）。下面的行号在工作树版本上核对过。

---

## P0（会让某条 S6 验收不成立，或与内核不变量冲突）

**P0-1 · Global 账户会把每条预留的 `mission_id` 写成 `"global"`，S6-01/S6-07 的账本证据直接消失**
- 发现：`governance/budgets.py:251` —— `chain[-1].account_id.removeprefix("budget:")` 作为 `budget_reservations.mission_id` 写入。D6-1 把 Mission 账户的 `parent_id` 指向 `budget:global` 之后，`chain[-1]` 不再是 Mission 账户，每条 reservation 的 `mission_id` 变成字符串 `"global"`。`costs_report`（`budgets.py:358`、SQL 在 `:368`）按 `mission_id` 过滤 `budget_reservations`，于是每个 Mission 的 `costs.json` 里 `reservations` 恒为空数组。
- 违反：S6-01「每个 Mission 的 `budget:<mission>` 账户只记本 Mission 的预留/结算」、S6-07「账本里 settled 与 reserved …」的可判定证据；也违反 §18.2 的归属语义。
- 处置：`reserve()` 增加显式 `mission_id` 形参，由 `create_attempt` / `create_service_intent`（两处都已知 `task.mission_id`）传入，禁止从账户链末端反推。D6-1 必须把这条改动写进决定表，并在 S6-01 里加一条断言"两个 Mission 的 `costs.json.reservations` 非空且互不包含对方 subject"。

**P0-2 · `Budget.max_tool_calls` 会让任务图提交整体失败（继承列表没跟上）**
- 发现：`contracts/models.py:159-166` 的 `fits_within` 遍历**全部**字段，规则是"父设了、子为 None → 不通过"。新增的 `max_tool_calls`（`models.py:143`，工作树已加）没有进入两处继承列表：`graph/task_graph.py:181`（`normalise_budgets`，只继承 `max_attempts / max_concurrency / max_runtime_seconds`）和 `planning/manager.py:134-144`（`inherit_limits`，同样缺）。
- 后果：只要 Mission 预算带 `max_tool_calls`（S6-07 的工具维度用例**必须**带），Planner 的任务图在 `open_account`（`budgets.py:126-130`）抛 `BudgetError` → `commit_task_graph` 整体拒绝；synthesis / conflict 这类系统任务会在 accept 事务里炸——`manager.py:135-137` 的注释写明 `inherit_limits` 存在的唯一理由就是防这个。Global→Mission 也是同一个坑：`create_mission`（`commit_service.py:270-276`）没有任何继承步骤，Global 设了 `max_tool_calls` 而 MissionSpec 没设，建 Mission 就失败。
- 违反：S6-07（三个维度各一条）、附加门槛（step02–06 全绿）。
- 处置：D6-8 里显式写出"新增预算维度必须同步三处：`fits_within` 的语义、`normalise_budgets`、`inherit_limits`，外加 Global→Mission 的继承（新增）"。**这是工作树现在就已经坏掉的状态**，不是未来风险。

**P0-3 · 回显核对写死 `self._config.model`，Planner/Manager 一路由到 `large` 就会在计划阶段判 `model_echo_mismatch` 停掉 Mission**
- 发现：`orchestrator/event_handler.py:875 / 881`（Planner）、`933-949`（Worker）、`1519 / 1525`（Manager）三处都是 `if echoed and echoed != {self._config.model}`；另外 `model_profile_ref=self._config.model` 有四处（`:361`、`:1434`、`:1679`、`:1939`），`model=self._config.model` 两处（`:1889`、`:1972`）。共 11 个点。
- 后果：D6-4 规定 `by_role` 把 Planner/Manager/Critic 路由到 `large`，而 `AgentBridge.echoed_models`（`runtime/agent_worker.py:145-153`）读的是该 Run 的真实回显 —— 与 `config.model`（= `small` 或 demo 默认模型）不等 → `fail_planning(MODEL_ECHO_MISMATCH)`，Mission 在第一步就死。
- 违反：S6-03、demo `multi-mission`、附加门槛。
- 处置：D6-4 补一张改造点清单，并规定 `expected` 取自**冻结在 dispatch intent 里的 `profile.model`**，不是当前运行时配置——否则重启后换了配置，旧 intent 的核对会误判，与 S6-08「旧 Attempt 回到原目标配置」直接冲突。

**P0-4 · 多执行池的改造面在计划里只有一句话，而现有装配是硬性单例**
- 发现：`runtime/assembly.py:198-226` 的 `assemble_orchestrator_runtime(config, provider)` 只接受**一个** provider，绑定**一个** `config.model`、**一个** `config.execution_db`（`assembly.py:121-123`）、**一个** gateway、**一个** `WorkspaceManager`；`Orchestrator.__init__`（`event_handler.py:131-138`）签名也是单 provider；`self._bridge = AgentBridge(self._assembled.runtime, …)`（`event_handler.py:165`）单桥。`self.assembled.gateway` / `.workspaces` 在代码里被引用十几处。
- 计划缺口：D6-4 的 `RuntimeProfile.provider` 没说是对象还是工厂；`OrchestratorConfig` 是 frozen dataclass，只有 `extra: dict` 一个口子，计划没说多 profile 配置放哪；没说 gateway 与 WorkspaceManager 在多 runtime 下**必须共享单例**（`_bind_agent` / `_bind_workspace` 若绑到错池的 gateway，S6-05 的拒绝链和 S6-04 的工作区隔离都会失效）。
- 处置：D6-5 补"改造面清单"：`AssembledOrchestratorRuntime` → `RuntimePools`（共享 gateway/workspaces，按 profile 分 runtime/bridge/execution db），`Orchestrator.__init__` 的 provider 改成 `Mapping[profile_id, provider]` 且保留单 provider 兼容路径（否则 step02–05 的全部测试构造签名都要改，附加门槛"全绿"会被自己打掉）。

**P0-5 · S6-06 的"有界等待"在当前 `run()` 循环里根本到不了**
- 发现：`event_handler.py:252-277` 的 `run()`：`_cycle` 不 progress 且 `_has_inflight()` 为 False 时，连续 2 轮（≈2×`poll_interval`=0.1s）就 `return`。`_has_inflight`（`:281-286`）只统计 SUBMITTED 的非 critic intent。
- 后果：D6-5 的"Task 等待超过 `profile_wait_seconds`（默认 300）→ `stop_task(RUNTIME_UNAVAILABLE)`"永远等不到：没有在途 intent 时循环 0.1 秒后就退出，Mission 停在 ACTIVE，既没有"有界等待"的可观察终点，也没有"明确降级"。
- 违反：S6-06。
- 处置：二选一并写进计划——① 把"因 profile 不可用而等待中的 Task"计入 in-flight，并把边界改成测试可注入的时钟（`Store.open(clock=...)` 已支持）且远小于 300s；② 直接改判定：第一次检出不可用就降级或显式停止 + 事件，不靠墙钟等待。现在的 300s 默认值在 fixtures 测试里既跑不到也不该跑。

**P0-6 · S6-02 的触发条件与 D6-9 的验证并发互为前提，却被拆到不同切片**
- 发现：`event_handler.py:303-334` 的 `_cycle_inner` 是严格顺序：collect 之后 `for stored in list_results_by_verification("PENDING","RUNDING")` 逐个 `await self._verify(...)`，一轮之内把待验证队列清空。下一轮分配前观测到的 `pending_verifications` 几乎恒为 0 —— "放慢 Critic"只会让这一轮变长，不会形成队列。
- 后果：D6-2 的 `max_pending_verifications`（工作树默认 4，`backpressure.py:40`）不可能被触达；S6-02「待验证结果数 ≥ 高水位时 `BackpressureRaised(verifying)`」无法成立。而计划把 `test_backpressure.py`（S6-02）放在切片 A、把 Verifier 并发放在切片 D。
- 附带：一旦 D6-9 把验证挪到后台 asyncio 任务集合，`_has_inflight()`（`:281-286`）不认识这些任务 → `run()` 会在验证还在跑时返回，Mission 永远不 COMPLETED。
- 处置：把"验证跨 cycle 异步化 + 计入 in-flight"提到切片 A 与 S6-02 同批；或者把 S6-02 的触发维度改成顺序循环里真实会堆积的 `pending_dispatch` / `running_attempts`，并在计划里写明选了哪个、为什么。

---

## P1（设计缺口 / 与代码事实不符）

**P1-1 · `stop_task` 一定会终止整个 Mission，计划的措辞误导**
`commit_service.py:2846-2905`：`stop_task` 在 FAILED 掉该 Task 之后立刻 `next_mission(mission, MissionStatus.FAILED, …)` 并 `_cascade_stop`（`:1760-1807`）取消全部 READY/ACTIVE 任务。D6-5 的 `stop_task(RUNTIME_UNAVAILABLE)`、D6-9 的 `stop_task(VERIFIER_UNAVAILABLE)`、D6-8 的 runtime 维度都写成"停 Task"，读起来像只停一个分支。必须写明这是 Mission 级终止，并据此重述 S6-06「其他可执行 Mission 继续」= 只保证跨 Mission 隔离，不保证同 Mission 的其他分支。

**P1-2 · Global 账户耗尽会被错误归因给某个 Task，且会连累无辜的 Mission**
`event_handler.py:1999-2016` 用 `error.account_id == mission_account(mission.id)` 区分"池耗尽 → `fail_mission`"与"Task 耗尽 → `stop_task`"。Global 耗尽时 `account_id == "budget:global"`，落进 else → 记成某个 Task 的停止（归因错误），而且**两个 Mission 都会因为对方花光 Global 而各自被"Task 停止"**。D6-1 完全没有这一支。处置：增加第三分支 `account_id == "budget:global"` → `fail_mission(BUDGET_EXHAUSTED, scope="global")`，并在 S6-01 里明确"本步只测 Mission 池耗尽；Global 池耗尽的归因单独一条测试"。

**P1-3 · `fits_within` 比的是父的上限，不是父的剩余 —— D6-1 的措辞与代码不符**
`budgets.py:126-130` 只做 `limits.fits_within(parent.limits)`。两个 Mission 各自 `max_tokens = Global.max_tokens` 都能开户成功（超卖），只有 `reserve()` 的链式检查会暴露。D6-1 写的"Mission 预算不得超过 Global **剩余**"不成立。要么改判据（`open_account` 里按"已开同级子账户 limits 之和"校验），要么把措辞改成"不得超过 Global 上限"并在 §6.1 登记"超卖靠 reserve 链兜住"。

**P1-4 · `scheduler_state` 目前绕过 Commit Service，且没有事务**
`storage/store.py:826-843` 的 `get/put_scheduler_state` 是 Store 的裸方法，没有对应的 CommitService 包装，`put_scheduler_state` 自己也不开事务。`store.py` 文件头的不变量原文是「Every mutation … is issued by the Commit Service only」。D6-2 说"由 Commit Service 写"——实现要对齐，并且**背压状态更新 + 各 Mission 的 `BackpressureRaised` 事件必须在同一个事务内**，否则崩溃点会留下"状态已变、事件没写"的不可解释历史。

**P1-5 · S6-07 的"守恒"断言在数学上就是错的**
`budgets.py:317-355` 的 `settle` 语义是"用事实替换预留"：`reserved_* -= 预留额`、`settled_* += 实际用量`，两者本就不相等（预留是上限、结算是事实）。D6-8 写的"reserved 与 settled 之和守恒、不重复相加"没法写成测试。改为三条可判定断言：① 同一 `usage_ref` 重复导入时 `import_usage` 返回 0（`imported_usage` PK 已保证，测试要锁住）；② 账户 `settled_tokens` == 该 Mission 全部 `imported_usage` 的 token 之和；③ 结算后 `reserved_* >= 0`，且账户上的开放预留 == 未 SETTLED 的 reservation 之和。

**P1-8 · 升级/降级的错误分类没有定义，与 L4-4 正面相关**
D6-4 的升级触发含 `provider_error`，D6-5 的不可用分类是 `provider_unavailable`（连接/超时/5xx）。但 step04 journal §5 的 **L4-4（归属第 6 步）**记的是真实 DeepSeek 上高频的 `provider_protocol_error / tool_parse` → turn FAILED。这类既不是"验证失败"也不是"provider 不可达"，连续两次就会撞上 `profile_failure_threshold=2` 把一个健康 profile 标成 unavailable；反过来如果不算，升级链又永远不触发。必须给一张 `错误 → {升级 / 降级 / 都不算}` 的分类表，并说明它与 `empty_response_retries`（`assembly.py:66`）、SDK 的 turn FAILED 的关系。

**P1-9 · 每 profile 的价格表没有接到账本，而 L2-6 的归属就是第 6 步**
`_reservation`（`event_handler.py:391-396`）用的是全局 `self._config.price_table`；`AgentBridge(unpriced=self._config.unpriced)`（`:165`）是全局开关；`config.policies()`（`assembly.py:129-144`）是单一 estimator。D6-4 的 `RuntimeProfile.price_table?` 无处落地，两个 profile 价格不同必然记错。而 D6-12 直接写"unpriced 记账，L2-6 登记不变"——这等于把 §8.2 budgets 行「所有真实调用费用关联」和 §23.2「每模型成本」降级成 token-only。二选一并明写：真接 DeepSeek 价格表；或在 §6.1 登记"本步费用维度仍 unpriced，S6-07 的 cost 维度只用 fixtures 验证"，并把 L2-6 重新归属到第 8/9 步。

**P1-10 · `trace_id = mission_id` 与既有 `ids.trace_id` 冲突**
`contracts/ids.py:56-57` 是 `trace_id(mission) = "trace-" + sha256(mission)[:16]`，`_emit`（`commit_service.py:207`）写的就是它。D6-10 与 §6.1 写的"`trace_id` = mission_id"如果照字面实现，`trace.json` 的 trace_id 会与 `events.jsonl` 的不一致 —— S6-09「所有 Result 可追」自相矛盾。裁定应改写成"trace 根是 Mission，`trace_id = ids.trace_id(mission_id)`，与事件信封同值"。（§23.1 的 10 字段里 `agent_id` 也是必需项，`Attempt.agent_id` 有，可取。）

**P1-11 · 背压事件落在 events 表的口径**
`schema.py:44` `events.mission_id TEXT NOT NULL`，计划选择"写进每个活跃 Mission"。两个后果要写进计划：① 活跃 Mission 数为 0 的瞬间，状态变化在事件表里完全不可见（只剩 `scheduler_state`），S6-02 的时间线可能缺头缺尾；② 同一次变化产生 N 条事件，`metrics.json` 里"背压次数"会重复计数。建议 `scheduler.json` 以 `scheduler_state` 的变更序列为唯一真值，events 只作为各 Mission 时间线的投影，并写明去重口径。

**P1-12 · 全局 `max_running_attempts` 的权威检查点没定，且默认值永不触发**
`create_attempt` 的 `max_open_attempts`（`commit_service.py:1899-1909`）只统计 `task.mission_id` 之内，是 Mission 级。D6-1 的全局上限若只在 Allocator 侧生效就只是建议值——两次 `_decide` 之间、以及多实例场景会越限（§17.1/§17.5 要求原子单点）。另外默认 `max_concurrency × 4`：demo 的 `--max-concurrency` 默认是 **1**（`__main__.py:371`），全局上限 = 4，而两个 Mission 一共最多开 2 个，上限永远不触发。要给出：权威检查点（应在 `create_attempt` 事务内）+ demo 的实际参数。

**P1-13 · "公平轮转"在现状下几乎是空操作**
D6-1 的"按 `created_at` 轮转起点"只改 `_cycle_inner` 的 Mission 遍历顺序；真正的门是 `_decide` 里每 Mission 各自独立的 `concurrency_limit=self._config.max_concurrency`（`event_handler.py:1781`），对所有 Mission 是同一个常数，先遍历的 Mission 并不会多拿。轮转只有在**引入全局上限之后**才有意义（先遍历者会先占满）。计划要写明：轮转是为全局上限服务的，两者必须同批落地；否则 S6-01「均有进展」靠的是现状而不是本步新机制。

**P1-14 · fixtures 缺三样关键能力，S6-03 的"回显证明"现在没法测**
`testing/fixtures.py:68` 的 `RoleScriptedProvider` 是按 role 脚本化、同步返回的，没有延迟钩子、没有连接错误钩子，**也没有回显自己 model 名的能力**。而 `echoed_models`（`agent_worker.py:145-153`）读的是 `record.response_json["model"]` —— 如果 fixtures 不回显，`echoed` 为空集，`if echoed and …` 直接短路，"实际 provider/model 改变由回显证明"这句话在 fixtures 上是空的。计划只写了 `FailingProvider(kind="unavailable", times=N)`。必须补：① 每个 fixture provider 回显自己的 model 名（否则 S6-03 退化成只验标签，正是纲要禁止的）；② 放慢 Critic 的实现位置（provider 内 `asyncio.sleep`，还是 `critic_wait_seconds`）；③ 这些 sleep 与 `_critic_wait=120`、`turn_deadline_seconds=900`、`test_timeout` 的关系。

**P1-15 · 并发验证 + `Store.transaction()` 的可重入锁 = 静默数据损坏的入口**
`storage/store.py:267-291`：`threading.Lock` + 实例级 `_depth` 计数实现可重入事务。单线程 asyncio 下，只要有一个 `with store.transaction()` 块内部出现 `await`，另一个协程进入 `transaction()` 会在 `self._lock` 上阻塞整个事件循环（死锁）；更糟的是两个协程交错时，`_depth` 会把 B 的事务误认成 A 的嵌套事务而不提交。现在恰好安全（CommitService 的方法全是同步的、事务内无 await），但 D6-9 一引入并发验证，任何一次"在事务里 await"就是静默损坏。计划 §5 写的"每次 Commit 是短事务"是**假设**，需要变成**断言**：在 `transaction()` 里记录持有者 `asyncio.current_task()`，重入时校验是同一个 task，并加一条不变量测试。

---

## P2（登记 / 术语 / 测试 / 可砍范围）

**P2-1 · 接手的遗留只登记了 2/8 条。** 计划正文只提到 L2-6（D6-12）与 L3-5（§2 不做）。归属"第 6 步"而计划完全没提的还有：
- **L2-4**「`run_tests` 子进程无网络隔离；只有 env 白名单 + cwd + 超时」（归属"第 6/7 步沙箱"）—— D6-7 的 `DeploymentPolicy` 只有 `allowed_tools / max_tool_calls_per_attempt / max_file_bytes / denied_path_prefixes`，没有网络。
- **L2-7**「每 Attempt token 预留不是硬上限」—— D6-8 加了 tool_calls 和 runtime，没碰这条；硬上限要靠 `hard_cap_micros`，而它需要价格表（见 P1-9），循环依赖。
- **L3-3**「在途 UNKNOWN 出站调用 …… 第 6 步做对账通道」—— `settle` 在有 UNKNOWN 用量时**永远拒绝结算**（`budgets.py:325-328`），Mission 会带着一条永不释放的预留结束，`costs.json` 表现为 reserved 不归零。这正是 S6-07 要断言的那张表，必须有处置。
- **L4-3**「系统预留只覆盖 token 维度；成本维度上综合/冲突任务无预留；`assert_no_secrets` 只查字段名」—— 只有最后半句被 D6-10 覆盖（`context_builder.py:358-375` 确实只查字段名）。
- **L4-4** 见 P1-8。
- 处置：plan 加一节"§5 接手的遗留 → 本步处置（做 / 推迟到第 X 步 + 理由）"，逐条回答。L3-4（老化 + 背压上限）已由第 5 步 allocator 的 aging + 本步 D6-2 覆盖，可标关闭。

**P2-2 · `DEPLOYED_LAYERS` 是第二套真值。** 未部署层的提交期拒绝**已经实现**：`STEP2_IMPLEMENTED_LAYERS`（`contracts/models.py:42`）+ `task_graph.py:264` + `graph/changes.py:507` + `commit_service.py:461`；运行期也已经有 `LayerResult(layer, ERROR, "layer not deployed in this build")`（`verifier_router.py:150-151`）。D6-9 新引入 `DEPLOYED_LAYERS` 会造成两处常量。真实增量只有两点：拒绝 reason 改名为 `verification_policy_undeployed`；运行期从"ERROR → 走重试"改成"ERROR → `stop_task(VERIFIER_UNAVAILABLE)` 不重试"。计划要写成"改既有常量/路径"，否则实现者会新建一套。

**P2-3 · 术语与裁定的三处瑕疵。**
- `verifier_workers`（D6-3 ⑤）："Verifier Worker" 在术语目录查无定义（简报 §4 末已指出），原文只有 §29.1 的拓扑数字"2 个"。把它当成 asyncio 并发度是纯实施约定，§6.1 登记了执行池却漏了这一条，补上。
- D6-3 ② 把"暂停低优先级任务"实现成"只放行 tier 0/1"。`allocator.py:163` 的 tier 1 是**饥饿档**（等满一个 aging window），tier 2 是**公式档**。语义上可以接受，但计划写"公式档任务不分配"要补一句"公式档 = §29.3 打分档，不等同于低优先级"，否则和理论 05-10 的措辞对不上。
- D6-3 ⑥ 登记"合并重复候选"不做，理由写的是"第 5 步去重校验已阻止重复任务入图"——**答非所问**。§18.5/§20.3 说的是候选 **Artifact** 的合并，不是任务去重。改理由，或直接写"本步不做，归第 8 步"。

**P2-4 · 52 条原文规则，计划对得上的不到一半，且无法核对。** 明显没有落点的至少有：3.7（§9.1 搜索多样性：扩并发必须同时扩多样性）、3.12（§10 第 9/10 项——权限交集结果与本 Attempt 预算必须出现在任务包里，计划没说 `build_worker_package` 是否带）、3.24（§18.4 每 Attempt 记录"工具调用 / 运行时间 / 是否进入最终成功路径"——"运行时间"无落点）、3.28（§18.6 进展信号 6 项是 S6-01「均有进展」的定义来源，计划把"均有进展"直接降成"都 COMPLETED"，这是偷换还是收紧，要登记）、3.41（§23.2 的"误报率""污染率"）。建议加一张"52 条 → 决定 / 登记 / 不做"的对照表，作为独立 review 的可核对面。

**P2-5 · 角色配比归并掩盖了原文规则。** D6-11 把 Connector/Simplifier/Failure Analyst 归入 Exploiter 份额；但 §29.2/§8.3 的动态调整规则明确写"中期：Exploiter 和 **Connector** 增加"（简报歧义 5），归并之后这条在 `metrics.json` 里不可观测。建议：配比表按 §9.2 的 8 个角色列全，无百分比的三个显式记 `null`。

**P2-6 · 建议砍掉的范围。**
- D6-3 ⑤「RAISED 时 `verifier_workers` 2 → 4」：在 Verifier 是瓶颈（慢 Critic）的场景里，提高并发只会同时多花 Critic 预算，而且和 ①「降低 Worker 并发」叠加后 S6-02 的"Worker 并发下降"更难观测。建议本步只做 ①②③④，⑤ 登记不做。
- D6-10 的 `metrics.json` 十项里，unpriced 下"费用"列恒空（见 P1-9）；建议本步只出能填满的列，空列显式写 `null` 并注明原因。
- D6-12 的真实两模型报告依赖 P1-9 的裁定；若确认 unpriced，报告的"费用"列写 null 并说明，不要留成半成品。

**P2-7 · 测试覆盖建议。**
- `evaluate()`（`backpressure.py:163-200`）的滞回单测要覆盖两条边界：观测落在 `(low, high)` 开区间时保持上一状态；`max_pending_verifications=1` 时 `low = 0`（`:64-65`），意味着必须完全排空才 CLEARED —— 这是不是想要的，要有一条测试固化。
- `reserve()` 的多维失败各一条，并断言"抛异常时没有任何 `_apply` 生效"（检查循环与应用循环是分开的，语义正确，但需要测试锁住）。
- S6-08 的断言应是"`execution-large.db` 里**根本不存在**这个 agent 记录"，而不是"large 池没恢复它"——后者可能只是恰好没跑到。
- 新增"事务内不得 await"的不变量测试（见 P1-15）。
- S6-05 需要一条"信封/消息文本声称已授权"的用例，并断言 `gateway.calls` 里 `outcome == "rejected:not_allowed"` 且交集本身未被改动（§21.3「外部内容不能改变系统权限」）。

---

## 结论

**不能直接进入实现。** 计划的骨架（D6-1…D6-13 与 S6-01…S6-09 的对应、§6.1 的实施约定登记）是站得住的，歧义 1/2/3/6/7/8/13 的裁定基本正确且都按 ORCH §13 登记为"非原文原句"，没有把实施约定冒充成原文；但它对**现有代码的事实**做了若干错误假设，其中至少六条会直接导致某条 S6 验收跑不出来，而且工作树里已经落地的切片 A 已经踩中了 P0-2。

必须先改（按顺序）：

1. **P0-2** —— 现在就是坏的：`max_tool_calls` 补进 `normalise_budgets`（`task_graph.py:181`）、`inherit_limits`（`manager.py:134-144`），并给 Global→Mission 补继承。
2. **P0-1** —— `reserve()` 加显式 `mission_id` 形参。
3. **P0-3** —— D6-4 列出 11 个 `self._config.model` 改造点，规定 expected 取自冻结的 intent.profile。
4. **P0-4** —— D6-5 补 `RuntimePools` 的改造面清单（共享 gateway/workspaces + 单 provider 兼容路径）。
5. **P0-5** —— 重定 S6-06 的"有界等待"边界（注入时钟或改成即时降级）。
6. **P0-6** —— 决定 S6-02 的触发维度，或把"验证异步化 + 计入 in-flight"提到切片 A。
7. **P1-1 / P1-2 / P1-3 / P1-4 / P1-5** —— 五条措辞与归因修正，其中 P1-5 直接决定 S6-07 能不能写成测试。
8. **P1-8 / P1-9 / P1-10 / P1-12 / P1-15** —— 错误分类表、价格表裁定、`trace_id` 裁定、全局上限检查点、事务不变量测试。

其余 P1（P1-11/13/14）与全部 P2 可以随切片处理，但 **P2-1 的遗留登记表必须在计划定稿前补齐**——现在 8 条里只交代了 2 条，其中 L3-3（UNKNOWN 预留永不释放）会直接出现在 S6-07 要检查的那张 `costs.json` 上。
