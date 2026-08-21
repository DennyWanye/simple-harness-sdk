<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# ARCHITECTURE 目录

记录 Simple Harness SDK 的架构生产事实。当前覆盖官方 Agent Memory v1、可信四元 identity、
fresh execution schema v4、SDK 自动 Context/recall staging、durable recall release retry，以及统一
consumer/production composition。schema v3 逐消息 Memory dispatcher 仅作为内部兼容线保留。

- [ARCHITECTURE.md](./ARCHITECTURE.md) — Agent Memory/Context contracts、identity binding、context
  staging、release retry、resource ownership、production builder、installed-wheel Linux ARM64 core gate，以及 Provider/预算、
  结构化消息、工具 catalog 与 projection outbox 权威边界。

<!-- last-updated: 2026-08-22 -->
