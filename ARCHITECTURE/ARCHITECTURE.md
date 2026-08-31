<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
last-updated: 2026-08-31
-->
<!-- last-calibrated: 716fb8513095c4ad1dc005cb0fefe991e584c156 -->

# Simple Harness SDK — 架构基线（Human Memory S1 candidate）

> 本文件记录当前生产边界；0.1.4 的缺陷段落仅保留为历史对照，不代表当前实现。

## Human Memory Program S1 当前边界（2026-08-31）

- `0.7.0` source candidate 公开严格、版本化的 Memory、TaskScope、Evidence、Disclosure DTO。Working Memory
  是 Context role，不属于四类长期存储 enum；长期类型精确为 Episode、Semantic、Procedure、Prospective。
- 主模型只提出结构化 Recall/Mutation/TaskScope 操作。SDK 校验 canonical JSON、hash、schema、run/evidence
  绑定；SDK 不把 LLM 输出当成事实、权限或数据库状态 authority。
- schema-v2 `EvidenceSpanRef` 不携带整段 canonical text，而是绑定 admitted envelope/receipt/item、唯一注册的
  identity UTF-8 normalization、byte offsets、exact quote/hash、actor provenance 与 support kind；Memory 只能经
  authority port 解析并复算。typed observation receipt 进一步绑定 exact evidence、admission 和 item，不能跨
  evidence 重放。分类 enum 统一来自无依赖 `information_classification_protocol`；Host-only
  `EvidenceItemAuthority` 使用公开 `EVIDENCE_ITEM_AUTHORITY_SCHEMA_VERSION=3`，强制携带 privacy floor、canonical information attributes 与
  classification authority ref。verification 只接受 exact authority type 并返回同一次 resolve 的 verified
  item authority，不存在 schema v2 decoder 或缺省分类。
- typed observation 的可信 provenance 只有 Tool/Trusted Tool 与 External/External Source 两种，且两者都必须
  解析 exact typed receipt。Mutation strict DTO 冻结 EpistemicStatus/evidence matrix：VERIFIED_EXTERNAL 只能
  由 External/External Source typed observation + SOURCE_VERIFIED 支持，普通 user/model/context 不能授权；
  Memory repository 对实际 authority 的复验仍是提交前必要条件。
- Episode、Semantic、Procedure、Prospective 各自使用 exact-key typed payload 与专属 lifecycle；operation 独立
  携带 epistemic/conflict/verification/valid-time/privacy attributes。existing target 固定 expected revision，
  created-by target 只能引用同类型 CREATE。Mutation schema v3 进一步要求 REVISE/SUPERSEDE/SUPPRESS 只作用于
  exact `ExistingMemoryTarget`，并可携带的唯一授权 wire 是无 authority 的 `MemoryActionAuthorityRef`；完整 Host
  authority 绑定 subject/action/target revision、canonical evidence/span hashes、run/turn、plan/operation、稳定
  `operation_intent_hash`、有效期、nonce、issuer 和 authority hash。Memory 通过 Host port 单次解析后，仍须在
  mutation transaction 原子唯一消费 `replay_identity`；直接构造 DTO、模型 support kind 或 ref 均无权限。
  `operation_intent_hash` 排除 ref，因此用户确认后注入 ref 不形成 plan-hash 循环。缺授权返回 typed
  `MemoryMutationApplyResult.NEEDS_USER_CONFIRMATION`；invalid authority 返回 typed REJECTED，成功才携带 receipt。
  CREATE 不需要 action authority；CONTEST 禁止携带 action ref，且 Memory consumer 仍须用确定性同槽/不同值或
  既有冲突规则验证，不能因模型自报 CONTEST 降级无关记忆。全 plan 先 canonical topological 排序再 wire/hash，
  并强制 `strict_atomic` authority receipt 覆盖全部 operation 和唯一 base→committed revision，不存在
  partial-success wire；schema v2 Mutation plan/receipt 和旧 operation wire 明确拒绝，不提供 decoder/migration。
- `RecallContext` 冻结 Host 允许的 memory type、scope、entity、time、event、environment、task phase、retrieval
  mode、Short-Horizon、disclosure、evidence 和预算。`RecallPlan` 精确绑定 context hash/revision，在可信当前时间
  验证未过期，保留全部 mandatory selector 且只能选择非空子集/更窄时间和预算；Decision 再绑定 Plan/Context
  evidence lineage。RecallDecision 使用独立 schema v3：`RECALL` 只允许 selected memories，
  `NEEDS_USER_CONFIRMATION` 只允许 typed conflict candidates 且每组至少两个唯一引用；两者都执行 disclosure
  gate；outcome/reason 矩阵固定，NO_RECALL/REJECTED 的 candidate count 恒为零，v2 wire 和未引用的旧 v1
  Decision decoder 均不可执行读取。unknown、external、untrusted 或 unknown-purpose disclosure 默认不能披露记忆。
