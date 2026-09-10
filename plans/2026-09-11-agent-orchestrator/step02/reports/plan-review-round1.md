# step02 plan 独立 review 原文（子代理 claude-opus-5，2026-09-11）

> 中文摘要：5 条 P0（阻塞判失联、Attempt FAILED 不存在、usage_refs 少算、intent 只冻 hash、漏 recover_pending_turns）、19 条 P1、3 条 P2；处置见 journal.md §1、plan.md §7。

---

已完成审阅（读了理论合订版 05/06/09/10/11、原文 §5/§10/§12–21/§24–27、ORCH-BUILD §0–4/§12–15、program.md、step02/plan.md+acceptance.md，并在代码里核了 SDK 事实）。

---

## 一、发现清单

### P0（不修就过不了验收 / 结论会错）

**1. D6 的续租规则与 S2-08 直接矛盾，会把"阻塞"误判成"失联"并重复扣费。**
D6 写"续租条件：turn_snapshot 状态仍为 RUNNING/QUEUED **且未 blocked**…租约过期→LOST→新 Attempt"。但 SDK 里 provider UNKNOWN 的表现恰恰是 `RunState.WAITING + wait_blocker`（`src/simple_harness/runtime/drivers/react.py:264-271`），投影到 `turn_snapshot.blocked=True`（`src/simple_harness/agents/base.py:166-195`）。按 D6，S2-08 的 UNKNOWN 一定会走成 LOST→新 Attempt，违反 ORCH §12.1 表"旧执行仍可能产生外部副作用…不得重复未知业务动作"和 §12.2"在途 UNKNOWN 预算保持占用"。
**改**：把 `blocked=True 且 blocker.kind ∈ {provider, tool}` 定义为"存活但阻塞"——续租、不释放预留、不 LOST；LOST 仅在 turn 不存在 / agent 打不开 / 无 blocker 且 ordinal 长期无推进时触发。另外存活状态集合必须补 `result_pending`（`contracts.py:38-43` 的枚举是 queued/running/result_pending/committed/failed），否则正在落库的 turn 会被判死。

**2. D7 的 `Attempt FAILED(reason=envelope_invalid)` 是一个不存在的状态。**
§25.2 与 D15（"Attempt 严格用 §25"）都没有 Attempt FAILED；仓库里已写好的 `src/agent_orchestrator/contracts/state_machines.py:47-67` 也明确注释"§25.2 defines no separate Attempt FAILED state and we do not invent one"，只有 `RETRY_WAIT`。D7 与 D15 自相矛盾。
**改**：统一为 `RETRY_WAIT` + `failure_reason=envelope_invalid`；若确要新增状态，按 ORCH §13"任何不同选择都要单独登记"写进 D15。

**3. D10 的费用口径（按 `usage_refs` 逐条导入）系统性少算。**
`usage_refs` 只放**本轮最后一次** provider 请求：`src/simple_harness/agents/execution.py:614-618` 与 `:665-669` 都是 `(f"provider-request:{response.request_id.value}",)`。一个多步 ReAct turn（写文件→跑测试→出 Envelope）会产生 N 次 provider invocation，但 `usage_refs` 只有 1 条。按 D10 导入 = 只记最后一次的钱，违反 ORCH §12.2 的费用事实层要求。
**改**：Attempt 级费用权威改用 `runtime.uow.read_provider_budget(RunId(agent_id))`（`src/simple_harness/execution/sqlite/uow.py:7659-7684`，返回 `committed_micros / reserved_micros / has_unknown_charge`，按 run 聚合；因 `creation_key=attempt_id ⇒ run_id==agent_id`，它天然就是 Attempt 粒度）。`usage_refs` 降级为交叉核对。`has_unknown_charge` 正好是 S2-08 的判据。顺带：plan §5 担心的"缺按 usage_ref 查的门面"并不需要新增 SDK 方法——`AgentRuntime.uow` 是公共属性，`read_provider_budget` / `read_provider_invocation` 都是公共方法，`provider_invocation_id(RunId, RequestId)` 也是公共函数（`execution/provider_invocations.py:151`）。只有"逐条列出某 run 全部 invocation"没有公共门面（`_provider_invocation_by_logical_call` 是私有）。

