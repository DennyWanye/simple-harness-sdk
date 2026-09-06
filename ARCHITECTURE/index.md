## Mandatory context repair source — 2026-09-06

Last updated 2026-09-06. Separate successor source from H078: typed pending-action
refusal is handled after actual response checkpoint, with at most two durable
same-Run repairs and original budgets. Every repair-bearing terminal (including
routed) still checks real ACK/current pending; fresh typed grants/physical guard
remain. Existing context.no_recall/context.apply audits bind repair identity.
SDK11 + Host3 new controls passed in separate batches; Dirac fixed-source/results
limited ACCEPT. Main owns H079 packaging/installed/r17 with M618; no source tests
repeated, no old Run or frozen wheel changes. LastPG21416 exit0/remaining[].
[Results and exact boundaries](../plans/2026-09-06-mandatory-context-action/RESULTS.md).

## Native Host ordinary Run verified — 2026-09-06

H078/Hostb3680732 real native r13 completed a fresh ordinary turn and the default
audit consumer enumerated45 public DTO rows. Public SDK metadata reports
verified_current_intervals, coverage_gaps=[], history_coverage=recorded; no tool
or effect path was exercised. Old r12 unverified history is not recertified.
Host Run f4370cbe-1a87-537c-8d3b-8e0abbf8bd16; native PG99878 exited normally,
remaining[]. [Host evidence and exact scope](/Users/denny/projects/simple_harness-primary-candidate/plans/2026-09-06-typed-use-primary/NATIVE-R13.md).

## Native driver audit successor — 2026-09-06

SDK-owned immutable start-mode selection exposes the actual driver to kernel
recording. Four new actual SQLite Runtime controls passed; opaque/subclass and
custom Host-control drivers remain unverified. Source13abfe8, no schema change;
H078 one offline artifact and installed3 checks plus Host4 checks passed and received scoped independent ACCEPT; native remains pending. [Evidence and scope](../plans/2026-09-06-native-driver-audit/RESULTS.md).

<!-- Updated 2026-09-06 -->

## Authorization expiry terminal proof successor — 2026-09-06

H076 authorization expiry wrote a failed Run without a run.failed event. This
successor atomically binds root React tool-authorization terminal decisions and
provides explicit public eligibility/recovery plus exact public terminal metadata.
21 unique new source controls passed in bounded batches (not24). Actual r6
SQLite/WAL-consistent COPY passed public eligibility -> recovery -> terminal ->
reopen exact replay;33 original events retained and original DB/WAL bytes unchanged
at this gate. Original userdata has NOT been recovered. Fixed077 sourcec29af669/wheel60f7fb16
has one offline build, small-target public consumer PASS and Dirac scoped artifact
ACCEPT; Host/native remain separate gates. Unknown/multicycle/child recovery shapes refuse. H075/H076
artifacts unchanged. [Contract](../plans/2026-09-06-decision-terminal-recovery/CONTRACT.md),
[results](../plans/2026-09-06-decision-terminal-recovery/RESULTS.md),
[artifact](../plans/2026-09-06-decision-terminal-recovery/ARTIFACT.md).

<!-- Updated 2026-09-06 -->

## Exact short Context identity successor (source scope)

H074 requires a positive ContextFragmentV2 source_revision even though the public
short selected item correctly hasNone. Isolated H075 successor now requires only
None for SHORT_HORIZON, preserves strict positive nonshort and existing hash domain.
Explicit execution9 descriptor/backup migration isolates old binaries before durable
business reads. Wire8PASS and separate migration7PASS; actual Host11groups short
normal physical-guard allow and independent-source deny2PASS in source overlay.
Old artifacts/user data unchanged. Frozen075 sourceabbb0fd has identical double
offline wheel7969a2e5; small target installed actualshort two controls plus previous
Host factory refusal failure3PASS11.40s, no remaining process. Dirac scoped source
ACCEPT; new-artifact/installed review ACCEPT. Full Host typed-use/native acceptance remains open. [Contract and remaining bounds](../plans/2026-09-06-short-context-revision/CONTRACT.md),
[source evidence](../plans/2026-09-06-short-context-revision/RESULTS.md).

