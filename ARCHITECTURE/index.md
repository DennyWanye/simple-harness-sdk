<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# ARCHITECTURE 目录

记录 Simple Harness SDK 的架构生产事实。当前 source candidate 版本权威为 `0.6.1`；覆盖官方 Agent Memory v1、可信四元 identity、
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

2026-08-25 Tool/Capability：SDK 0.6.1 source 已提供三类 capability record、bounded search/describe、
typed activation receipt、Run-local exposure port 与 ReAct ready-attempt 动态投影；Provider reserved 仍精确
重放原 request。fresh schema v6 分离 legacy Provider specs fingerprint 与完整 envelope digest，exact v5
只能显式 backup-first 迁移。目录可见性不拥有授权、确认、scope 或 effect authority。simple_harness Host
仍 pin 已发布 0.4.0 wheel，直到 exact 0.6.1 candidate 完成 Host/真实 UI prepublish gate；source、release、
consumer cutover 分别验收。

- [ARCHITECTURE.md](./ARCHITECTURE.md) — Agent Memory/Context contracts、identity binding、context
  staging、release retry、resource ownership、production builder、installed-wheel Linux ARM64 core gate，以及 Provider/预算、
  结构化消息、工具 catalog 与 projection outbox 权威边界。

<!-- last-updated: 2026-08-25 -->
