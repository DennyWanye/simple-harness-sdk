# 第 2 步：单 Task Mission 的可靠验收闭环 · 实施计划

- 日期：2026-09-11 · 基线 `dffd13c`（0.8.0）
- 原文依据：§2、§4、§5、§12–18、§20–21、§24–28；ORCH-BUILD-v1.0 §4、§12–15
- 术语：见 `../program.md` §0；本文件不重复定义

## 1. 主要矛盾

要让"交办一件工作 → 做完 → 验收 → 交付/带原因停止 → 任意关键点崩溃可恢复"成立，编排层必须成为**正式状态的唯一写入者**（Proposal/Commit），但真正干活的 BaseAgent 生活在另一套权威账本（SDK `execution.db`）里，两边没有原子事务。矛盾的主要方面是**跨库闭环的身份与幂等**：每次派发、每份结果、每次 Commit 都要有稳定身份，重放不产生第二次执行、第二份交付或第二次扣费。其余（Planner、Verifier、预算、工作区）都围绕这一条组织。

## 2. 范围（本步交付的整体，不可拆成"建完表也算完成"）

用户提交 Mission「在隔离工作区实现一个字符串解析函数，并通过给定测试」→ Planner（一个 BaseAgent 角色）提出**一个** Task Proposal → Commit → Reserve → 创建 Attempt 并派给 Worker BaseAgent（隔离 Workspace，只有工作区读写与跑测试工具）→ 收 Result Envelope + Artifact → Verifier（格式→规则→独立 Critic→真实代码测试，按 Task `verification_policy` 选层）→ FAIL 则带反馈的修复 Attempt（`retry_of`），直到 PASS 或 `max_attempts`/预算耗尽 → Commit 并交付；任意关键点重启后继续，已接受结果不重跑。

不做：多 Task DAG、Blackboard、动态改图、多 Mission 并发、真实外部修改、人工审批、学习。

## 3. 关键设计决定（D 开头；有争议的交独立 review 裁决）