**4. D5 只冻结"输入包 hash / 配置版本"，S2-04 无法成立。**
`AgentRuntime.create` 在 creation_key 相同但 `config_hash` 不同时直接 `ValueError`（`runtime.py:483-484`）；`submit` 在 input_id 相同但内容不同时抛 `AgentInputConflict`（`base.py:136-137`）。而 D-B4 的 context_builder 要把预算数字、Verifier 反馈等塞进输入包——重放时若重建，字节几乎必然漂移。冻 hash 只能"发现漂移"，不能"重放成功"。
**改**：dispatch intent 必须持久化**完整的 AgentConfig JSON 和完整的输入 Message JSON**（字节级），dispatcher 重放时直接读出来用，禁止在派发时重建。

**5. D14 的 `recover()` 漏了 `AgentRuntime.recover_pending_turns()`。**
`runtime.py:465-468` 明确："Wake every Agent with queued/running/result_pending turns (closure E6)"。编排进程和 SDK 执行在同一进程内，进程死了 turn 也停了；不调用它，S2-04/S2-05 崩溃后 turn 永远不会继续，测试只会挂或超时。
**改**：`Orchestrator.recover()` 第一步就是 `await runtime.recover_pending_turns()`，并在 S2-04/05 的断言里加"重启后 turn 最终 committed"。

---

### P1（实现前必须定清楚）

**6. 硬预算没有下沉，ORCH §12.2 的"无法落实成真实调用限制就不能宣称硬预算已完成"未被满足。**
`AgentLimits.lifetime_cost_limit_micros` 是死字段：`AgentLimits.termination_limits()` 全仓无调用（grep 确认），driver 用的是 runtime 级 `ports.termination_limits`，且 `_turn_limits` 里 `max_cost_micros=lifetime.max_cost_micros` 原样透传（`agents/execution.py:994-1000`），默认 10^10 micros。**唯一可用的下层硬限额**是 `ConsumerRuntimePolicies.budget_policy = BudgetPolicy(hard_cap_micros=…, refuse_on_unknown=True)`——它按 run 快照判定（`budget.py:136-166` + `uow.py:7659`），因每 Attempt 一个 Agent = 一个 Run，等价于"每 Attempt 硬上限"，但**整个 runtime 只能有一个值**。
**改**：D10 补一条：本步所有 Attempt 预算统一为同一个 `hard_cap_micros`（或每种预算档一个 runtime）；同时把 `max_model_calls_per_turn / max_tool_calls_per_turn / turn_deadline_seconds` 按 Attempt 预算写进 `AgentConfig.limits`（这三个确实按 Attempt 生效，见 `_turn_limits`）。

**7. 注入的 `FrozenPriceEstimator` 的 `pricing_key` 必须正好是 `"consumer"`。**
`_ConsumerProviderAdapter` 把 target 的 `pricing_key` 硬编码成 `"consumer"`（`runtime/consumer_adapter.py:221-227`），`FrozenPriceEstimator.bind` 不匹配即 `ValueError`。D10"CLI 从 provider 配置读价格表"要写死这条，否则真实模型一启动就崩。

**8. `ports.model` 必须与 provider 回显的 `response.model` 完全一致，否则每次调用都变 UNKNOWN。**
`dispatch.py:645-665`：只有 `response.usage is not None and response.model == target.model` 才用 estimator 计费，否则 `BudgetCharge.unknown()`；而 `refuse_on_unknown=True` 会让**下一次**调用直接 `BudgetUnknownError`。真实 OpenAI 兼容端点常回显带版本后缀的 model 名——这会让 S2-01 的真实模型跑变成常态化的 S2-08。计划必须记录 model 回显核对与处置（对齐 `SH_MODEL`，或显式接受 UNKNOWN 并 fail-fast）。

