# Slice 1 执行 journal · 委派最短价值链

- 仓库：simple-harness-sdk，执行基线 main = `fd12e7dd`（0.7.10）
- 方法：plan-task（plan 由主编排者依据 BA-v1.0 拆片，见 `../program.md`）
- 上游：Host `plans/taskSys2/base-agent-phase1-plan.zh-CN.md`

## 0. 路径判定与执行模式（开场留痕）

- `TASK_TYPE = delivery`。
- `FLOW_TIER = FULL`：命中"新持久化状态 / 多阶段状态机 / 公共 API 导出面变化"。
- `MACHINE_GATE`：**不启用**，完成记录走一页 journal + DoD 清单（无机器 receipt）。理由：本片新增 `base_agent` 模式默认不被任何 Host 使用；已有 v9 库只要求"仍可打开"，**不做**就地迁移器（迁移器与回执归 S5，届时再评估机器账本）；不涉及权限/身份/支付/共享基础设施。本片所有声明仍须附实测证据（命令 + 输出）。
- 输入语义敏感 / LLM 载荷驱动：`agent.delegate` 由模型输出驱动 → acceptance 已含 LLM 行为变异清单 V1–V5，每条有端侧容错断言；真人测试广度门不适用（SDK 库，无 UI），真实 provider 端到端作为证据留证。
- 冷路径：`build_agent_runtime` 对空目录直建 v10 库即冷路径，由 T2/T9 覆盖。
- **执行模式：集中兵力（当前 session 串行）**。T2→T9 环环相扣（schema → 契约 → mode → 内核分流 → driver → 装配 → 工具 → 端到端），且主要矛盾链路需要连贯的内核上下文；做完一个任务验一个、提交一个。T10 的真实 provider 演示与快照更新独立，可在 T9 之后并行。
- 回归口径见 `../baseline.md`；新红一条即阻断。

## 1. 计划挑战记录

- 第 1 轮（primary breadth，独立 Opus 挑战者）：4 P0 / 12 P1 / 5 P2，全部裁决见 `challenge-round-1.md`；核心发现：BaseAgent 永不终态会切断依赖终态的 `child_terminal_receipts` 通道 → 改为直接读子 Agent 结果行；用 `driver_kind="base_agent"` 取代新 start_mode，消掉两条 P0；finalize 同事务 ack continuation。
- **修订完成（2026-09-10）**：`plan.md` / `acceptance.md` 已改为 v2（`plan-status: finalized-v2`），21 条裁决的逐条落点见 `plan.md` 附 B；任务重排为 10 个，**价值验证里程碑 = T6.5**（内核级 spike），完整价值链验收 = T9。`program.md` 的主要矛盾与 S1 段已同步。closure 复核在执行开始前做一次。

## 1.1 v2 修订中新发现、但不在本片处理的问题

1. **`build_runtime` 硬锁 root profile key**（`kernel.py:147/3473-3479`）：`root_profile_key` 固定为 `"agent.general"`，无法直接用 `RuntimeProfile("agent.base","base_agent")` 作为 root。落地口径改为：root 条目键仍为 `"agent.general"` 但 `driver_kind="base_agent"`；`"agent.base"` 作为**子 Agent 的 profile_key** 一并注册（子 Run 由 `ChildCoordinator.launch` 按 `launch_request` 建立，不查 `self._profiles`）。裁决 #16 要求的"未注册即拒绝"由 `kernel.py:3490-3493` 天然成立。
2. **裁决 #11 的"同事务"落地方式**：`claim_profile_launch_and_commit_child`（`uow.py:4585-4790`，200 行、自开事务）内部无法插入模式行而不重写该方法。改为**先围栏后建 Run**（围栏行 + 委派行 + 子 binding 同一事务，且早于子 Run 存在），与 `RunClient.start_conversation` 的 `reserve_legacy_run_mode`（`kernel.py:911-933`）先于 `_start_run`（`:949-950`）同范式。**保证不变**：子 Run 从存在的第一刻起即被围栏。
3. **裁决 #8 的改动面比裁决表写的宽**：`short_context_migration.py` 有 **4 处**（`:83/:114/:191/:194`）`fresh_descriptor()` 语义都是"v9 目标"，都要改成 `legacy_v9_descriptor()`，不止 `_validate` 一处。
4. **T4 需要两个公开入口而非一个**：除裁决 #5 点名的 `Runtime.signal_base_agent_input`，创建路径也需要 `Runtime.start_base_agent_run`（否则 `AgentRuntime.create` 只能调私有 `_start_run`）。两者成对，同属围栏任务。

