<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# Simple Harness SDK 0.3.0 integration status

## Release status

Harness SDK 0.3.0 exposes the official Agent Memory v1 contract and production composition
path. Memory SDK 0.4 `MemoryManager` implements that contract directly. Consumers provide
trusted `AgentIdentity`, Provider/Tool/Authorization ports, an optional product Context provider,
and `memory=MemoryManager(...)`; the SDK owns automatic recall, frozen Context, committed-turn
outbox delivery, retry, and recovery.

The exact candidate passed the `simple_harness` product cutover and real macOS UI E2E. The tag
`v0.3.0` points to candidate source commit `fbb156f`; canonical `dist/BUILD_INFO.txt` and
`SHA256SUMS` identify the validated wheel `cf629cee…`. The source branch, `main`, and tag have
been pushed. The frozen wheel/sdist were uploaded to a draft GitHub Release and passed
download-back checksum verification. They are now published at the
[`v0.3.0` Release](https://github.com/DennyWanye/simple-harness-sdk/releases/tag/v0.3.0), and the
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

- Harness SDK: `0.3.x`, Python 3.11–3.13.
- Memory SDK integration: install Memory SDK 0.4 with its `harness` extra to resolve Harness SDK
  `>=0.3,<0.4`.
- New execution databases use fresh schema v4.
- Normal loading of execution schema v1-v3 fails closed. Exact v3 databases require the explicit
  backup-first `migrate_execution_v3_to_v4` maintenance API.
- Retired public query/sink, reserved query/write ports, and consumer-managed Memory adapters are
  not compatibility surfaces in 0.3.0.