- conversation causal metadata 在 raw evidence admission 后由 Host 独立注册，绑定 primary conversation、causal
  group sequence/manifest、role/time/TaskScope/entities 和 Tool terminal receipt；Tool parent 必须在同组且早于
  当前 item。缺失或非法 metadata 不删除 raw evidence，只使其不具备 Short-Horizon 资格。
- `MemoryAnalysisExecutorPort` 返回 `MemoryAnalysisResultEnvelope`，其中
  `MemoryAnalysisDeliveryReceipt` 精确绑定 Host issuer、Run/job/request/result/attempt、nullable Provider
  response ID、Provider response hash 与始终存在的 Host durable receipt id/hash。公开 receipt 自洽不是
  provider delivery authority；consumer 必须通过 `MemoryAnalysisDeliveryAuthorityPort` 查验 Host durable
  exact record。它与 `MemoryAnalysisReceipt` 严格分层，后者只证明 Memory validator/apply 结果。
- `WorkspaceBindingProposal` 只携带 subject、TaskScope、typed canonical root/filesystem identity 与 base
  revision，不允许模型选择 mode。Manual challenge/decision 固定 nonce、channel、actor、authorization
  evidence、交互事件和有效期；Auto mode 只能来自 Host 签发且绑定 Run/Context/configuration revision/hash
  的 snapshot。所有新 hash 使用 DTO-specific domain separator；公开 DTO 的字段自洽不等于 authority，
  `WorkspaceBindingAuthorityPort` 必须查验 Host durable exact record 后返回 grant，并在 commit 前再次验证。
- `WorkspaceBindingSetReceipt` 携带 sorted unique root identity hashes，由集合重算 canonical digest；
  genesis parent 固定为 null/empty-set digest，后续 revision 必须精确等于 parent roots 加 grant 的单个
  新 root，并严格推进 `base_revision + 1`。它属于 Host append authority lineage；schema v2
  `ContextRouteReceipt` 与
  `TaskExecutionEnvelope` 分别携带 exact binding-set receipt id/hash，使当前 Run 继续冻结旧 revision，
  新 root 只能由后续可信 route 生效。generic `AuthorizationReceipt`、opaque ref 与
  `RunContextSnapshot.metadata` 均不能替代该链，Auto 也不替代高风险 Tool 的既有授权。
  `ContextRouteReceipt` v1 decoder 只兼容无 authority 的 standalone 历史 wire；project v1 fail-closed。
- `RunContextAuthorityPort` 是每个新 Provider turn 的唯一 Context authority。snapshot 在 Provider reservation
  前冻结，绑定 run、turn ordinal、prior revision、payload/request fingerprint，并跨轮检查 revision 单调性及
  snapshot ID→payload hash 不变性；崩溃恢复重放已冻结的同一 request，不再次请求 Host。
- Provider continuation capability 和其 fingerprint 随 Run 冻结。durable response decoder 使用 exact-key
  public allowlist；Context 只追加由同一 public projection 重建的 assistant message，raw hidden reasoning、
  私有 metadata 与 transport credentials 不持久化。
- 私有 tool catalog 为每个 capability 冻结 effect class、route requirement 和 TaskScope requirement，模型不可
  覆盖。整个 tool batch 在任何 Effect prepare 前预检；同批先 route、后 project effect 仍会以
  `ROUTE_BARRIER_NOT_OBSERVED` 拒绝且不产生 effect ledger/handoff。
- Host 注入的 `TaskExecutionEnvelope` 绑定 run/call/effect、route receipt、capability fingerprint、TaskScope、
  exact root identity、binding-set revision、binding-set receipt id/hash 和 idempotency。fresh execution schema v7 保存 envelope JSON/hash，
  malformed、wrong-run 或 stale authority 在物理 effect 前 fail-closed。
- production kernel 已移除每轮自动 `recall_for_turn`/`release_recall`；无召回必须写 exact
  `DIRECT_STANDALONE` decision，显式 recall 通过同一 Run 的 tool continuation 完成。terminal-only
  `record_committed_turn` durable outbox 暂时保留，供后续 Host↔Memory evidence ingestion 接线。
- SDK 仍保持 product-neutral：唯一主对话、TaskScope archive、本地目录/bindings、五天短时域、动态 Context
  assembler、前瞻调度和 UI 不在 S1 内实现。

