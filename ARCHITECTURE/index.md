<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# ARCHITECTURE 目录

记录 Simple Harness SDK 的架构生产事实。当前覆盖官方 Agent Memory v1、可信四元 identity、
fresh execution schema v4、SDK 自动 Context/recall staging、durable recall release retry，以及统一
consumer/production composition。Memory 写入现为 terminal-only committed user+assistant Turn；旧
schema v1-v3 仍由正常 loader fail-closed；schema v3 只能通过显式、backup-first 的 offline migrator
升级为 v4，迁移会输出可校验的 neutral manifest，并持久化 legacy disposition/cursor。当前 candidate
version 为 0.3.0，wheel 公开 PEP 561 类型；root 与每个 continuation 独立绑定持久 Context snapshot
reference，未显式提供时仅从当轮 current message 内容寻址生成。product-neutral future-consumer fixture
覆盖官方 Memory 组合，真实产品接入仍只由后续 `simple_harness` cutover 验证。

- [ARCHITECTURE.md](./ARCHITECTURE.md) — Agent Memory/Context contracts、identity binding、context
  staging、release retry、resource ownership、production builder、installed-wheel Linux ARM64 core gate，以及 Provider/预算、
  结构化消息、工具 catalog 与 projection outbox 权威边界。

<!-- last-updated: 2026-08-22 -->