| # | 决定 | 依据/理由 |
|---|---|---|
| D1 | 新包 `src/agent_orchestrator/`，目录按原文 §27 + `contracts/`、`storage/`、`__main__.py`；加入 hatch wheel `packages`；版本 `agent_orchestrator/version.py = "0.2.0"`（与步骤号对齐，0.N.x = 第 N 步） | ORCH §1.2 |
| D2 | 编排库 `orchestrator.db`（SQLite，STRICT，WAL），`storage/schema.py` 单一 DDL + `orch_schema_migrations` 描述符；所有写入经 `storage.Store.transaction()`，由 `CommitService` 调用；事件表 `events` 与当前状态表（`missions/tasks/attempts/results/claims/artifacts/budget_accounts/budget_reservations/dispatch_intents/leases/commit_receipts/verifications`）同事务写 | 原文 §16.3；ORCH §1.3 |
| D3 | 身份：`mission_id = "mission-" + sha256(tenant_id, idempotency_key)[:16]`；`task_id = mission_id + ":task-" + 序号`；`attempt_id = task_id + ":attempt-" + 序号`；`result_id = "result-" + sha256(attempt_id, sdk turn_id, result_hash)[:16]`；`commit_id = "commit-" + sha256(proposal canonical json, base_version)[:16]`；`event.idempotency_key` 由事件类型 + 主体 id + 序号构成 | 原文 §17.4；ORCH §4.3 |
| D4 | 与 SDK 的映射：`creation_key = attempt_id`，`input_id = "attempt-input"`，预期 `turn_id = agent.turn_id_for(input_id)`，Planner/Critic 同理（`creation_key = "<mission_id>:planner"`、`"<attempt_id>:critic"`）；每个 Attempt/Planner/Critic 调用各用一个独立 BaseAgent（ORCH §12.5：不复用实例） | ORCH §2、§4.3 |
| D5 | 派发协议：编排事务内原子 Reserve + 创建 Attempt(PENDING) + `dispatch_intents`(PENDING, 冻结输入包 hash/配置版本/creation_key/input_id) → dispatcher 领取（CAS PENDING→CLAIMED 带 lease）→ `runtime.create()`（重试沿用 key）→ 写 `AGENT_CREATED` + agent_id → `submit()` → 写 `SUBMITTED` + SDK 回执（turn_id/seq）；任一点崩溃后重放同一 intent，不创建新 Attempt | ORCH §4.3 第 1–3 条 |
| D6 | 心跳与租约：Attempt 租约 `lease_ttl` 默认 60 s；续租条件是执行者**实际存活**：`agent.turn_snapshot(turn_id)` 状态仍为 RUNNING/QUEUED 且未 blocked，或 SDK 侧 `provider_turn_ordinal_to` 有推进；调度器不得凭空续租。租约过期且 turn 已不可见/无推进 → Attempt LOST → 新 Attempt(`retry_of`)（本步只做单执行者，LOST 路径的完整并发场景在第 3 步 S3-07） | 原文 §17.6；ORCH §12.1 |
| D7 | 结果采集：collector 只用 `get_result/turn_snapshot`；`wait_turn` 超时不触发 LOST；`AgentTurnResult.public_output` 必须含且只含一个 `<result_envelope>{json}</result_envelope>` 块，按 §26.4 严格解析（`id/task_id/attempt_id/outcome/summary/claims/evidence/artifacts/proposed_tasks/used_knowledge/risks/cost`）；核对 `task_id/attempt_id` 身份、`artifacts` 引用的产物 hash 存在且匹配、`usage_refs` 来自 SDK 账本；解析或核对失败 → 该 Attempt `FAILED(reason=envelope_invalid)`，进入有预算的修复 Attempt（反馈里带具体错误），不猜成 PASS | ORCH §4.3 第 4–5 条；§13 |
| D8 | Attempt 提交成功只到 `SUBMITTED`；`VERIFYING` 由验证队列驱动；Verifier verdict=FAIL 不能完成 Task；Task 只有在 `verification_policy` 全部必需层 PASS 并被 Commit 接受后进入 COMPLETED；状态机严格按原文 §25.1/§25.2 | ORCH §4.2 末段 |
| D9 | 验证层（本步实现四层，形式化/人工写 NOT_REQUIRED）：`format_check`（Envelope schema）、`rule_check`（确定性：产物存在、hash 一致、成功条件里列出的文件/测试存在、claims 非空且引用证据）、`critic_review`（独立 Critic BaseAgent：只看 Task Contract + 只读产物副本 + 测试输出，不看 Worker 的自我解释；输出 `<critic_verdict>` JSON）、`code_test`（在**只读验收副本**上以子进程跑 `pytest`，超时/无网络参数、隔离 env）；每层结果 PASS/FAIL/NOT_REQUIRED/ERROR 入 `verifications` 表；未运行的必需层不能算 PASS | 原文 §14.1；ORCH §12.4 |
| D10 | 预算两层：编排 `budget_accounts`（mission/task/attempt 三级，字段 `max_tokens/max_cost_micros/max_attempts/max_runtime_seconds/max_concurrency`，null=不限）+ `budget_reservations`（Reserve/Settle）；实际费用只从 SDK `provider_invocations`（按 `usage_refs = provider-request:<request_id>` 逐条导入，`imported_usage_refs` 去重）读取；`pricing_mode="unpriced_local"` 时 cost_micros=null 且 receipt 标 `unpriced`；付费 provider 必须注入 `FrozenPriceEstimator`（CLI 从 provider 配置读价格表） | ORCH §12.2 |
| D11 | Planner 本步只产单 Task Proposal（BaseAgent 角色 `planner`，输出 `<task_proposal>` JSON：goal/rationale/success_criteria/verification_policy/allowed_tools/budget）；Commit Service 校验：与 Mission 关系、allowed_tools ⊆ Mission、预算 ≤ Mission、verification_policy 非空；Planner 调用也 Reserve/Settle、可恢复（intent 类型 `plan`） | ORCH §4.2 |
| D12 | Workspace：`<evidence_root>/workspaces/<attempt_id>/`（每 Attempt 独立可写）；Artifact = 文件路径 + sha256 + version，存 `artifacts` 表；验收在 `<attempt_id>-verify/` 只读副本上进行；正式版本只在 Commit 时登记 `accepted_artifacts`，Worker 不能改正式版本 | 原文 §20 |
| D13 | 工具（Tool Gateway 的第 2 步形态）：编排层实现 `ToolExecutorPort`，工具名 `workspace_read_file/workspace_write_file/workspace_list/run_tests`；按 `run_id → attempt` 解析工作区，路径必须在工作区内（拒绝 `..`/绝对路径/符号链接逃逸）；`run_tests` 子进程 `python -m pytest -q` 于工作区、超时 120 s、`env` 只保留 PATH/HOME 白名单；Task `allowed_tools` 与 Mission `allowed_tools` 求交集后才暴露；Critic 只给 `workspace_read_file/workspace_list`，Planner 无工具；`agent_delegate` 一律不暴露 | 原文 §21；ORCH §12.5 |
| D14 | 调度器：单进程 asyncio `Orchestrator.run(until_idle=True)` 循环：`recover()` → 领取 dispatch intents（并发上限 `max_concurrency`）→ 采集结果 → 验证队列 → 管理决策（修复/停止）→ 空闲退出；每个动作是幂等的 Commit；可反复启动 | 原文 §16.4 |
| D15 | Mission 状态（原文 §26 只给 string，按 ORCH §13 固定）：`CREATED / PLANNING / ACTIVE / COMPLETED / FAILED / CANCELLED`，`stop_reason ∈ {verification_passed, budget_exhausted, max_attempts_reached, planning_failed, cancelled}`；Task/Attempt 严格用 §25 | ORCH §13 |
| D16 | CLI `python -m agent_orchestrator`：`mission create/get/cancel/events`、`attempt get`、`artifact show`、`demo --scenario single-task --provider fixtures|<配置名> --evidence-dir <dir>`；provider 配置：`fixtures`（内置确定性脚本）或环境 `SH_BASEURL/SH_APIKEY/SH_MODEL`（+ 可选价格表 `SH_PRICE_INPUT_MICROS/SH_PRICE_OUTPUT_MICROS`）；未实现 scenario 报 `not_implemented` 退出码 3 | ORCH §14.3 |
| D17 | 证据目录：`baseline.json / events.jsonl / final_state.json / artifacts/ / verification.json / costs.json / test-report.json`；密钥不写入 | ORCH §14.3 |
| D18 | Fixture provider：脚本化 BaseAgent 响应（Planner 一条、Worker 写文件+跑测试+Envelope、Critic 一条），第一次故意提交有错代码以触发 S2-02 修复路径 | ORCH §14.2 |