**9. D13 完全没提 `tool_schemas`，工具将无法传参。**
`consumer_adapter.py:258-267`：没有在 `AgentRuntimePorts.tool_schemas` 里声明 schema 的工具，会拿到"零参数闭合 schema"；且 `ToolSpec.description` 被硬编码成 `f"Tool: {name}"`（同文件 :270），模型看不到任何工具语义。
**改**：D13 补 4 个工具的 JSON Schema（SDK 子集禁止 `additionalProperties`），并明确工具语义只能靠 `AgentConfig.instructions` 传达。另外 `AgentRuntimePorts.tool_names` 是**全 runtime 一份**（registry 在 `assemble_runtime` 时一次性封口），per-Agent 收窄只能靠 `AgentConfig.tool_names` 的曝光集（`runtime.py:141-153`）——D13 的"求交集后才暴露"要说明落在 config 上。

**10. D13 的 `run_tests` 子进程参数不可行。** `python -m pytest` + env 只留 PATH/HOME：`python` 未必是当前解释器，剥掉 PYTHONPATH 后可能找不到 pytest。改用 `sys.executable -m pytest -q -p no:cacheprovider`、`PYTHONDONTWRITEBYTECODE=1`、`start_new_session=True` + 超时 kill 进程组、cwd 固定。

**11. D12 的"只读副本"与 D9 的 `code_test` 冲突。** pytest 需要写 `__pycache__` / `.pytest_cache`；字面 chmod 只读会直接失败。语义应改为"Worker 不可触达的独立副本"，写权限归验证进程。

**12. D13 的工作区解析只覆盖 Worker，漏了 Critic。** tool ctx 只有 `run_id/request_id/call_id`（`consumer_adapter.py:280-285`），而 Critic 是**另一个 run**（`creation_key=<attempt_id>:critic`）。要把映射定义成 `run_id → (attempt_id, 视图, 读写权限)` 三元组，Critic 映到 `<attempt_id>-verify/` 且只读——否则 §10.2"Verifier 应尽量独立"和 D9 的独立性都落不了地。

**13. Task 状态机缺 S2-06 需要的边。** §25.1 与已实现的 `_TASK` 表（`state_machines.py:115-124`）里 `VERIFYING → FAILED`、`VERIFYING → CANCELLED` 都非法。但"最后一次 Attempt 验证 FAIL 且预算耗尽"和"验证中 cancel Mission"必然处在 VERIFYING。D15 必须写死路径：`VERIFYING → ACTIVE → FAILED/CANCELLED`，并在 S2-06 断言里体现。

**14. Critic / 验证阶段的预算归属缺失。** ORCH §12.2 点名"Planner、Manager、Synthesizer、Critic、Verifier…都要有明确预算归属"。D11 只给 Planner 配了 Reserve/Settle，D9 的 `critic_review` 没有。S2-02 的"两次 Attempt 费用都归 Mission"也没覆盖 Planner/Critic。

**15. Mission 级成功判定被悄悄收窄成"唯一 Task PASS"。** ORCH §12.4 末段明确："Mission 停止条件里的 Verifier PASS 必须对应满足 Mission 根成功条件的候选；某个中间子 Task PASS 只完成该 Task"。D8/D15/S2-01 全篇只有 Task 级判定，Mission COMPLETED 是自动推出的。这正是"建完表也算完成"式收窄，而且第 3 步 S3-06（局部全过但整体不达标）会因此没有地基。
**改**：Commit 交付前必须对 `Mission.success_criteria` 做一次独立判定（哪怕第 2 步只是规则层），并在 S2-01 里断言这条事件。

**16. 版本关系未定义，违反 ORCH §13 最后一句（"Task Contract 和输入、产物、Review 的版本关系必须在第 2 步定义"）与原文 §17.3。** D3 里 `base_version` 只在 `commit_id` 的哈希输入中露了一次面。计划里没有 `mission.version` / `task.version`（§26.1/§26.2 都有 `version: integer`）、没有 §26.3 要求的 Attempt `prompt_version` / `context_version`、没有 artifact `version`（§20.2）、没有"这次 verification 验的是哪个版本"。