## 2. 任务执行记录

| 任务 | commit | 验证命令 | 结果 | 备注 |
|---|---|---|---|---|
| T2 schema v10 + 冻结 v9 | `1ba3bba` | `pytest tests/execution/test_base_agent_schema_v10.py tests/execution/test_short_context_migration_still_targets_v9.py`；固定回归命令 | 8 passed；回归 60 failed / 15 errors 与基线逐条相同 | v9 库仍可打开；v7/v8→v9 升级器仍以 v9 为目标 |
| T3 契约骨架 | `d838a22` | `pytest tests/agents/test_config_contracts.py` | 21 passed | ast 扫描：kernel/agent_turn 不 import agents |
| T5 结果载体 + finalize | `308a67e` | `pytest tests/agents/test_turn_finalize.py`；固定回归命令 | 5 passed；回归红集与基线相同 | 同事务 ack 全有或全无（fault 钩子实测）；任何唤醒路径先 finalize 再 drive |
| T6 AgentExecutionDriver | `f1e149b` | `pytest tests/agents/test_agent_driver.py` + legacy ReAct 回归 3 文件 | 5 passed；legacy 28 passed | 预算超限 = 失败的 Turn，Agent 不死；意外 continuation 被 ack 不终态 |
| **T6.5 价值验证里程碑** | `66f875d` | `.venv/bin/python -m pytest -q tests/agents/test_base_agent_kernel_spike.py` | **3 passed** | 主要矛盾"不死的执行身份"一面已被真实 driver 证明 |
| T4 围栏与公开入口 | `9fc1335` | `pytest tests/agents/test_api_mode_fence.py` | 9 passed；回归红集与基线相同 | start_snapshot.py / start_mode.py sha256 冻结闸 |
| T7 build_agent_runtime / BaseAgent / AgentRuntime | `e149d52` | `pytest tests/agents/test_build_agent_runtime.py` | 8 passed；回归红集 ⊆ 基线（import purity 转绿） | queued Turn 跨重启由 kernel.recover 唤醒（E6） |
| T8 agent_delegate | `7a0e43c` | `pytest tests/agents/test_delegate_tool.py` | 15 passed；回归红集 ⊆ 基线 | 预围栏→ticket→launch→binding→输入→条件等待；续做不毒化；V1–V5 |
| T9 mock 端到端 | `b014a71` | `.venv/bin/python -m pytest -q tests/agents/test_delegation_e2e_mock.py` | 1 passed（provider 恰好 4 次） | 完整价值链验收 |
| T10 真实 provider + 快照 | `b014a71` | `--run-real-provider tests/agents/test_delegation_e2e_real_provider.py`；`examples/base_agent_delegation.py` | 见 §4.3 | 工具名 `agent_delegate`；assistant tool_calls 从 effect 账本恢复 |

## 3. 触碰既有红的说明

- T2 schema v10：实测 v10 后红集与基线逐条相同（既有 schema/迁移夹具本来就因缺旧环境或旧断言而红，失败方式未变），无需解释差异。
- T10：`tests/unit/contracts/test_public_api.py::test_public_api_matches_frozen_snapshot` 由红转绿——快照与四处硬编码版本本来就停在 0.7.8；本片按计划更新到当前 `__version__` 并追加 BaseAgent 导出，另冻结 `public-api-0.7.10.json` 做「只增不减」断言。
- T7：`tests/artifact/test_import_purity.py::test_import_has_no_host_or_runtime_side_effects` 由红转绿——根因是根包 `__all__[0]` 不是 `"__version__"`（四个 `MandatoryContext*` 条目被放在最前），本片把 `"__version__"` 移回首位并追加 BaseAgent 导出。这是**触碰既有红**，不是本片引入的行为变更；`baseline-known-failures.txt` 保持冻结，回归口径改为「红集 ⊆ 基线红集」。


## 4. 核心价值 smoke

### 4.1 里程碑 T6.5（内核级 spike，2026-09-10）