## 4. 模块与任务（内部切片，对外仍是一个版本）

| 切片 | 任务 | 交付 | 决定性测试 |
|---|---|---|---|
| A 合同与存储 | A1 `contracts/`：Mission/Task/Attempt/ResultEnvelope/Claim/Event 数据类 + JSON 编解码 + 状态机常量与合法转换表；A2 `storage/`：schema、Store（事务、CAS 更新、事件追加）；A3 `orchestrator/commit_service.py`：`commit(proposal)` 幂等回执；A4 `api/missions.py`：create/get/cancel/events；A5 `governance/budgets.py`：账户、Reserve/Settle、导入 usage | `tests/orchestrator/step02/test_contracts.py`、`test_store_and_commit.py`、`test_budgets.py` |
| B 执行链 | B1 `scheduling/leases.py` + `scheduler.py`：intent 领取、租约、心跳检查；B2 `runtime/agent_worker.py`：BaseAgent 桥（create/submit/回执）、`runtime/tool_gateway.py`（工作区工具）、`runtime/role_templates.py`（planner/worker/critic 指令与输出契约）；B3 `planning/planner.py`；B4 `context/context_builder.py`（§10 的 1、2、6、8、9、10、11 项，本步无 Blackboard）；B5 `artifacts/*`；B6 collector：Envelope 解析与核对 | `test_dispatch_and_collect.py`、`test_tool_gateway.py`、`test_planner.py` |
| C 验证与闭环 | C1 `verification/*` 四层 + router；C2 `orchestrator/event_handler.py` + `state_machine.py`：ResultSubmitted→VERIFYING→PASS/FAIL→修复 Attempt/停止→Commit 交付；C3 `observability/logs.py,traces.py`；C4 `__main__.py` CLI + demo + 证据目录；C5 fixture provider | `test_verification.py`、`test_single_task_closure.py`（S2-01/02/06/07）、`test_cli_demo.py` |
| D 恢复与幂等矩阵 | D1 故障注入钩子（`Store.fault(point)`）：S2-03/04/05/08；D2 `Orchestrator.recover()` 全路径；D3 真实模型演示（DeepSeek/luna）；D4 wheel 打包含新包 + 干净 venv 验证；D5 独立 review + 处置 | `test_recovery_matrix.py`、`test_real_provider_single_task.py`（opt-in）、journal |

