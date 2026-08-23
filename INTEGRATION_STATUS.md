<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# Simple Harness SDK 0.5.0 candidate integration status

## Release status

Harness SDK 0.5.0 adds the single durable command authority and fresh execution schema v5 while
retaining the Agent Memory v1, production composition, and observability v1 contracts. Memory
SDK 0.5 `MemoryManager` implements those retained contracts directly. Consumers provide
trusted `AgentIdentity`, Provider/Tool/Authorization ports, an optional product Context provider,
and `memory=MemoryManager(...)`; the SDK owns automatic recall, frozen Context, committed-turn
outbox delivery, retry, and recovery.

No `v0.5.0` tag or release exists yet. Publication is blocked on the Memory SDK 0.5.1
exact-wheel matrix against released Harness 0.4.0 and this Harness 0.5.0 candidate. The current
promoted release remains
[`v0.4.0` Release](https://github.com/DennyWanye/simple-harness-sdk/releases/tag/v0.4.0); the
public stable wheel URL returns the exact validated bytes from source commit `bc6ae8d`.

The earlier 0.5.0 candidate built from `a9502f2` is withdrawn and must not be published. It was
superseded after independent audit found output-convergence, stale-claim takeover, and legacy
preflight blockers. Only a later `candidate-manifest.json` that binds the fixes and passes the
Memory SDK 0.5.1 matrix is eligible for promotion.

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

- Harness SDK candidate: `0.5.0`, Python 3.11–3.13.
- Memory SDK integration: released Memory SDK 0.5 remains bound to Harness `>=0.4,<0.5`;
  Memory SDK 0.5.1 will widen this only after the two-wheel matrix passes.
- New 0.5 databases use fresh execution schema v5.
- Normal loading of execution schema v1-v4 fails closed. There is no v4-to-v5 migration;
  `migrate_execution_v3_to_v4` remains an offline maintenance API for the 0.4 line only.
- Retired public query/sink, reserved query/write ports, and consumer-managed Memory adapters are
  not compatibility surfaces in 0.5.0.