命令：`.venv/bin/python -m pytest -q tests/agents/test_base_agent_kernel_spike.py` → `3 passed`。
- `test_two_results_on_one_run_never_terminal`：同一 Run 两条输入 → 两条结果行（seq 1、2），run_events 全量回放无 completed/failed/cancelled，第二次 provider 请求含第一轮 assistant 回答，provider 恰好调用 2 次。
- `test_input_continuation_is_acked_and_rescheduled`：两条 continuation 均 ACKED 且回执 id 为 `{run}:progress:{cid}:{epoch}`；第二条输入无需外部唤醒即被消费；`_reschedule` 被调用。
- `test_stage_then_kill_then_recover_commits_once`：在 stage 之后 finalize 之前注入崩溃，旧租约过期后新 Runtime 恢复：结果行恰好 1、hash 与 staged 相同、provider 仍只调用 1 次、Run 保持 WAITING。

**矛盾转化再分析**：主要矛盾的第一面（执行身份能反复交结果而不死）已解决；现在决定成败的问题转为第二面——**父 Agent 能否在不依赖终态的前提下可靠取回子 Agent 的结果并做幂等结算**（T8 的结果直读 + 续做语义），其次是公开入口与围栏（T4）让这条链能被应用代码而不是测试内部路径调用（T7）。剩余任务顺序不变：T4 → T7 → T8 → T9 → T10。

### 4.2 完整价值链（T9，mock 确定性）

命令：`.venv/bin/python -m pytest -q tests/agents/test_delegation_e2e_mock.py` → `1 passed`。
主 Agent 收到复杂任务 → `agent_delegate` 创建子 BaseAgent（独立 run，`parent_run_id` = 主 run）→ 子 Agent 一个 AgentTurn 得出带 NONCE 的结论 → 主 Agent 最终回答含 NONCE（NONCE 只写在子 Agent 的委派模板里，主 Agent 的 instructions/输入/工具 schema 里都没有）→ 运行时关闭、租约过期后新运行时 `open` 同一主 Agent → `get_result` 读回同一 result_hash 且 provider 计数不变 → 第二轮 `ask` 在同一执行身份上完成（seq 2）→ run_events 全程无终态事件 → provider 恰好 4 次调用、脚本用尽 → `sys.modules` 无 `simple_harness_memory`。

### 4.3 真实 provider 复现（T10，证据，不作为 MUST 判定）

- 端点：Host `.env` 的 svtun `gpt-5.6-luna` 今日直连探针 **90 s 超时**（`ProviderTimeoutError`），改用记录在 `.local-test-evidence/2026-09-07/credentials/deepseek.env` 的 DeepSeek 端点（`deepseek-v4-pro`，直连探针 1.3 s）。密钥只经环境变量注入，日志/报告中已核对不出现。
- 第 1 次（修 wire 前）：主 Agent 第二次请求被端点拒绝（`provider_request_rejected`）——根因是持久 Context 里的 assistant 消息不带 `tool_calls`，紧随的 `tool` 消息让 OpenAI 兼容端点 400（Host 侧同一问题曾用进程内 memo 绕过，其 docstring 明说"上游应修"）。本片在 SDK 侧修：`agents/wire.py` 从 effect 账本（`raw_call_id/tool_name/arguments_json`）在**请求副本**上恢复 `metadata.provider_tool_calls`，`OpenAICompatibleProvider._message_payload` 序列化为 `tool_calls`；持久 Context 不变；决定性测试 `tests/agents/test_provider_wire.py` 3 例。另：DeepSeek 拒绝带 `.` 的函数名，工具标识改为 `agent_delegate`（文档里仍称 agent.delegate 能力）。
- 修 wire 后 4 次 pytest：**2 次失败、2 次通过**（审计核对后如实修正）。失败的 run2/run3：`provider_protocol_error` / 账本 `provider_response_not_durable`——`dispatch.py:596` 的 `provider_response_json(...)` 对主 Agent 最后一次综合调用的响应抛 ValueError（既有 SDK 逻辑，非本片代码），Turn 记失败、Agent 存活（符合设计）；子 Agent 已 settled、NONCE 在子结果里。通过的 run4（12.79 s）与 run5（10.21 s，review 修复后）：`{"main_state":"committed","delegation_count":1,"delegation_state":"settled","child_run_state":"waiting","nonce_in_child_result":true,"nonce_in_final_answer":true,"provider_calls":3}`。失败率 2/4 → F-BA-1 提为**下一片优先**。
- 演示脚本 `examples/base_agent_delegation.py "请比较三种排序算法并推荐一种。"`：exit 0，`state=committed delegations=1 nonce_relayed=True`，最终回答含子 Agent 的验证码，输出中无 key。
- 报告文件已落仓：`reports/real-provider-run{,2,3,4,5}.txt`、`reports/demo-run2.txt`（key 已脱敏，见 `reports/README.md`），与 mock 报告分开，不互相冒充（BA40）。

