<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# ARCHITECTURE 目录

记录 Simple Harness SDK 的架构生产事实。当前 source candidate 版本权威为 `0.7.0`。Human Memory S1
已经把自动 pre-Provider recall 改为显式的同 Run route seam：每个新的 Provider turn 只能消费 Host 经
`RunContextAuthorityPort` 返回并由 SDK 校验、冻结的 Context snapshot；同批 route-required effect 在
route receipt 尚未可见时会在 ledger/handoff 前拒绝。fresh execution schema v7 持久绑定
`TaskExecutionEnvelope`，ReAct checkpoint schema v5 跨轮保留 snapshot revision 与 ID→payload hash，
Provider durable response 只接受 public allowlist，隐藏推理和私有 metadata 不进入 ledger、checkpoint 或
Context。旧 `AgentMemoryPort.record_committed_turn` terminal outbox 仍保留；生产 kernel 不再自动调用
`recall_for_turn`/`release_recall`。Memory SDK 的新 evidence/认知状态和 Host TaskScope 产品实现不属于本仓
当前能力，仍由后续 release unit 完成。

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

Human Memory Program 当前只完成 Harness SDK 的 S1 source candidate 边界；不得把后续 Memory SDK、Host
TaskScope、动态 Context、单主对话 UI 或数字孪生体目标误当成已有产品能力。

<!-- last-updated: 2026-08-30 -->
