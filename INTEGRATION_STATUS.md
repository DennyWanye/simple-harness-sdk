<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# Simple Harness SDK 0.4.0 integration status

## Release status

Harness SDK 0.4.0 exposes the official Agent Memory v1 contract, production composition, and
observability V1 protocol. Memory SDK 0.5 `MemoryManager` implements those contracts directly. Consumers provide
trusted `AgentIdentity`, Provider/Tool/Authorization ports, an optional product Context provider,
and `memory=MemoryManager(...)`; the SDK owns automatic recall, frozen Context, committed-turn
outbox delivery, retry, and recovery.

Tag `v0.4.0` points to candidate source commit `bc6ae8d`; canonical `dist/BUILD_INFO.txt` and
`SHA256SUMS` identify the validated wheel `aaf8d79a…`. The source branch, `main`, and tag have
been pushed. The frozen wheel/sdist passed download-back checksum verification and are published at the
[`v0.4.0` Release](https://github.com/DennyWanye/simple-harness-sdk/releases/tag/v0.4.0); the
public stable wheel URL returns the exact validated bytes.

## Consumer matrix

| Consumer | Status | Evidence boundary |
| --- | --- | --- |
| `simple_harness` | integrated and real-UI validated | S6 installed exact wheels and passed automated plus Computer Use UI scenarios with DeepSeek |
| AIPhone | interface ready, not integrated | product-neutral SDK fixture only; repository and runtime unchanged |
| K6/AgentOS | interface ready, not integrated | product-neutral SDK fixture only; repository, PostgreSQL and runtime unchanged |
| NovelTagSystem | out of scope | no repository, database, runtime, or test changes |

“Interface ready” means the SDK contract carries deployment/household/actor/session identity,
personal/family scopes, official Context preparation, resource ownership, bounded failure policy,
and durable committed-turn delivery. It does not mean a product has adopted or tested the SDK.

## Compatibility and migration

- Harness SDK: `0.4.x`, Python 3.11–3.13.
- Memory SDK integration: Memory SDK 0.5 has a base dependency on Harness SDK `>=0.4,<0.5`.
- New execution databases use fresh schema v4.
- Normal loading of execution schema v1-v3 fails closed. Exact v3 databases require the explicit
  backup-first `migrate_execution_v3_to_v4` maintenance API.
- Retired public query/sink, reserved query/write ports, and consumer-managed Memory adapters are
  not compatibility surfaces in 0.4.0.