## 5. 兑现表（phase-4 ③3；被测对象是 SDK 库，测试方式一律为可复跑 pytest 脚本，无 UI）

| AC | 矛盾地位 | 含 UI | 测试方式 | 驾驶者 | 证据 | 状态 |
|---|---|---|---|---|---|---|
| AC1 委派创建子 Agent | 决定性 | 否 | 脚本 | AI | `tests/agents/test_delegate_tool.py::test_delegate_creates_child_agent_and_returns_child_result_row`；journal §4.2/§4.3（真实模型 3 次调用、子 run waiting、委派 settled） | ✅ |
| AC2 结果回到主 Agent | 决定性 | 否 | 脚本 + 真实模型 | AI | mock：最终回答含 NONCE（`test_delegation_e2e_mock.py`）；真实：`nonce_in_final_answer: true`（§4.3 第 3 次 + 演示脚本） | ✅ |
| AC3 单层/配额/幂等 | 决定性 | 否 | 脚本 | AI | `test_child_catalog_excludes_delegate` / `test_quota_rejects_second_delegation_in_same_turn` / `test_same_delegation_id_is_idempotent` / `test_one_delegation_is_exactly_one_child_turn` | ✅ |
| AC4 同 Agent 多轮 | 决定性 | 否 | 脚本 | AI | `test_base_agent_kernel_spike.py::test_two_results_on_one_run_never_terminal`（run_events 全量回放）+ 端到端第二轮 seq 2 | ✅ |
| AC5 RESULT_PENDING 后重启只提交一次 | 决定性 | 否 | 脚本 | AI | `test_turn_finalize.py::test_result_pending_then_kill_then_recover_commits_once`、`test_base_agent_kernel_spike.py::test_stage_then_kill_then_recover_commits_once`（真实 driver；provider 不重调） | ✅ |
| AC6 实例互相独立 | 次要 | 否 | 脚本 | AI | `test_build_agent_runtime.py::test_create_returns_independent_agents`、`::test_create_does_not_call_provider` | ✅ |
| AC7 旧 API 拒新模式（root+child） | 次要 | 否 | 脚本 | AI | `test_api_mode_fence.py` 9 例 + `test_child_run_is_mode_fenced_before_it_exists` | ✅ |
| AC8 不依赖用户记忆 SDK | 次要 | 否 | 脚本 | AI | `test_build_agent_runtime.py::test_no_memory_entrypoint_is_called`；端到端 `sys.modules` 断言；`grep -r simple_harness_memory src/simple_harness/agents` 无 import | ✅ |
| AC9 委派 UNKNOWN 结算 | 次要（可选） | 否 | 脚本 | AI | `test_delegate_tool.py::test_delegate_unknown_reconciles_from_child_result_row`（COMPLETED 带子结果回执 / STILL_UNKNOWN 带 pending evidence_ref；observe 不 launch） | ✅ |
| AC10 真实模型复现 | 次要（可选） | 否 | 脚本（opt-in，真实端点） | AI | §4.3 与 `reports/`：run4/run5 通过 + 演示脚本通过；run2/run3 既有 `provider_response_not_durable`（F-BA-1） | ✅（证据，非 MUST） |
| V1–V5 LLM 变异 | — | 否 | 脚本 | AI | `test_delegate_tool.py` 五条 + `test_agent_driver.py` 未知工具名/provider 拒绝 | ✅ |

无降级、无未批准的等价替代；全 AI 驾驶（库类被测对象，无真人点击面）。

## 6. 遗留清单（不许悬空）