**17. Event 词表未固定，且验收里用了原文没有的事件名。** acceptance 用了 `MissionCompleted` / `ResultRejected`——§16.2 的清单里没有；而 §16.2 要求的 `HeartbeatReceived` / `BudgetReserved` / `BudgetReleased` 在 8 条验收里一次都没出现。ORCH §13 要求偏离必须单独登记。D2 只列了 `events` 表名，没列类型枚举。

**18. D-A4 漏掉 ORCH §4.2 对 `api/missions.py` 的两条硬要求**："校验成功条件、预算和工具"和"**不接受任意 Agent 自报 tenant**"。计划只写了 `create/get/cancel/events` + 幂等。

**19. D18 的 fixture provider 方案在多 Agent 下不确定，且造不出 S2-08。** `tests/agents/provider_fixture.py:31-53` 是全局有序 `script.pop(0)`；本步有 Planner / Worker×2 / Critic 四个 Agent 共用一个 provider 实例，脚本必须按 `(role, ordinal)` 或 `run_id` 分派。S2-08 需要一个额外 fixture：provider 在 handoff 后抛任意非 `_DEFINITE_PROVIDER_FAILURES` 异常即 `settle_unknown`（`dispatch.py:576-578`），这是最省事的 UNKNOWN 制造器。另外 fixture 的 `MODEL="agent-model"` 必须与 `AgentRuntimePorts.model` 默认值保持一致（见发现 8）。

**20. 崩溃点矩阵不完整，且注入点选错了库。** D-D1 只有 `Store.fault(point)`（编排库内），但 S2-04/S2-05 的崩溃点在**两库之间**。必须把 6 个跨库点列成验收表并各配一个注入钩子：① `runtime.create()` 返回后 / 编排写 `AGENT_CREATED` 前；② `submit()` 返回后 / 写 SDK 回执前；③ SDK turn 已 committed / 编排写 `ResultSubmitted` 前；④ ResultSubmitted 后 / 验证前；⑤ 某一验证层 PASS 后 / 写 `verifications` 前；⑥ Commit 事务中途。

**21. S2-03 的幂等点少列 4 个**：同一 `usage_ref`/预算重复导入、同一 artifact 重复登记、同一 verification 重复执行、同一 Planner 提案重复 Commit。D10 提了 `imported_usage_refs`，但验收没覆盖。

**22. D1 的打包收尾不完整（ORCH §1.2 逐项列举了）。** 实测：`pyproject.toml:104-139` 的 mypy `files` 没有 agent_orchestrator；`REUSE.toml` 的 annotations 不覆盖 `src/agent_orchestrator/**`；缺 `src/agent_orchestrator/py.typed`（`src/simple_harness/py.typed` 存在）；`scripts/check_source_provenance.py:107` 只认 `src/simple_harness/` 前缀（新包是净新增、无上游来源，需显式登记这个结论）；`[tool.hatch.version]` 仍取 `src/simple_harness/version.py`，而 `scripts/verify_release_gate.sh` 按它拼 wheel 名——**是否把 SDK 版本从 0.8.0 抬到 0.9.0 未定**，不抬就会和已发布的 0.8.0 wheel 撞名，"干净 venv 装 wheel"的证据不可信。

**23. Planner / Critic 的重试身份缺失。** D4 固定 `creation_key="<mission_id>:planner"` + `input_id="attempt-input"`。规划失败后的第二次规划：同 key 不同 config → `ValueError`（`runtime.py:483`）；同 agent 同 input_id 不同内容 → `AgentInputConflict`（`base.py:136`）。Worker 因为一 Attempt 一 Agent 侥幸没事，Planner/Critic 必须带序号（`:planner:1` / `:critic:1`）。

**24. `result_id` 双键规则未落地。** ORCH §13 第一行要求"§13 `result_id` 与 §26 `id` 同时存在且不同则拒绝，不静默取一个"。D3 由系统派生 `result_id`，D7 又要求 Envelope 里带 `id`——两者不一致时以谁为准、是否拒绝，计划没写。

---

### P2

**25. `AgentRuntime.create()` 不校验 tool_names。** 只有 `create_many` 校验（`runtime.py:575-584`）；工具名拼错会静默变成"这个 Agent 没有工具"，Worker 会看似"想不出办法"。编排层要自己断言 `config.tool_names ⊆ runtime.tool_names`。