## Tool / Capability 目录当前生产链（2026-08-25）

- `simple_harness.tools.RuntimeToolCatalog` 冻结 executable Tool、Skill resource、Workflow profile 三类
  产品中立记录，提供 bounded search/describe 与 nonce-bound typed activation receipt；
  `CatalogRunToolExposure` 只改变 Run-local visibility，不保存或调用 handler。
- `simple_harness.tools.ToolRegistry` 仍是每个 Runtime 的显式 Tool 注册和执行边界：它不扫描 import、环境
  或 Host 配置，负责唯一名称、参数 schema 校验、call-id 生命周期、取消与 handler dispatch。
- `ToolCatalogSnapshot` / `DurableToolCatalogResolver` 冻结并解析 Provider tool schema；新的
  `RuntimeToolCatalog` / `CatalogRunToolExposure` 则专门拥有 capability source、摘要搜索、describe、
  `direct/deferred/hidden` 暴露策略与 Run-local 动态激活。既有
  `workflows.capability_build.CapabilitySearchPort` / `CapabilityActivatePort` 和 durable-task
  `CapabilityCatalogPort` 仍分别服务能力包构建/激活和工作流可用性查询，不与 Runtime Tool 披露边界混用。
- `ReActRunInput.tool_exposure` 在每个新 `ready` reserve 前重新投影 direct+activated executable Tools；
  `provider_reserved` 只从 durable request snapshot 重放，restart 不会偷换 schema。
- SDK 继续保持产品中立：未来目录层只能接收消费者显式提供的 capability source、permission metadata
  和 execution handler；不得 import simple_harness Host、MCP manager、Skill loader 或特定 Provider。
- 目录/披露是可见性层，不是 authorization authority。搜索、describe、activate 不得执行工具，也不得
  绕过既有 ToolRegistry、authorization、effect/UoW、workspace scope、确认、幂等与恢复边界。
- `runtime/drivers/react.py::_tools()` 仍提供兼容静态输入；配置 `ReActRunInput.tool_exposure` 时，每个新的
  ready reserve 都从 Run-local exposure 重新投影。`provider_reserved` 已把完整 request schema 持久化，
  因而动态 projection 只能发生在新的 ready reserve 前，不能改写已 reserve request。
- ReAct checkpoint schema v3 持久 exact exposure state；terminal activation Effect replay通过 body-free
  typed receipt 确定性 reapply。fresh schema v6 单独存 Provider specs fingerprint 与完整 catalog envelope
  digest；handler locator resolution 对 missing/changed/extra identity fail-closed。exact v5 只允许关闭 Runtime
  后用显式 backup-first migrator 升级。
- Tool/Capability 子系统的已发布基线仍来自 0.6.4，未被 0.7.0 S1 改写；仓库整体 source candidate
  版本权威已是 0.7.0。源码就绪、release artifact 和 Host cutover 是三个不同状态，不能相互代替。

上述目录链路是 0.6.4 延续到 0.7.0 的实现事实；0.7.0 公开 release 尚未执行。

## SDK Observability S1/S2 当前事实（2026-08-23）

`v0.4.0` 已从 `bc6ae8d` 的干净 detached worktree 构建并发布；wheel SHA-256 为
`aaf8d79a71b75bde0d71157a635b841eb557ea8889e2824571cacd7d8a58ecb6`，下载回验通过。

- SDK 现在拥有 import-pure 的 `simple_harness.observability` 边界：immutable V1
  event/correlation wire contract、default-deny 有界 attributes、non-blocking `SafeEmitter`、
  Noop/recording/mobile ring/composite/JSONL/logging sinks、sink failure counters 与基础
  `diagnostics_snapshot()` schema。导入该边界不会初始化 runtime、execution、provider、Tool 或 SQLite；
  顶层 runtime exports 改为 lazy resolve，同时保持既有 public API。
- `ProductionRuntimeConfig` 可接收 Host sink，并拥有唯一 `ObservabilityRuntime`。事件进入固定容量队列，
  sink 工作不在业务 caller thread 执行；overflow、sink exception、reentrancy、emit-after-close 与 close
  timeout 只增加诊断计数，不改变 Run 结果。
- JSONL 采用有界轮转、regular-file/no-symlink 检查和 `0600` 权限；ring buffer 固定容量。
  Observability 不是 workflow、Context、Memory、authorization 或 retry authority。