| # | 事项 | 归属 |
|---|---|---|
| L1 | ReAct 最终 CAS 与 stage 之间的残留冻结窗口：崩在这一小段会重生成（acceptance 已把 AC5 收窄为"stage 之后"） | S5 · BA31（把 RESULT_PENDING 并进同一次 CAS） |
| L2 | 预围栏后、launch 前崩溃留下的 `run_context_use_requirements` 孤儿行不可删（续做复用同一 child_run_id，正常重试不新增） | S5 · BA37 一并处置 |
| L3 | lifetime `TerminationLimits` 是 runtime 级（driver 单一 policy fingerprint），`AgentConfig.limits` 的 per-turn 上限本片只记录、只由委派配额/等待生效；per-agent/per-turn 精确执法 | S2 · BA11 |
| L4 | UNKNOWN provider/tool 期间 AgentTurn 停在 running（`agent_turn_outcome` 与 `wait_blocker` 互斥是刻意的），恢复语义 | S2 · BA11 |
| L5 | 真实端点 `provider_response_not_durable`（`dispatch.py:596` 对某些响应抛 ValueError，修 wire 后 **2/4 次**），Turn 失败但 Agent 存活；需要抓一份原始响应定位 | **F-BA-1，下一片优先** |
| L6 | svtun `gpt-5.6-luna` 直连 90 s 超时（本片改用 DeepSeek 留证）；Host 说"Host-shaped 请求 200"，SDK 直连的差异未查 | 独立环境项 |
| L7 | 工具标识 `agent_delegate`（端点函数名不允许 `.`）；plan/acceptance 文本仍写 agent.delegate 能力名 | 文档口径，无需改代码 |
| L8 | `ToolContext.agent_delegate_context`（plan T8 提议的新字段）未加：委派工具改由 run_id 反查绑定，不需要新字段 | 已在 journal 说明，无 |
| L9 | assistant `tool_calls` 恢复依赖 `request_id` 前缀解析 run_id 与 effect 账本按 turn 分组；raw call id 跨轮重复时按轮次对位，找不到则 `{}`（计数 `wire.fallback_total`）；上游"first-class transcript field"仍是 SDK 后续工作 | S3/S5 |
| L11 | assistant `tool_calls` 恢复找不到账本行时降级 `{}`（`wire.fallback_total` 计数，未上报为事件）；REJECTED/未落 effect 的调用会命中 | S3 |
| L12 | uow 新 facade 6 个超 10 行（最大 `commit_agent_turn_result_and_idle` 约 100 行，因复用 uow 私有 helper）；下沉到 `sqlite/base_agent/turns.py` | S2 |
| L10 | 既有红 75 → 73：本片顺手修了 import purity 与 public-api 快照两条（均记为触碰既有红）；其余 73 条既有红原样 | 基线口径不变 |

> 以下三条在 v2 修订时已确定为"本片不做、如实记账"，执行期只需确认没有被无意扩大。

1. **残留冻结窗口（裁决 #9，归 S5 的 BA31）**：`react_loop.py:717-732` 的最终 CAS（phase 复位、清空 request/response 快照）与本片的 stage 事务之间存在一个未被同一次 CAS 覆盖的窗口；在该窗口内崩溃会重新调用模型。本片的 AC5 失败域**只从 stage 之后起算**，不写"假装通过"的测试。处置方案：S5 把 `RESULT_PENDING` 并进 react_loop 的同一次 CAS。
2. **v10 之后旧 v9 库的只读审计通道会拒绝**：`execution/sqlite/database.py:145` 的只读审计 reader 断言 `reader.schema_version != SCHEMA_VERSION` 即 `RunAuditUnavailable`。v9 库仍能 `Database.open`，但审计 reader 会拒。既有设计（审计只服务当前版本），v10 只是把界线从 9 挪到 10。归 S5 的 BA37。
3. **`execution/sqlite/migrations/execution_v5_to_v6.py:156` 用 `fresh_descriptor()` 盖戳**：v5→v6 迁移完成后把 `sdk_schema_migrations` 单行改写成"当前 fresh 版本"，v10 之后会盖成 `(10,"0010_fresh",…)`，而库里并无 v10 的表。这是**先于本片就存在**的错位（今天已经会盖成 9），本片不改也不扩大；`tests/execution/test_execution_v5_to_v6_catalog_migration.py` 只走 `accepted_descriptor_rows()` 校验，不会因此变红。归 S5 的 BA37。
4. **UNKNOWN 期间的 AgentTurn 停滞**：`agent_turn_outcome` 与 `wait_blocker` 被排他校验挡死（刻意：不确定的动作不得被冻结成已完成结果），因此 provider/工具 UNKNOWN 期间该 Turn 停在 `running` 直到 blocker 解除。完整语义归 S2 的 BA11。

