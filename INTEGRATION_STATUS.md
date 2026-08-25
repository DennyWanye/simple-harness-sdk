<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# Simple Harness SDK 0.6.2 candidate integration status

## Release status

Harness SDK 0.6.2 provides a provider-neutral Runtime capability catalog, same-Run progressive Tool
exposure, and fresh execution schema v6 while retaining the Agent Memory v1, production
composition, and observability v1 contracts. Memory
SDK 0.5 `MemoryManager` implements those retained contracts directly. Consumers provide
trusted `AgentIdentity`, Provider/Tool/Authorization ports, an optional product Context provider,
and `memory=MemoryManager(...)`; the SDK owns automatic recall, frozen Context, committed-turn
outbox delivery, retry, and recovery.

The 0.6.2 source and exact wheel are candidates. The simple_harness working tree currently vendors
the candidate built from base source `322d77...` plus reviewed patch SHA-256
`472ae6188b2e07aadadb4ea86906f357a0a28b3fccf11126169ee5d9dc31a9cb`, with wheel SHA-256
`92f5be18381ee3e5b96f8d338439f86000453a30c170faedcdb1a813e3f44d31`; Host automation passed.
The Host now composes SDK authority and the physical ToolRegistry with one capability scope store and
freezes the physical policy fingerprint into RunStart. A real macOS UI CAP-1 Run completed same-Run
13→14→15 exposure, real filesystem search/read, and a correct README-based answer. CAP-2 through CAP-4 then passed
the localhost browser, frozen Skill and external-origin safety gates. CAP-5 passed across a complete app/backend restart,
including stale-nonce rejection and recovery. Finally, the Host installed the exact 0.6.2 wheel and a packaged macOS app
started without `PYTHONPATH`, reported `sdk_version=0.6.2`, repeated 13→14 exposure and completed a real README read.
This local candidate consumption is not a tag, release, or production promotion. The current published fallback remains
v0.5.2; no release/tag claim is made until the source is represented by a clean immutable release commit, the exact
artifact is rebuilt/verified from that identity, and publication is explicitly authorized.

The one-time 0.6.0 prepublish artifact failed Host composition because the public ReAct builder had
no Run-local exposure resolver seam. It was not vendored, tagged, uploaded, or released and must not
be promoted. The corrected candidate uses 0.6.2 rather than overwriting the immutable 0.6.1 artifact.

The earlier 0.5.0 candidates built from `a9502f2` and `7fd6610` (wheel digest prefix
`7d70b9fa2f59`) are withdrawn and must not be published. They were superseded after independent
audit found output-convergence, stale-claim takeover, legacy preflight, and per-command output
ownership blockers. The promoted `candidate-manifest.json` has SHA-256
`9cc8363c33ecfef2a0c446d17ca89a02e0b58fe1b6fe6e4ae77a0ac2b706d59f` and binds all fixes; the
withdrawn candidates remain ineligible for publication.

## Consumer matrix

| Consumer | Status | Evidence boundary |
| --- | --- | --- |
| `simple_harness` | 0.6.2 candidate vendored locally; publication not authorized | Host automation, real UI CAP-1 through CAP-5 and exact-wheel packaged revalidation passed; clean immutable release provenance remains required before publication |
| AIPhone | interface ready, not integrated | product-neutral SDK fixture only; repository and runtime unchanged |
| K6/AgentOS | interface ready, not integrated | product-neutral SDK fixture only; repository, PostgreSQL and runtime unchanged |
| NovelTagSystem | out of scope | no repository, database, runtime, or test changes |

“Interface ready” means the SDK contract carries deployment/household/actor/session identity,
personal/family scopes, official Context preparation, resource ownership, bounded failure policy,
and durable committed-turn delivery. It does not mean a product has adopted or tested the SDK.

## Compatibility and migration

- Harness SDK candidate: `0.6.2`, Python 3.11–3.13.
- Host candidate integration also vendors Memory SDK 0.5.2 candidate SHA-256
  `deff2fa85a269a3978f2c6efcd99fda77abcb74444170361365fd00ec0164e9e`; it is not promoted by
  this blocked Harness gate. Earlier published compatibility lines remain historical release facts.
- New 0.6 databases use fresh execution schema v6.
- Normal loading of execution schema v1-v5 fails closed. Exact v5 has an explicit backup-first
  offline v5-to-v6 migrator; there is no implicit migration. There remains no v4-to-v5 migration;
  `migrate_execution_v3_to_v4` remains an offline maintenance API for the 0.4 line only.
- Retired public query/sink, reserved query/write ports, and consumer-managed Memory adapters are
  not compatibility surfaces in 0.5.0.
