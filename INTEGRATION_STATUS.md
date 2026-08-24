<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# Simple Harness SDK 0.5.2 integration status

## Release status

Harness SDK 0.5.2 retains the single durable command authority and fresh execution schema v5 while
retaining the Agent Memory v1, production composition, and observability v1 contracts. Memory
SDK 0.5 `MemoryManager` implements those retained contracts directly. Consumers provide
trusted `AgentIdentity`, Provider/Tool/Authorization ports, an optional product Context provider,
and `memory=MemoryManager(...)`; the SDK owns automatic recall, frozen Context, committed-turn
outbox delivery, retry, and recovery.

Version 0.5.2 is the release candidate for the nested durable start-input thaw fix. Promotion
requires the frozen exact-wheel gates and public redownload verification; this status will be
replaced with the immutable tag, source commit, and asset digests after publication.

The earlier 0.5.0 candidates built from `a9502f2` and `7fd6610` (wheel digest prefix
`7d70b9fa2f59`) are withdrawn and must not be published. They were superseded after independent
audit found output-convergence, stale-claim takeover, legacy preflight, and per-command output
ownership blockers. The promoted `candidate-manifest.json` has SHA-256
`9cc8363c33ecfef2a0c446d17ca89a02e0b58fe1b6fe6e4ae77a0ac2b706d59f` and binds all fixes; the
withdrawn candidates remain ineligible for publication.

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

- Harness SDK release candidate: `0.5.2`, Python 3.11–3.13.
- Memory SDK integration: released Memory SDK 0.5 remains bound to Harness `>=0.4,<0.5`;
  the Memory SDK 0.5.1 candidate passed its exact-wheel Harness 0.4/0.5 matrix before this
  release was promoted.
- New 0.5 databases use fresh execution schema v5.
- Normal loading of execution schema v1-v4 fails closed. There is no v4-to-v5 migration;
  `migrate_execution_v3_to_v4` remains an offline maintenance API for the 0.4 line only.
- Retired public query/sink, reserved query/write ports, and consumer-managed Memory adapters are
  not compatibility surfaces in 0.5.0.