## 7. 代码 review（phase-3 A4）

独立 Opus 评审者对 `git diff fd12e7dd..b014a71 -- src/` 做正确性 review：**P0 1 / P1 3 / P2 9**。处置（每条修复配决定性测试，全部落进 `tests/agents/` 回归套件）：

| id | 级别 | 结论 | 处置 | 决定性测试 |
|---|---|---|---|---|
| F1 子 Agent 可递归繁殖（capability_snapshot 只影响请求不做执行闸门） | P0 | 属实（评审者已实证孙 Agent） | `BaseAgentToolRegistry` 按 Run 的 start snapshot `capability_snapshot.tools` 做执行期闸门（`ExposureGuardedTool`，未暴露 → REJECTED `tool_not_exposed_for_agent`）；`agent_delegate` 另加 `parent.role != root` → REJECTED `agent_delegation_not_permitted` | `test_delegate_tool.py::test_child_cannot_delegate_and_unexposed_tool_is_rejected`（子调 delegate 不产生孙；未配该工具的 Agent 调用被拒） |
| F2 轮内 UNKNOWN/授权等待必死锁 | P1 | 属实 | `_drive` 新增 BaseAgent 分支：有 claim 时对 `wait_blocker`/`authorization_wait` 不 ack、提交阻塞器/决策后释放本执行者租约；`AgentRuntimeReconciliation` 在 `reconcile()` 时调 provider coordinator 的 `reconcile_incomplete`（consumer 默认端口原是 no-op） | `test_agent_driver.py::test_unknown_provider_outcome_suspends_the_turn_and_resumes_after_reconcile`（transport 失败 → 阻塞器 → reconcile CONFIRMED_NOT_STARTED → 同一 Turn 续跑并 ack，无重复输入） |
| F3 stage 后 finalize 非冲突异常会终态化 Agent | P1 | 属实 | `_drive` 把 `_commit_agent_turn` 单独 try：非冲突异常记日志、放弃 authority、Run 保持 WAITING，交 finalize-first 恢复 | `test_agent_driver.py::test_finalize_exception_keeps_agent_alive_and_recovers` |
| F4 首轮重跑重复写用户消息 | P1 | 属实 | driver 两个分支统一用 `{turn_id}:context:user` 追加用户消息；instructions 用 `{run_id}:context:instructions` | `test_agent_driver.py::test_first_turn_rerun_does_not_duplicate_the_user_message` |
| F5 claim 为 None 时静默跳过 ack | P2 | 属实、后果严重 | finalize-first 与 `_finalize_pending_agent_turn`：输入未 ACKED 又拿不到 claim → 抛冲突，不静默 finalize | 由 F3 用例覆盖（恢复后 continuation 必须 ACKED） |
| F6 V1「未知 delegation_id 不创建子」口径 | P2 | 口径问题 | 修正验收口径：本片 `delegation_id` 是调用方幂等键，"未知 id"指归属别的 Agent/轮的 id；全新 id 就是新委派 | 已有 `test_unknown_delegation_id_is_rejected_not_created` |
| F7 配额判定与写入之间无原子性 | P2 | 属实（当前无 await 故安全） | 配额判定并进 `reserve_delegation` 事务（`DelegationQuotaExceeded`） | 既有配额用例 |
| F8 `reserved→reserved` 重放被拒 | P2 | 属实 | 允许同态重放 | 续做用例 |
| F9 `_settle_failed_provider_turn` 无覆盖 | P2 | 属实 | 已有 `test_provider_rejection_is_a_failed_turn_and_next_turn_recovers` 覆盖"拒绝后下一轮能开新请求"（该路径即由它修复） | 同左 |
| F10 子 Agent 失败原文透给父模型 | P2 | 属实 | 失败结果只带 `error_code` + 异常类名，不带原文 | — |
| F11 可能写空 `tool_calls` 数组 | P2 | 属实 | 过滤后为空则不写 | `test_provider_wire.py` |
| F12 wire 回填失配降级 `{}` | P2 | 设计取舍 | 保留降级 + `fallback_total` 计数；删死代码 `_mapping` | — |
| F13 per-turn 限制只记录不执行 | P2 | 属实 | 记遗留 L3（S2 BA11） | — |