- S2 将同一个 `ObservabilityRuntime` 注入 Runtime、Context staging、Memory outbox、Provider 与 Tool
  coordinator。Run start/terminal、Context `new→preparing→staged/degraded→consumed/abandoned`、outbox
  claim/apply/retry/dead-letter、Provider/Tool attempt outcome 均只在对应 authority 操作返回后发射；
  event 构造、allowlist 或 sink 失败只增加 dropped/error counter，不反馈业务结果。
- correlation 从既有 run/request/call/effect authority ID 单向派生为固定长度 opaque IDs；run 是跨异步、
  outbox 与 coordinator 的稳定 trace/root 锚点，request/call/effect 只扩展 parent/operation，不进入授权判断。
  事件 attributes 不接收正文、异常文本、provider/tool payload 或认证材料。
- startup recovery 对可恢复 durable Run 发射 `recovery.observed_state`，明确
  `replayed=true, history_complete=false`，随后成功恢复才发射 `recovery.resolved`；不会伪造 post-commit
  crash gap 中缺失的历史事件。
- `Runtime.diagnostics_snapshot()` 在 emitter 基础健康上增加 active Run 数，以及 Context/outbox/recovery
  的有界 status counts、oldest age 与最多 20 个稳定 error-code aggregates。查询只选择
  status/timestamp/error-code 列；关闭、query error 或 250ms deadline 返回稳定 degraded section，不抛入业务。

## Agent Memory v1 自动召回历史基线（0.6.x，已由 0.7.0 S1 取代）

本节保留 0.6.x 的迁移与恢复事实，便于审查 breaking change。凡涉及 pre-Provider
`recall_for_turn`、recall release 或 automatic Context preparation 的描述均不是 0.7.0 当前生产路径；
0.7.0 当前权威以本文件顶部 “Human Memory Program S1 当前边界” 为准。terminal committed-turn outbox
仍被保留，因此其 terminal/replay 段落继续适用。

- `AgentMemoryPort` 是唯一官方 Memory 边界：`recall_for_turn`、`release_recall`、
  `record_committed_turn`。旧 query/sink 与 reserved query/write ports 已从两层 public surface 退休，
  manual preparation helpers、adapter-facing DTO 与 `ContextPreparationMode` 也不再公开，仅作为 schema v3
  和内部回归兼容代码保留。
- `AgentIdentity(deployment_id, household_id, actor_id, session_id)` 是可信身份；每个 session 首次
  conversation entry 前写入 immutable binding，任何 rebind 都在第二次 recall 前 fail-closed。
  `MemoryScopeRef` 只允许 personal/family，automatic committed turn 只允许写可信 actor 的 personal scope。
- `ConversationTurnInput` 持有完整 identity、Message、canonical `memory_text`、recall scopes 与可选
  product `source_snapshot_ref`；`ConversationContinuationInput` 持有本轮 Message、`memory_text` 与独立的
  可选 `source_snapshot_ref`。continuation 不继承 root ref；未提供时只按本轮 current message 生成
  deterministic content-addressed ref。附件 body、reasoning 与 tool payload 不会隐式进入 Memory。
- `build_consumer_runtime` 与 `build_production_runtime` 统一接收一个 `memory=AgentMemoryPort`；
  `ResourceOwnership.BORROWED` 不关闭消费者资源，`RUNTIME` 在 build failure 或重复 Runtime close 时
  恰好关闭一次。同一 resolved path 同时作为 execution/Memory storage 会在组合时拒绝。
- `RunClient.start_conversation()` / `signal_conversation()` 是 Memory-enabled 的正式入口：SDK 自动调用
  product `ConversationContextProviderPort.prepare_once` 与一次 bounded recall，合并并冻结 Context stage，
  再进入 durable start/continuation。generic `start()` 在 Memory enabled 时拒绝绕过该入口。
- execution SQLite 只接受唯一、self-contained 的 `0004_fresh.sql`。新库包含 immutable
  `agent_identity_bindings`、扩展后的 `context_preparation_staging`、durable
  `memory_recall_releases`，以及 final `memory_outbox` committed-turn schema；旧 schema 稳定
  fail-closed 且不执行隐式/in-place migration。显式 `migrate_execution_v3_to_v4` 要求 Runtime 已关闭、
  exact v3 descriptor、同目录新 backup path 与完整 `LegacyIdentityMap`；它以新 v4 文件校验后原子替换，
  replace 后失败则从精确 backup 恢复。legacy `(user_id, session_id)` 可映射到重命名后的完整
  `AgentIdentity`，migrator 会同步重写 execution session/actor 主键、相关 FK、snapshot 与 Context staging。