## 5. 与 SDK 的边界（不改动清单）

- 不改 `simple_harness.agents` 任何公共合同；不新增 SDK 表；不 import `simple_harness.execution.sqlite.uow` 私有方法——费用导入通过 `AgentRuntime.uow.read_provider_invocation` 等公共门面（若缺按 `usage_ref` 查的门面，则在 SDK 加**一个**最窄的只读方法并在本步验收，记入 journal）。
- `react_loop.py`、kernel 零改动。

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| 模型不按 Envelope 契约输出 | 角色模板给出精确 JSON 骨架与示例；解析失败=可见失败 Attempt+带错误反馈的修复；真实模型测试统计一次通过率 |
| `run_tests` 子进程逃逸/挂起 | cwd 固定工作区、路径校验、超时 kill 进程组、env 白名单；不承诺网络隔离（记 journal） |
| 两库不一致（intent 已 AGENT_CREATED 但 SDK 无 agent） | 重放 `runtime.create(creation_key)` 幂等；`open()` 不到则视为未创建重来（同 key） |
| 费用未知（UNKNOWN provider 调用） | 预留保持占用，Attempt 保持 SUBMITTED/阻塞，不写零、不释放 |

## 7. 独立 review 后的修订（2026-09-11，裁决表见 journal §1）

以下条目**覆盖**上文对应决定；实现以本节为准。