**26. 两套租约的关系没写。** `AgentRuntimePorts.lease_ttl_seconds=30.0` 是 SDK 执行租约，D6 的 60s 是编排业务租约。ORCH §12.1 要求"业务租约与 SDK 执行租约分别保存，最终提交同时检查适用身份/版本"——D6 只描述了业务侧。

**27. D9 的层序与成本。** §14.1 层序是 格式→规则→Critic→测试，D9 照抄；但把 LLM Critic 排在确定性 `code_test` 之前，S2-02 的错误代码会先烧一次 Critic 的钱。是否短路（任一必需层 FAIL 即停）计划未定。

---

## 二、计划应当显式记录、目前只是隐含的 6 个决定

1. **Attempt 硬预算的落地方式**：`ConsumerRuntimePolicies.budget_policy=BudgetPolicy(hard_cap_micros=X, refuse_on_unknown=True)` 按 run 生效 ⇒ 等价 per-Attempt 上限，但全 runtime 只有一个 X；本步接受"所有 Attempt 预算统一"，还是每档预算起一个 runtime。
2. **费用权威口径**：Attempt 实际费用 = `uow.read_provider_budget(RunId(agent_id))`；`usage_refs` 仅交叉核对；"unpriced" 由 `policies.pricing_mode == "unpriced_local"` 判定，**不是**从账本里读到 0 就当免费（0 费率 estimator 写出来的是 `TRUSTED_USAGE / 0`，不是 unknown）。
3. **dispatch intent 冻结的内容**：完整 `AgentConfig` JSON + 完整输入 `Message` JSON 的字节，而非仅 hash + 版本号；重放读取而非重建。
4. **Mission 级成功判定独立于唯一 Task 的 PASS**（对齐 ORCH §12.4，为第 3 步 S3-06 留接口）。
5. **版本模型**：`mission.version` / `task.version` / Attempt 冻结的 `task_version + context_version + prompt_version` / `artifact.version` / `verification.verified_version` 的定义与写入时机（ORCH §13 明文要求第 2 步定）。
6. **新包的合规收尾**：`py.typed`、mypy `files`、REUSE annotations 三处必须加；`source-manifest.tsv` 因新包无上游来源而不加（写明结论）；SDK 发布版本号是否 0.8.0→0.9.0。

（第 7 项若还有余量：Envelope 的 `id` 以系统派生为准还是以模型自报为准，以及 `result_id`/`id` 双键冲突拒绝规则。）

---

## 三、总体判断

不能按现状实施。范围与结构是对的——D1–D18 覆盖了 ORCH §4.2 的模块表、§4.3 的提交顺序骨架、§14.3 的 CLI 与证据目录，D2/D5/D7 抓住了"跨库身份与幂等"这个真正的主要矛盾，也没有把 Blackboard/动态图提前——但有 5 处 P0 会让验收在真机上直接不成立：D6 的续租规则与 S2-08 语义互斥（阻塞被判成失联、重复调用重复扣费）、D7 用了 §25.2 里不存在的 Attempt FAILED、D10 的 `usage_refs` 口径系统性少算多步 ReAct 的费用、D5 只冻 hash 导致 S2-04 重放必然撞 `config_hash` / `input_hash` 冲突、D14 漏掉 `recover_pending_turns()` 导致崩溃后根本恢复不了。再加上 P1 里三处 ORCH 明文要求被跳过（§12.2 的下层硬限额、§12.4 的 Mission 级成功判定、§13 的版本关系定义）和 D1 的打包收尾只做了 hatch 一项，当前 plan 若照做，交付物会停在"表建好了、fixture 跑通了"，而 S2-04/05/08 这三条恰恰是本步"可靠"二字的全部含金量。把上述 5 条 P0 + 发现 6/15/16/20/22 补进 plan（多数是补决定和补断言，不是改架构），这份计划就能作为 ORCH-BUILD 要求的那个"单一 Step 2 版本"整体实施。