<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# ARCHITECTURE 目录

## Receipt-bound Provider reservation source — 2026-09-06

Isolated successor from frozen H073 `0282fa98`: schema2 typed Context intents,
original request/time checkpoint, actual Memory authority port and atomic
receipt/Provider claim association, existing handoff CAS and new-grant retry,
payload-free public view, explicit execution7→8 WAL-aware migration are implemented
with Dirac scoped source ACCEPT at69db778. New bounded source batch32PASS,
separate migration3PASS and adjacent33PASS; retained initial failures are documented.
Fixed0.7.4 source9229269 has identical double offline wheels (168 source files)
and6 target-installed public consumer tests passing; Dirac independent artifact read-only review
is scoped ACCEPT (exact168 package bytes/119 origins/manifest chain). No Host default/native/formal401 PASS. See the
[artifact handoff](../plans/2026-09-06-recall-use-reservation/ARTIFACT-HANDOFF.md).
Generic no-Memory behavior is retained;
missing/legacy typed carrier is not an empty attestation. See
[contract](../plans/2026-09-06-recall-use-reservation/CONTRACT.md) and
[results](../plans/2026-09-06-recall-use-reservation/RESULTS.md).


记录 Simple Harness SDK 的架构生产事实。本叶后继 source candidate 为 `0.7.4`（冻结0.7.3不变）。Human Memory S1
已经把自动 pre-Provider recall 改为显式的同 Run route seam：每个新的 Provider turn 只能消费 Host 经
`RunContextAuthorityPort` 返回并由 SDK 校验、冻结的 Context snapshot；同批 route-required effect 在
route receipt 尚未可见时会在 ledger/handoff 前拒绝。fresh execution schema v7 持久绑定
`TaskExecutionEnvelope`。`WorkspaceBindingAuthorityPort` 现定义独立的 Manual challenge/decision 与 Host
Run-mode snapshot 验证链；只有 Host durable lookup 后返回的 grant 才能进入 append transaction，随后
`WorkspaceBindingSetReceipt` 携带 sorted unique root identity hashes：genesis 固定 canonical empty-set
parent，后续只能验证为 exact parent set 加 grant 的一个新 root，并固定 base→new revision。schema v2/v3
route receipt 和每个 project effect envelope 都交叉绑定该 binding-set receipt id/hash；v1 decoder 只
兼容无 authority standalone，project v1 fail-closed。generic Tool authorization receipt 或
`RunContextSnapshot.metadata` 不具备此 authority。0.7.1 的 route receipt v3 区分 context-tool 与 Host-initial
provenance；ordinary start snapshot v7 将完整 Host initial route/hash 纳入 durable start identity，ReAct
checkpoint schema v6 只在 checkpoint 不存在时原子初始化，并在恢复时以 version-zero 初始锚拒绝启动 route/TaskScope/binding 冲突，保留合法演进的当前 route。
它继续跨轮保留 snapshot revision 与 ID→payload hash，
Provider durable response 只接受 public allowlist，隐藏推理和私有 metadata 不进入 ledger、checkpoint 或
Context。旧 `AgentMemoryPort.record_committed_turn` terminal outbox 仍保留；生产 kernel 不再自动调用
`recall_for_turn`/`release_recall`。Memory SDK 的新 evidence/认知状态和 Host TaskScope 产品实现不属于本仓
当前能力，仍由后续 release unit 完成。