| # | 修订 |
|---|---|
| D5' | dispatch intent 冻结**完整 `AgentConfig` JSON + 完整输入 `Message` JSON**（字节级），dispatcher 重放时直接读出，禁止重建；`input_hash` 只用于核对 |
| D6' | 存活判定：`turn_snapshot.state ∈ {queued, running, result_pending}` 均为存活；`blocked=True` 且 blocker 为 provider/tool UNKNOWN = **存活但阻塞**：续租、预留不释放、不 LOST（S2-08）。LOST 仅当：turn 不存在 / `open(agent_id)` 失败 / 无 blocker 且 `provider_turn_ordinal_to` 在 `stall_seconds`（默认 3×lease_ttl）内无推进。业务租约（Attempt，60 s）与 SDK 执行租约（`lease_ttl_seconds` 30 s）分别保存；最终 Commit 同时核对 `attempt.agent_id/turn_id` 与 `task_version` |
| D7' | 无效 Envelope → Attempt `RETRY_WAIT`（`failure.reason=envelope_invalid`），不新增 Attempt FAILED；`ResultEnvelope.id` 由系统派生（`ids.result_id(attempt, turn, hash)`），模型自报的 `id`/`result_id` 记入 `client_result_id`；两者同时出现且不同 → 拒绝 |
| D10' | Attempt 实际费用权威 = SDK `uow.list_provider_invocations(RunId(agent_id))`（本步新增的**唯一** SDK 只读门面）逐条导入：tokens 来自 `usage_json.usage`，`cost_micros` 来自 `budget_charge.amount_micros`；`usage_refs` 只做交叉核对；`unpriced` 由 `policies.pricing_mode == "unpriced_local"` 判定；priced 模式下 `amount_micros is None` = 费用未知 → 预留保持占用。硬限额下沉：runtime `ConsumerRuntimePolicies(budget_policy=BudgetPolicy(hard_cap_micros=X, refuse_on_unknown=True))` 按 Run（= Attempt）生效，本步所有 Attempt 共用同一 X（= Task `max_cost_micros`）；`AgentConfig.limits` 按 Attempt 预算写 `max_model_calls_per_turn / max_tool_calls_per_turn / turn_deadline_seconds`。estimator 的 `pricing_key` 必须为 `"consumer"`；`ports.model` 必须等于 provider 回显 `response.model`（CLI 用 `SH_MODEL`；不一致 → 记 `model_echo_mismatch` 并 fail-fast） |
| D13' | 4 个工具的 JSON Schema 在 `AgentRuntimePorts.tool_schemas` 声明（无 `additionalProperties`）；工具语义写进角色指令；per-Agent 收窄靠 `AgentConfig.tool_names`（编排层断言 ⊆ `runtime.tool_names`）；工作区映射 `run_id → (attempt_id, view, mode)`：Worker→`workspaces/<attempt>/`(rw)，Critic→`workspaces/<attempt>-verify/`(ro)；`run_tests` 用 `sys.executable -m pytest -q -p no:cacheprovider`，`PYTHONDONTWRITEBYTECODE=1`，`start_new_session=True` + 超时 kill 进程组，cwd 固定，env 白名单 PATH/HOME/LANG |
| D12' | 验收副本 = Worker 不可触达的独立拷贝（验证进程可写 `__pycache__`），不是 chmod 只读 |
| D14' | `Orchestrator.recover()` 第一步 `await runtime.recover_pending_turns()`；崩溃点矩阵固定 6 个跨库钩子：`after_agent_created` / `after_submit` / `after_turn_committed` / `after_result_submitted` / `after_layer_pass` / `mid_commit` |
| D15' | Task 从 VERIFYING 停止或取消的路径固定为 `VERIFYING → ACTIVE → FAILED/CANCELLED`（两次 Commit，事件各自可见） |
| D19 | 版本模型：`mission.version`/`task.version` 每次 Commit +1；Attempt 冻结 `task_version`、`context_version`（= 输入包 hash）、`prompt_version`（角色模板常量）；`artifact.version` 按 (attempt, path) 递增；verification 记录 `verified_task_version` 与 `result_hash` |
| D20 | 事件词表：原文 §16.2 名称原样使用（MissionCreated、TaskCommitted、AttemptStarted、HeartbeatReceived、ResultSubmitted、VerificationPassed、VerificationFailed、TaskCompleted、BudgetReserved、BudgetReleased）；登记扩展：MissionPlanning、MissionActivated、MissionSuccessJudged、MissionCompleted、MissionFailed、MissionCancelled、AttemptCreated、AttemptClaimed、AgentCreated、InputSubmitted、ResultRejected、VerificationStarted、VerificationLayerRecorded、AttemptLost、TaskFailed |
| D21 | Mission 级成功判定独立于 Task PASS：Commit 交付前对 `Mission.success_criteria` 逐条判定（`pytest:<path>`/`file:<path>` 确定性；自由文本交 Critic 的 mission 级裁定），事件 `MissionSuccessJudged`；全部满足才 COMPLETED，否则 Task COMPLETED 而 Mission FAILED（`stop_reason=mission_criteria_unmet`），为 S3-06 留地基 |
| D22 | Planner/Critic 身份带序号：`<mission_id>:planner:<n>`、`<attempt_id>:critic:<n>`；Critic 预算在 Task 账户下 Reserve/Settle（`counts_attempt=False`），Planner 在 Mission 账户下 |
| D23 | 验证层按 §14.1 顺序（格式→规则→Critic→测试），任一必需层 FAIL 即短路 |
| D24 | 合规收尾：`src/agent_orchestrator/py.typed`、mypy `files`、REUSE annotations 三处；`source-manifest.tsv` 不加（净新增、无上游来源，写明）；SDK 版本 0.8.0 → **0.9.0**（新包 = 新特性；避免与已发布 0.8.0 wheel 撞名） |
| D25 | `api/missions.create` 校验成功条件非空、预算合法、工具集合法；`tenant_id` 来自 API 调用方参数，永不取自 Agent 载荷 |
| D26 | fixture provider 按角色分派（系统消息里的 `[role:<name>]` 标记）+ UNKNOWN 制造器（handoff 后抛非确定性异常 → `settle_unknown`）；`MODEL` 与 `ports.model` 一致 |