- offline migrator 对每条 legacy Memory event 输出
  `simple-harness/execution-migration-manifest/v1`：`KEEP_COMPLETED_PAIR`、`SUPPRESS_TENTATIVE`、
  `SUPPRESS_TERMINAL`、`DEFERRED_TURN` 四类 disposition。completed continuation 的 assistant
  `continuation_id=NULL` 只允许由唯一 terminal event、最大 durable sequence、唯一 receipt 与 claim epoch
  交叉求解；零/多候选均 fail-closed。非终态最新 user 形成 versioned `legacy_turn_cursor`；迁移后每次
  continuation enqueue 在同事务 CAS supersede 前一 cursor，terminal 在 committed-turn transaction 中
  consume 当时 active cursor，failed/cancelled 仍为零 pair。source disposition 使用 `legacy-source`
  namespace，迁移后 turn input 使用 `turn-input` namespace，避免 identity collision。
- root start 与普通 user continuation enqueue 只提交执行/Context事实，不产生 tentative Memory intent。
  completed root/continuation terminal 才构造一份 canonical user+assistant `CommittedTurn`，并与 terminal
  state、delivery、parent wake 在同一 SQLite transaction 写入。failed/cancelled terminal 零 intent；
  非文本输入或输出若没有显式 `memory_text` 也跳过，不保存 attachment/tool/reasoning payload。
- terminal replay 同时验证 committed-turn presence 与 canonical payload/hash：same 可重放，different、
  missing 或事后 added 均稳定 conflict。turn identity、完整四元 identity、personal actor scope、recall
  write fence 与 SDK 冻结的 `turn_started_at` 一起进入 canonical envelope。
- context preparation 在 product provider 前先持久 claim identity/input hash、当轮有效
  `source_snapshot_ref` 与有界 lease。winner 调用 product provider 与 deterministic recall；replay 复用
  claim 中同一 ref 和 frozen stage，不二次调用两者；相同 continuation ID 改 ref 或 payload 稳定 conflict。
  非owner调用按Context request与lease horizon等待winner；正常慢provider不会被固定一秒误判为失败，owner
  崩溃后由waiter在lease到期时CAS takeover，并继续生成同一stage hash/release identity。product provider 只能提供
  persona/history/skills/tool hints 等非 Memory Context，伪造 `source=memory` 会在冻结前拒绝。
  private snapshot 把 recall 结果作为 USER/untrusted data，并在 start/continuation 原事务消费后清空
  private bytes、保留 lineage/hash；continuation 的 frozen prepared messages 连同本轮 message 一次性
  进入 ReAct durable context，Memory 数据不得提升为 SYSTEM。ReAct 恢复只读冻结 snapshot，不二次 recall。
- valid/corrupt recall result 的 release candidate 先写入 `memory_recall_releases` 再调用
  `release_recall`；失败不回滚 stage，由运行时与 startup pump 持久重试直至 released。
- recall timeout/transient/typed contract failure 统一冻结 `degraded_empty` stage；公开状态只保存稳定
  error code 与可用 write fence，不包含 exception 文本、路径或 payload。Memory 始终作为 USER/
  `untrusted_data` 注入，不能提升为 SYSTEM/developer authority。
- `RunClient.signal()` 的 generic continuation namespace 明确拒绝保留的 `conversation_user` kind；
  普通 user turn 只能通过 `signal_conversation()` 携带 typed DTO、durable context stage 与同事务 intent，
  产品 generic payload 不能伪造该 authority。
- committed-turn `MemoryDispatcher` 调用唯一 `AgentMemoryPort.record_committed_turn`，核对 receipt 的
  turn ID/hash；claim owner + epoch + expiry 防止 takeover 后旧 worker settle。transient 指数退避，
  permanent/conflict dead-letter；record 成功后 ack 前崩溃会以同 turn/hash 重放。
  `REJECTED_ERASED` 作为隐私安全的 applied no-op 收敛且日志只含 ID/hash/attempt/code。backlog 对所有
  state 提供计数，cleanup 只按 limit 删除 settled applied。Runtime 在 recovery/drain 后启动 pump，
  close 时 bounded drain；dispatcher 的单进程 run lock 保证 close drain 会等待已 claim 的慢速
  committed-turn 调用完成，不能由 pump cancellation 留下仅因关闭竞态产生的 claimed backlog。
  Agent Memory release pump 独立恢复；统一 Memory resource 按显式 ownership
  关闭且不会 double-close。关闭路径把
  `asyncio.wait` 输入物化，并只 cancel/gather 未完成任务，兼容 Python 3.11–3.13。