S1 `a2-003` 现已冻结 schema-v2 cognitive wire：EvidenceSpan 由 admitted evidence authority 精确验证 UTF-8
byte range，typed observation 绑定 exact evidence/admission/item；四类长期记忆使用独立 payload/lifecycle、
revision target、canonical DAG 与 strict-atomic authority receipt。RecallPlan 必须绑定未过期 RecallContext，
保留 Host mandatory selector 并只允许缩窄；unknown/external/untrusted disclosure 默认不能产生 RECALL。
RecallContext 还把 Host 当前 Procedure applicability fingerprint 集合纳入 canonical hash，模型计划没有
对应可写字段，不能扩大或伪造当前适用性。RecallDecision 已单独升级为 strict schema v4：每个 selected item
明确区分 cognitive-memory
和 Short-Horizon source，前者绑定 memory type/exact revision，后者绑定 exact chunk ref 且禁止伪造
memory type。NEEDS_USER_CONFIRMATION 使用有序、完整的 atomic group/member，不接受部分冲突组。
typed result/page 与 ContextFragment v2 继续绑定 decision/result/item/use；Context assembly 按 fragment
`(id, hash)` 组装。公开 parser 只接受 v4，v3 与 naked source ref fail closed。
分类 enum 的唯一事实源是无依赖 `information_classification_protocol`；EvidenceItemAuthority 使用公开
`EVIDENCE_ITEM_AUTHORITY_SCHEMA_VERSION=3`
由 Host 强制附带 privacy floor、canonical attributes 和 classification authority ref。span verification
只接受 exact Host authority type，一次 resolve 后返回同一 verified item authority 供后续 join 复用。
typed observation 仅允许 Tool/Trusted Tool 或 External/External Source 两组 exact provenance 且必须解析
typed receipt；Mutation DTO 同时冻结 epistemic/evidence matrix，Memory repository 后续仍复验 authority。
conversation causal metadata 是 raw evidence 入库后的独立 Host registration，非法 metadata 不删除原始证据，
只失去后续 Short-Horizon 资格。该 registration 现使用独立
`CONVERSATION_EVIDENCE_SCHEMA_VERSION=3`：可召回 item 必须 all-or-none 绑定 Host 已验证
`EvidenceItemAuthority` 派生的 RFC 6901 `public_text` pointer、UTF-8 SHA-256、effective privacy、canonical
information attributes、classification authority ref 与 item-authority id/hash；没有该绑定的 evidence 仍永久保存，
但不得进入索引。v2 conversation metadata/receipt/registration fail closed。

S1 `a2-006` 已新增 Host-owned `MemoryActionAuthority`：该授权约束最初随 mutation schema v4 引入，
当前继续由 schema v5 承载；
REVISE/SUPERSEDE/SUPPRESS 只能引用 `MemoryActionAuthorityRef`，Memory 必须经 Host durable authority port
单次解析并校验 exact subject/action/existing target revision/evidence/run/turn/plan/operation/expiry/nonce/issuer/hash。
action schema v2 还绑定 authority-free whole-plan `plan_intent_hash` 与 canonical operation index；其他 operation
被插入或修改时旧授权必然失效。plan/operation intent hash 都明确排除 authority ref，避免 plan/authority hash
循环，而最终 `plan_hash` 仍承诺 ref；Memory repository 仍必须在同一
mutation transaction 唯一消费 `replay_identity`。缺 authority 使用 typed
`MemoryMutationApplyResult.NEEDS_USER_CONFIRMATION`，不伪装成异常或 Recall outcome；COMMITTED result 与可信
apply receipt 都会复验全部 protected existing operation 已携带 ref。CREATE 不需要 action authority；CONTEST 不得携带 action ref、必须是
CONTESTED、禁止 destructive terminal lifecycle，也不因此取得覆盖、删除或任意降级无关记忆的权限。Memory
consumer 仍必须把 CONTEST payload/lifecycle 与可信 target state 做 exact unchanged 比较，只允许 conflict flag 变化。

S3 Procedure/Prospective 的 Host authority seam 也已补齐，但还不是 Memory repository 实现。
`ProcedureObservationAuthority` 以 ref-only wire 绑定 exact subject/scope/memory revision、TaskScope、admitted
evidence span、terminal receipt/outcome、版本化 applicability fingerprint、risk/hazard、预期 lifecycle transition
及 Run/operation；`ProspectiveSignalAuthority` 绑定 exact typed trigger/hash、scheduler registration revision、
clock/event/ack receipt、outbox（仅 ack）、occurrence 和 lifecycle transition。两者完整 authority 只能由 Host
resolver 返回，校验窗口统一为 `issued_at <= now < expires_at`，并携带 nonce/replay identity；Memory 后续仍须
复验当前 head/scope/receipt，并把 replay fence、decision、CAS 和 outbox 放在同一事务。Procedure applicability
fingerprint v2 使用 exact fields + version 的 canonical domain hash，避免字段分隔符碰撞；Memory pure kernel
必须复用同一算法，避免 exact-wheel 漂移。

