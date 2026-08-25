<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# ARCHITECTURE 目录

记录 Simple Harness SDK 的架构生产事实。当前 source candidate 版本权威为 `0.6.2`；覆盖官方 Agent Memory v1、可信四元 identity、
fresh execution schema v4、SDK 自动 Context/recall staging、durable recall release retry、统一
consumer/production composition，以及 Harness Observability S2 authority-transition instrumentation。
Memory 写入现为 terminal-only committed user+assistant Turn；旧
schema v1-v3 仍由正常 loader fail-closed；schema v3 只能通过显式、backup-first 的 offline migrator
升级为 v4，迁移会输出可校验的 neutral manifest，并持久化 legacy disposition/cursor。当前 candidate
wheel 公开 PEP 561 类型；root 与每个 continuation 独立绑定持久 Context snapshot
reference，未显式提供时仅从当轮 current message 内容寻址生成。两层 public surface 只保留统一
`AgentMemoryPort`，旧 query/sink、manual preparation 与 adapter-facing Memory DTO 已退休；非 owner 会按
有界 request/lease horizon 等待，并在 owner lease 过期后 CAS takeover。product-neutral future-consumer fixture
覆盖官方 Memory 组合；`simple_harness` 已完成 exact-wheel cutover、自动化与真实 macOS UI 验收。
AIPhone、K6/AgentOS、NovelTagSystem 仍未修改或测试，只能声明接口就绪。

2026-08-25 Tool/Capability：SDK 0.6.2 source 已提供三类 capability record、bounded search/describe、
typed activation receipt、Run-local exposure port 与 ReAct ready-attempt 动态投影；Provider reserved 仍精确
重放原 request。fresh schema v6 分离 legacy Provider specs fingerprint 与完整 envelope digest，exact v5
只能显式 backup-first 迁移。目录可见性不拥有授权、确认、scope 或 effect authority。simple_harness Host
当前工作树将 0.6.2 的 morphology-safe discovery 与 privacy-safe handler diagnostics 固化为本地 candidate
wheel（SHA-256 `92f5be18381ee3e5b96f8d338439f86000453a30c170faedcdb1a813e3f44d31`）；这只是本地 candidate
consumption，不是 tag/release 或 production promotion。Host 已修复 SDK authority/legacy
ToolRegistry 的 split scope Store，并把物理 policy fingerprint 冻结进 RunStart，严格 stale 校验仍保留。
真实 macOS UI CAP-1～CAP-5 已覆盖 filesystem、browser、Skill、external-origin policy 与完全重启后的独立
根 Run；重启 Run 从 13 个基线工具重新激活到 16 个，并在 stale nonce 被拒绝后重新 describe/activate 自愈。
Host 随后从 exact wheel 同步，packaged macOS app 在无 `PYTHONPATH` 条件下再次完成 13→14 与真实 README
读取。正式发布稳定线仍保持原版本；干净不可变 source identity、artifact、release 与 consumer promotion
继续分别验收。

- [ARCHITECTURE.md](./ARCHITECTURE.md) — Agent Memory/Context contracts、identity binding、context
  staging、release retry、resource ownership、production builder、installed-wheel Linux ARM64 core gate，以及 Provider/预算、
  结构化消息、工具 catalog 与 projection outbox 权威边界。

<!-- last-updated: 2026-08-25 -->