- child terminal 与 parent signal 虽在同一 durable terminal 事务提交，wake pump 仍须等待该 child
  离开 process-local active task 生命周期后才可 claim/ack 对应 parent signal；因此
  `wait_idle(child)` 的完成边界不会与 parent 自动恢复竞速，startup recovery 在 Python 3.11–3.13
  保持相同可观察顺序。
- `build_production_runtime(ProductionRuntimeConfig)` 是严格生产组合根：Provider、Tool、Auth、
  Delivery、reconcilers、统一 Agent Memory、Context provider 都必须显式组合；同时保留
  0.1.5 的 tool catalog、Provider budget resolver/projection pump、run binding 与 structured-message
  services。`ConsumerRuntimePorts`、policies 与 builder 现在也是顶层正式 public API。
- StartSnapshot v5 新增 conversation envelope、preparation mode、stage identity/hash 与 private
  snapshot；v1–v4 仍可读。Memory enabled 缺 envelope/stage/mode 时 kernel fail-closed；disabled
  generic run 不创建 Memory intent。ReAct 完成结果经 typed `conversation_output`，通用 payload
  只保留非敏感诊断。
- CI 与本地 reproducibility gate 都使用无环境覆写的 plain `uv build`；CI 单次构建 authoritative
  wheel/sdist 并生成 canonical `BUILD_INFO.txt`/`SHA256SUMS`，Python 3.11–3.13 测同一 wheel。
  release 仅 manual dispatch 下载、校验并上传 program publisher 已创建 release 的原 bytes，不响应
  tag、也不重新 build；artifact contract 静态拒绝 CI 重新引入 `SOURCE_DATE_EPOCH`。
- `simple_harness.testing.arm64_candidate:run_core_gate` 是 zero-argument synchronous public gate：
  只在 Linux ARM64 与两个非 editable、版本精确的 installed-wheel distributions 上运行。它用真实
  Harness fresh v4 UOW、Memory SDK 0.4 `MemoryManager` 与 dispatcher，先 terminal commit 完整 Turn，再注入
  Memory record 成功但 ack 前崩溃，关闭并重开两库后验证 outbox 以同 turn/hash 收敛，同时 read-back WAL、
  FK、integrity 与 0600。成功结果包含 `minimal_runtime`、`memory_outbox_restart`、`sqlite_reopen`
  三个 true 值及 Python/架构/distribution identity；任何失败以 stable code 抛错/CLI 非零退出。
- root、`simple_harness.runtime` 与 `simple_harness.testing.arm64_candidate` 的 `__all__` 均由同一 public
  API snapshot 固定；conversation DTO/ports、production builder 与 ARM64 gate entrypoint 的增删或重排
  都会触发契约测试失败。wheel 包含 PEP 561 `simple_harness/py.typed` marker；artifact gate 从隔离 venv
  安装实际 wheel，并以 strict mypy 导入 public Agent Memory 与 execution migration manifest 类型。
- 2026-08-22 S2-T1～T8 验证：terminal/outbox fault、root/continuation replay、apply-before-ack restart、
  `REJECTED_ERASED`、transient/permanent/conflict、claim takeover/stale epoch、bounded drain/cleanup 等
  dispatcher 场景，以及 root/多 continuation legacy classification、NULL FK 歧义 fail-closed、renamed
  target identity、连续 post-migration continuation、cursor/replace crash windows 均通过；Python
  3.11/3.12/3.13 full pytest 各 1366 passed / 2 expected skips，ruff 与 release-owned mypy 全绿。
  source provenance、REUSE、wheel/sdist/twine/canonical artifact 结果在本 slice 最终门禁记录。
- 2026-08-22 S5 Harness candidate half：version/public snapshot/metadata 已冻结为 0.3.0；
  product-neutral minimal/rich Context fixture 只使用 `ConsumerRuntimePorts(memory=...)` 与
  `ConsumerRuntimePolicies.local_default()`，覆盖四元 identity、personal/family scope、automatic recall、
  frozen replay/restart、committed turn、`REJECTED_ERASED` 与 `memory=None`。无 Memory 对话的 frozen
  message metadata 现接受只读 Mapping 并保持 JSON object。Python 3.11/3.12/3.13 final full pytest 各
  1373 passed / 2 expected skips；candidate artifact/future-consumer/ARM64 contract targeted 33 passed。
  exact-wheel 联测暴露的慢速 Memory inflight-close 竞态由 dispatcher run lock 收口，并由阻塞式
  committed-turn close 回归锁定。最终 Harness 0.3.0 / Memory 0.4.0 authoritative wheel 在 clean
  Python 3.11/3.12/3.13 中均由 site-packages 加载：strict mypy、四套通用 conformance、产品中立
  conversation fixture、自动 recall/committed pair/frozen restart、`memory=None` 与 apply-before-ack restart
  全绿；Memory standalone、真实 `[harness]` extra 解析及 Harness 0.2/0.4 拒绝 artifact matrix 为 10 passed。
  本地 ignored `joint-manifest.json` 固定两包 commit、wheel/sdist/BUILD_INFO/SHA256SUMS hash；不上传原始证据。