Main-model analysis 的 provider delivery 由独立的 `MemoryAnalysisResultEnvelope` 承载：其中 Host
durable `MemoryAnalysisDeliveryReceipt` 必须经 injected authority lookup 验证；Memory 后续产生的
`MemoryAnalysisReceipt` 仍只负责 validator/apply，两者不可互相替代。

2026-09-01 的 relation 增量把 mutation wire 升级为 strict schema v5：Semantic payload 显式区分
`claim | relation`，V1 relation 只允许 `applies_to`，same-plan endpoint 必须经显式 dependency 引用 CREATE，
并限制为 Semantic claim → Procedure/Prospective；普通 mutation target 的同类型规则不变。package-root 公开
validation diagnostic 只输出稳定 bounded reason，不回显不可信输入。跨仓 Memory v7 candidate 已通过原子关系持久化、
公开 committed receipt view 与数字孪生图投影的 exact-wheel 验收；Host durable pre-admission audit 仍未完成。

2026-08-25 Tool/Capability：SDK 0.6.2 起已提供三类 capability record、bounded search/describe、
typed activation receipt、Run-local exposure port 与 ReAct ready-attempt 动态投影；Provider reserved 仍精确
重放原 request。fresh schema v6 分离 legacy Provider specs fingerprint 与完整 envelope digest，exact v5
只能显式 backup-first 迁移。目录可见性不拥有授权、确认、scope 或 effect authority。simple_harness Host
当前工作树将 0.6.2 的 morphology-safe discovery 与 privacy-safe handler diagnostics 固化为本地 candidate
wheel（source `67f5769ca5501f17e37193477d87a149203b6887`，SHA-256
`ffb7c0619851f3c936fcc1d0cf527d07f49e87770291b85e57fe87032ac02c2e`）；这只是本地 candidate
consumption，不是 tag/release 或 production promotion。Host 已修复 SDK authority/legacy
ToolRegistry 的 split scope Store，并把物理 policy fingerprint 冻结进 RunStart，严格 stale 校验仍保留。
真实 macOS UI CAP-1～CAP-5 已覆盖 filesystem、browser、Skill、external-origin policy 与完全重启后的独立
根 Run；重启 Run 从 13 个基线工具重新激活到 16 个，并在 stale nonce 被拒绝后重新 describe/activate 自愈。
Host 随后从 exact wheel 同步，packaged macOS app 在无 `PYTHONPATH` 条件下再次完成 13→14 与真实 README
读取。source `67f5769…` 的最终 reproducible wheel 与完整真测 wheel 的 `simple_harness/` 运行时包逐文件
相同；Host 重锁、重装后又完成一次无 `PYTHONPATH` 冷启动与可操作 UI 冒烟。正式发布稳定线仍保持原版本；
tag、release 上传、download-back 与 consumer promotion 继续分别验收。

- [ARCHITECTURE.md](./ARCHITECTURE.md) — Agent Memory/Context contracts、identity binding、context
  staging、release retry、resource ownership、production builder、installed-wheel Linux ARM64 core gate，以及 Provider/预算、
  结构化消息、工具 catalog 与 projection outbox 权威边界。
- [PROJECT_STATUS.md](./PROJECT_STATUS.md) — 当前 SDK candidate 模块完成度、最近里程碑与跨仓开放门禁。

Human Memory Program 当前只完成 Harness SDK 的 S1 source candidate 边界；不得把后续 Memory SDK、Host
TaskScope、动态 Context、单主对话 UI 或数字孪生体目标误当成已有产品能力。

<!-- last-updated: 2026-09-05 -->
