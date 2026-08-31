<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# ARCHITECTURE 目录

记录 Simple Harness SDK 的架构生产事实。当前 source candidate 版本权威为 `0.7.0`。Human Memory S1
已经把自动 pre-Provider recall 改为显式的同 Run route seam：每个新的 Provider turn 只能消费 Host 经
`RunContextAuthorityPort` 返回并由 SDK 校验、冻结的 Context snapshot；同批 route-required effect 在
route receipt 尚未可见时会在 ledger/handoff 前拒绝。fresh execution schema v7 持久绑定
`TaskExecutionEnvelope`。`WorkspaceBindingAuthorityPort` 现定义独立的 Manual challenge/decision 与 Host
Run-mode snapshot 验证链；只有 Host durable lookup 后返回的 grant 才能进入 append transaction，随后
`WorkspaceBindingSetReceipt` 携带 sorted unique root identity hashes：genesis 固定 canonical empty-set
parent，后续只能验证为 exact parent set 加 grant 的一个新 root，并固定 base→new revision。schema v2
route receipt 和每个 project effect envelope 都交叉绑定该 binding-set receipt id/hash；v1 decoder 只
兼容无 authority standalone，project v1 fail-closed。generic Tool authorization receipt 或
`RunContextSnapshot.metadata` 不具备此 authority。ReAct checkpoint schema v5 跨轮保留 snapshot revision 与 ID→payload hash，
Provider durable response 只接受 public allowlist，隐藏推理和私有 metadata 不进入 ledger、checkpoint 或
Context。旧 `AgentMemoryPort.record_committed_turn` terminal outbox 仍保留；生产 kernel 不再自动调用
`recall_for_turn`/`release_recall`。Memory SDK 的新 evidence/认知状态和 Host TaskScope 产品实现不属于本仓
当前能力，仍由后续 release unit 完成。

S1 `a2-003` 现已冻结 schema-v2 cognitive wire：EvidenceSpan 由 admitted evidence authority 精确验证 UTF-8
byte range，typed observation 绑定 exact evidence/admission/item；四类长期记忆使用独立 payload/lifecycle、
revision target、canonical DAG 与 strict-atomic authority receipt。RecallPlan 必须绑定未过期 RecallContext，
保留 Host mandatory selector 并只允许缩窄；unknown/external/untrusted disclosure 默认不能产生 RECALL。
RecallDecision 已单独升级为 schema v3：RECALL 只携带 selected，NEEDS_USER_CONFIRMATION 只携带
通过资格与 disclosure gate 的 typed conflict candidates，每组至少两个唯一候选；outcome/reason 矩阵固定，
NO_RECALL/REJECTED 的 candidate count 恒为零，v2 与未引用的旧 v1 Decision decoder 均不可执行读取。
分类 enum 的唯一事实源是无依赖 `information_classification_protocol`；EvidenceItemAuthority 使用公开
`EVIDENCE_ITEM_AUTHORITY_SCHEMA_VERSION=3`
由 Host 强制附带 privacy floor、canonical attributes 和 classification authority ref。span verification
只接受 exact Host authority type，一次 resolve 后返回同一 verified item authority 供后续 join 复用。
typed observation 仅允许 Tool/Trusted Tool 或 External/External Source 两组 exact provenance 且必须解析
typed receipt；Mutation DTO 同时冻结 epistemic/evidence matrix，Memory repository 后续仍复验 authority。
conversation causal metadata 是 raw evidence 入库后的独立 Host registration，非法 metadata 不删除原始证据，
只失去后续 Short-Horizon 资格。

S1 `a2-006` 已新增 Host-owned `MemoryActionAuthority`：Mutation schema v3 的
REVISE/SUPERSEDE/SUPPRESS 只能引用 `MemoryActionAuthorityRef`，Memory 必须经 Host durable authority port
单次解析并校验 exact subject/action/existing target revision/evidence/run/turn/plan/operation/expiry/nonce/issuer/hash。
`operation_intent_hash` 明确排除 authority ref，避免 plan/authority hash 循环；Memory repository 仍必须在同一
mutation transaction 唯一消费 `replay_identity`。缺 authority 使用 typed
`MemoryMutationApplyResult.NEEDS_USER_CONFIRMATION`，不伪装成异常或 Recall outcome；CREATE 不需要 action
authority，CONTEST 不得携带 action ref，也不因此取得覆盖、删除或任意降级无关记忆的权限。

Main-model analysis 的 provider delivery 由独立的 `MemoryAnalysisResultEnvelope` 承载：其中 Host
durable `MemoryAnalysisDeliveryReceipt` 必须经 injected authority lookup 验证；Memory 后续产生的
`MemoryAnalysisReceipt` 仍只负责 validator/apply，两者不可互相替代。

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

<!-- last-updated: 2026-08-31 -->