- 2026-08-22 A2-002 candidate correction：continuation Context ref 改为每轮独立 public input，claim 在
  provider 前持久有效 ref，未提供时只对当轮 current message 内容寻址；root + 两次 continuation、两类
  provider crash window、跨重启稳定 replay/conflict 与 installed-wheel public/type fixture 已纳入回归。
- 2026-08-22 产品验收：Harness `fbb156f` / 0.3.0 wheel `cf629cee…` 已由 simple_harness `4e797ccd`
  通过 exact installed-origin/hash 门消费。Harness full `1379 passed, 2 skipped`；产品 Gate r4 的
  21/21 required 场景达到 `READY_FOR_AUDIT`。真实 macOS UI 覆盖 root/新 Session recall、PPT/权限/Artifact、
  长历史、恶意 Memory、冷重启、recall timeout、record transient 未提交即退出后的 startup recovery，
  以及 stop/cancel projection。此结论只适用于 simple_harness；未来消费者仍是接口就绪状态。
- 2026-08-22 本地 promotion：canonical `dist/` 复用上述 exact wheel/sdist，`BUILD_INFO.txt` 与
  `SHA256SUMS` 指向 `fbb156f`；tag `v0.3.0` 指向同一 source commit。2026-08-23 source、`main` 与 tag
  已推送；本地冻结 wheel/sdist 已正式发布到 GitHub Release，并通过公开稳定 URL 下载回验。
- 2026-08-22 Agent Memory v1 S1 验证：Python 3.11/3.12/3.13 full pytest 各
  1334 passed / 2 expected skips；canonical identity/scope/hash、automatic recall、durable empty、
  atomic release-pending、replay、rebind、malicious product Context、ownership/build cleanup 与 legacy
  public-port retirement targeted 全绿。此前 consumer authority / recall release / finite bounds targeted
  74 passed；spawn-child recovery 全 8 参数在 Python 3.11 连续 20 轮
  （160 cases）通过；P0/P1 authority hardening targeted 76 passed；ARM64 public/artifact targeted
  9 passed。release-owned mypy 12 files 无错误；冻结 H3 范围 `ruff check src tests` 已由
  484-error baseline 清零，`mypy src/simple_harness/runtime src/simple_harness/execution` 也由
  31-error baseline 清零；全局规则未放宽，无法由 formatter 安全拆分的长 SQL / exact-wheel fixture
  使用局部 `E501` 标注；REUSE 341/341 compliant，source provenance PASS；
  临时目录 wheel/sdist build + twine check PASS。以 Memory
  candidate `87820fe2c4cdde21c3a9356ca461b93fe00aadcb` 完成本地非 ARM64 内部链路 smoke
  （不作为 A-ARM64 PASS）。

## 0.1.5 Context authority 当前事实（2026-08-21）

- `Message.content` 是 `str | tuple[ContentBlock, ...]`；canonical JSON、StartSnapshot、
  ReAct 恢复和 OpenAI serializer 保留结构，不允许通过 `str(list)` 降级。
- `ProviderBindingResolver.resolve(run_id)` 一次性绑定每个 Run 的 Provider、可选 frozen
  estimator 与预算策略；其 fingerprint 随 StartSnapshot 持久化并在恢复时校验。
- SQLite schema v2 持久化不可变、内容寻址的 tool catalog generation。Runtime 按
  generation + fingerprint 精确恢复；0.1.5 不执行 GC，因此 WAITING/restart 引用不会丢失。
- Provider terminal settlement 与 `provider_projection_outbox` receipt 在同一事务提交；
  reader 使用单调 `sequence` cursor，支持重启后幂等投影。
- `deskpet_public_progress` 是可选公开进度元数据；缺失、空白或类型错误时只剥离该字段，
  不阻断业务工具参数。8 MiB/block 与 16 MiB/Run 上限由产品 ingress 前置执行。

## 1. Consumer Adapter 层（`runtime/consumer_adapter.py`，当前边界）

外部消费者 → SDK kernel 的桥接层。核心入口 `build_consumer_runtime(ports: ConsumerRuntimePorts) -> Runtime`。