复验：便宜层全量（ruff / mypy 0 issues / `tests/agents` 83 passed）+ 固定回归命令红集 ⊆ 基线（新红 0）+ 核心价值 smoke（mock 端到端 + 真实 DeepSeek 端到端 10.2 s `committed`、NONCE 回传）。

## 8. 完成度审计（phase-3 B）

独立 Opus 审计者（`MODE: code-audit`，在 12bb60b 上自行复跑）：**VERDICT: PASS**，必须 AC 8/8，可选 AC9/AC10 有证据，综合完成度约 96%，无 P0/P1 开放项，无未闭环的 plan 层缺陷。7 条 P2 缺口全部在收尾提交处置：V1 口径与「结构+执行期闸门」改写进 acceptance；uow facade 行数放宽并记 L12；真实 provider 报告落仓 `reports/`；§4.3 失败率修正为 2/4；`test_turn_finalize.py` 永真 `or` 断言改为精确断言；DoD 10 措辞与 AC2 测试名对齐；testcase README AC9 行更新；无关工作树改动还原。

## 9. DoD 清单（phase-final）

| 项 | 证据 | 状态 |
|---|---|---|
| 主要矛盾对应的决定性 AC 实测达成 | §4.1 里程碑 3 passed；§4.2 mock 端到端 1 passed；§4.3 真实模型 committed + nonce 回传 | ✅ |
| 整体可用性实测通过（原始需求核心路径） | 用户目标"主 Agent 收到复杂任务→创建子 Agent 完成"：`examples/base_agent_delegation.py` 真实模型 exit 0 | ✅ |
| 全部"必须" AC 有测试证据 | §5 兑现表 AC1–AC8 全 ✅，无降级 | ✅ |
| plan 层回炉闭环 | 无 A2 事件（`a2-events.md` 不存在）；两轮挑战裁决均在 plan v2.1 落实 | ✅ |
| 工作树干净且已提交 | 终态行所记被测 HEAD 之后仅含文档的收尾提交；`git status --porcelain` 空 | ✅ |
| 干净态复验 | 审计者在 12bb60b 上独立复跑：tests/agents 77 passed、schema+快照 14 passed、固定回归 58 failed/15 errors/1931 passed（新红 0）；被测 HEAD 上主编排者再跑 tests/agents 与固定回归（见终态行前一行） | ✅ |
| 分级冒烟 | 库类被测对象：核心价值 smoke = §4.1/§4.2；真实端点 smoke = §4.3 | ✅ |
| 无回归 | 固定回归命令红集 ⊆ 基线（73 ⊂ 75），新红 0；mypy 0 issues | ✅ |
| 幂等性审查 | submit/create/finalize/delegation 的幂等分支各有测试（`test_finalize_is_idempotent_by_receipt_id`、`test_same_delegation_id_is_idempotent`、`test_reserved_delegation_is_resumed_not_poisoned`、`test_submit_reschedules_the_run` 的 input_id 回放）；"遍历 + 写副作用"点：`recover` 的 open-turn 唤醒（幂等：唤醒只调度）、`AgentProviderWire`（只改请求副本） | ✅ |
| 可追溯矩阵无断点 | 审计 VERDICT PASS（§8） | ✅ |
| testcase 存盘、index 同步、脚本纳入回归套件 | `testcase/base-agent-slice-1/README.md`、`testcase/index.md`；脚本全部在 `tests/` 下随 pytest 运行 | ✅ |
| journal 终态行 | 见末行 | 待收尾 |
| code review 执行且 P0/P1 闭环 | §7：P0 1 / P1 3 全部修复并各配决定性测试 | ✅ |
| retro.md | `slice-1/retro.md` | ✅ |

## 10. 终态

被测 HEAD `1d2a17c` 上主编排者复验：`tests/agents` + schema 测试 83 passed / 1 skipped；固定回归命令 `58 failed, 1931 passed, 3 skipped, 14 warnings, 15 errors in 46.37s`，红集 ⊆ 基线（新红 0，既有红 73/75）；mypy 0 issues；真实 DeepSeek 端到端 run5 passed（10.21 s）。完成判定依据本 journal 与 §9 DoD 清单（无机器 receipt）。

VERDICT: SHIPPED — Slice 1 委派最短价值链（AC1–AC8 MUST 全部实测达成；AC9/AC10 有证据；known gaps 见 §6 L1–L12，F-BA-1 下一片优先）— 2026-09-10 — 1d2a17c