**0.3.0 历史链路（非当前 0.5.2 source 事实）**：

1. `Database.open(ports.database_path)` 打开 SQLite（以 `build_consumer_runtime` 符号为准）。
2. `SqliteExecutionUnitOfWork(database)` 建 uow。
3. `_ConsumerToolExecutorAdapter.build_registry()` 把消费者的 `ToolExecutorPort` 包成 SDK `ToolRegistry`。
4. `_ConsumerAuthorizationAdapter` / `_ConsumerProviderAdapter` 桥接 auth/provider。
5. 组装 `RuntimePorts`；缺省不注册 delivery sink，避免无声假投递。
6. `build_react_driver` + `build_runtime` 返回 Runtime。

**当前边界**：
- 工具 handler 仍以简化 context 调用 consumer executor，不能替代严格 production authority。
- `build_consumer_runtime` 是正式易用组合根：支持统一 Agent Memory、Context provider、ownership 与
  显式 local policies；定价默认仍为 unpriced local，严格计费/调和需求使用 production builder。

## 2. Delivery Dispatcher（`execution/delivery.py`）

- `DeliverySink.deliver(payload, *, idempotency_key)` — 消费者的投递实现。
- `DeliveryDispatcher.run_once()`：`claim_delivery` → 找 sink → `sink.deliver()`；**无异常 → `complete_delivery`**；异常 → `release_delivery`（可重投）。
- 空 `sinks` 合法；此时 `run_once()` 返回 `False`，delivery 保持 PENDING。只有空白 sink key
  会在构造时被拒绝。
- **缺陷根源**：只要 sink 存在且不抛异常，就标记 DELIVERED。no-op sink 满足"不抛异常"，于是假投递。

## 3. Driver Failure Terminalization（`runtime/kernel.py`）

- driver 边界异常在 `except Exception`（行 1438 起）：`logger.exception("sdk_run_driver_failed", extra={"run_id": ...})`（**行 1454-1455，已修复**，原为 bare `run_id=` 触发 `TypeError` 遮蔽原始异常并跳过 terminalization）。
- 随后 `read_run` → 若 state 非终态 → `_terminalize(state=FAILED, payload=failure.to_dict(), deliveries=())`，`private_cause` 进 `HarnessError` 但被 `to_dict()` 排除在对外 payload 外。

## 4. Database 生命周期（`execution/sqlite/database.py` + `kernel.py`）

- `Database.open()` / `close()` 显式拥有单连接；`SqliteExecutionUnitOfWork.close()` 转交关闭。
- generic `build_runtime` 只关闭调用方注册的 hook，不擅自拥有外部资源。
- `build_consumer_runtime` 注册 UoW close hook；两个组合根均按 `ResourceOwnership` 决定是否关闭统一
  Agent Memory，production 另外关闭 projection 等内部 owned resources。
- `Runtime.close()` 先 bounded drain delivery/Memory，再停后台任务并释放 leases/fences，最后执行
  组合根注册的 close hooks；重复 close 幂等，runtime-owned Agent Memory 恰好关闭一次。

## 5. CI / Release（当前）

- `ci.yml`：用与本地完全相同的 plain `uv build` 单次 build + canonical provenance，Python
  3.11/3.12/3.13 对同一 Actions artifact 跑 full pytest；release-owned paths 独立 ruff/mypy，
  另跑 source provenance。
- `release.yml`：仅 manual dispatch，按 candidate commit / Actions run / artifact / wheel SHA /
  version 校验冻结制品，再上传原 bytes；无 tag trigger、无 `uv build`、不创建第二套制品。
- `release-candidate-conformance.yml` 按 candidate commit 与 artifact SHA 在 macOS ARM64、
  Windows x64、Linux ARM64 上消费 exact Harness/Memory wheels，不拥有发布权限；Linux ARM64 额外执行
  `run_core_gate` 的 committed-turn apply-before-ack restart lane。

## 6. Agent Memory Port（`runtime/agent_memory.py`）

- 唯一 public protocol 是 `AgentMemoryPort` 的 recall/release/record 三方法。
- `AgentIdentity`、personal/family scope、canonical request/result/committed-turn hash、stable error/status
  与 ownership policy 由 `runtime/agent_memory.py` 定义。
- 旧 `ConversationMemoryQueryPort` / `ConversationMemorySinkPort`、`MemoryQueryPort` /
  `MemoryWritePort` 已退出 public export；旧逐消息 DTO 与 v3 DDL 仅作不可加载的历史兼容事实，
  production dispatcher 已只接受 committed turn。
