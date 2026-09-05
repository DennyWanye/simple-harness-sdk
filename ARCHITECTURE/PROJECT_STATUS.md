<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# Simple Harness SDK 项目状态

本页只记录当前 SDK source candidate 的生产事实和仍开放的跨仓门禁。完整协议边界见
[ARCHITECTURE.md](./ARCHITECTURE.md)，目录入口见 [index.md](./index.md)。


## Core audit producer source checkpoint — 2026-09-05

Real lease-bound core intervals, canonical child/workflow/control sources and
continuous recording verification are implemented in the isolated source tree.
Independent missing-whole-preflight findings now have first/later/same-epoch
corruption regressions; causal parent/child and canonical event bindings are checked.
196 directly affected tests plus two workflow relation tests pass; exact installed
old0.7.2 resume executes once and is correctly unverified by the new reader.
**C5 command source93ce163 independently scoped ACCEPT**: explicit audit
schema1, same-transaction version facts and stable pre-Run command pages.
Descriptor-column P2 now returns typed incompatibility before SELECT; schema8 PASS. Adjacent
236 PASS; exact installed072 legacy/middle-writer gaps remain unverified.
**Whole source leaf remains incomplete**: Delivery physical intervals and
independent complete producer review are still required. No version/schema/wheel
freeze or native claim. See [core progress](../plans/2026-09-05-run-operation-audit/CORE-PROGRESS.md).

## Stable Run audit page source successor — 2026-09-05

Metadata correction 6a8b0e4 independently scoped ACCEPT after the original canary
counterexample. Public open/page seams now materialize safe immutable audit snapshots
from one source read transaction, with bounded page reads, original source hashes,
opaque cursor/refs, version checks and restart continuity. Public async audit facades
offload to dedicated mode=ro connections; the runtime writer transaction remains separate. 85 focused/adjacent tests
pass; fixed pagination correction review pending. The6b5d inode-only dataset boundary
was independently BLOCKED; format2 verifies actual immutable Run/start/owner identity
and the captured append-only event cut on every page. The real kernel >256-record and waiting→terminal
prefix oracles pass. Disk/time limits yield unavailable, not partial completeness.
Source only: no frozen version/schema/wheel or installed consumer changes. Full producer
coverage/history gaps remain. See ../plans/2026-09-05-run-operation-audit/PAGINATION.md.

## 2026-09-05 isolated Run operation audit V1 source candidate

From2b842, an isolated follow-up adds public `read_run_operation_audit` (UoW/RunClient)
with safe names, source/call/attempt hashes and links, recorded timestamps/duration and
usage provenance. Pre-effect denial/wait/failure facts and source CAS transitions reuse
SDK-owned run_events; no schema or package-version change. **74 focused tests passed
(1.57s)** plus selected lint. The original 0eb1 metadata projection was independently
BLOCKED for arbitrary error-code leakage; the correction uses closed SDK codes,
registered-tool provenance and opaque join refs, with fixed-source re-review pending.
This is a bounded atomic first slice: current-source
completeness is separate from partial history coverage. Stable pagination and complete
producer enumeration are pending MUSTs; no full-Run/all-SDK audit or native completion.
Frozen main/wheel unchanged; independent fixed-source review and consumer integration
pending. See [contract](../plans/2026-09-05-run-operation-audit/CONTRACT.md),
[coverage](../plans/2026-09-05-run-operation-audit/COVERAGE.md) and
[results](../plans/2026-09-05-run-operation-audit/RESULTS.md).


## 2026-09-05 route 恢复限定修复

- 用户已批准 S5b 的必要 Harness 解冻例外；candidate 0.7.2 修复合法路由演进后授权恢复误拒绝。
- version-zero immutable anchor 与 current route 分别验证，既有 wire/导出不变；新内部 port 要求见架构。
- 针对性 checkpoint 13 passed，现有 runtime 148 passed，执行层/契约 991 passed / 2 fixture skips。
- 完整授权恢复新增 2 passed（文件 14 passed），独立审查无 P0/P1；
  exact wheel、Host 两 root 真实生产验收仍未完成，S5b 仍 BLOCKED。

## Human Memory Program

| 能力 | 当前状态 | 证据与边界 |
|---|---|---|
| S4 Host initial TaskScope route | Harness candidate 已完成 | route receipt v3、ordinary start snapshot v7、ReAct checkpoint v6 已冻结 Host authority、exact run/TaskScope/binding 并对恢复冲突 fail closed；Host execution composition 仍是跨仓门禁。 |
| S1 cognitive/evidence strict wire | Harness candidate 已完成 | schema-v2 cognitive wire、EvidenceItemAuthority v3、RecallDecision v4；Memory SDK 与 Host 产品能力仍需各自验收。 |
| S3 typed recall/use/assembly wire | Harness candidate 已完成 | v4 source-aware decision、Host-only Procedure applicability fingerprint authority、atomic confirmation-group page carrier、单一 budget identity、typed result/page、ContextFragment/assembly v2 与 use authorization/receipt 已冻结；Memory executor/provider adapter 仍是跨仓门禁。 |
| S3 Short-Horizon recall-text authority | Harness candidate 已完成 | Conversation evidence schema v3 将唯一 public_text pointer/hash 与 effective classification、item authority exact 绑定；无授权绑定只保留原始 evidence，不可索引。 |
| S1 Host action authority | Harness candidate 已完成 | `MemoryActionAuthority` schema v2 的 whole-plan commitment 继续由当前 mutation schema v5 承载；它防跨 operation 重放，COMMITTED result/receipt fail-closed 检查 protected refs，CONTEST 不能表达 destructive terminal lifecycle。 |
| S1 Semantic relation mutation wire | SDK 跨仓闭合 | mutation schema v5 冻结 claim/relation discriminator 与唯一 `applies_to`；relation endpoint 支持 existing/created exact ref，created ref 必须显式 dependency 指向 CREATE，且只允许 Semantic claim → Procedure/Prospective。Memory v7 持久化与 graph projection exact-wheel 已通过；Host durable diagnostic audit 仍是后续门禁。 |
| S3 Procedure/Prospective Host authority | Harness candidate 已完成 | 两套 schema v1 ref-only authority 已冻结：Procedure terminal/applicability 与 Prospective scheduler/event/ack signal 都必须 Host resolve，exact scope/revision/receipt/transition binding，半开有效期和 replay identity。Memory consumer 尚待跨仓实现。 |
| Memory repository action-authority consumer | 未完成，跨仓门禁 | Memory 必须单次 resolve 并复验 exact binding，在 mutation transaction 内原子唯一消费 replay identity；同 receipt 的幂等重放是唯一例外。 |
| TaskScope、动态 Context、数字孪生体 | 未完成 | 不属于当前 Harness SDK candidate 的已交付能力。 |

## 最近里程碑

- **2026-09-01 — Harness 0.7.1 Host-initial route candidate**：新增 origin-specific route
  provenance、ordinary start snapshot v7 与 ReAct checkpoint v6；fresh start、restart、tamper、legacy
  compatibility 和 existing-checkpoint conflict 均有聚焦测试。
- **2026-09-01 — Harness Semantic relation schema-v5 candidate**：Semantic claim 显式携带
  `semantic_kind=claim` 并保留 qualifiers；一等 relation payload 只开放 `applies_to`，冻结 exact endpoint、
  dependency DAG、类型矩阵、自环/relation endpoint 拒绝和 strict v5 fail-closed。新增 package-root validation
  diagnostic 仅给稳定 bounded reason，不回显 credential-bearing wire；跨仓 Memory transaction/graph 已通过
  exact-wheel public value 与 40-case integrity matrix，Host durable audit 尚未完成。focused conformance、全量
  1728 tests、ruff 与 mypy 均通过。
- **2026-08-31 — Procedure recall applicability authority**：`RecallContext` 现在绑定 Host 当前可用的
  canonical applicability fingerprint 集合并纳入 context hash；`RecallPlan` 没有对应可写字段，因此主模型只能
  继承这项 authority，Memory 可对 Procedure revision 做 exact current-applicability gate。
- **2026-08-31 — Harness typed Recall v4 candidate**：公开 recall wire 升级为 source-aware v4，
  Short-Horizon-only 与 mixed source 都可精确表达；confirmation 以完整有序组处理。typed result/page、
  use authorization/receipt、recalled ContextFragment v2 和 `(fragment_id, fragment_hash)` assembly 形成
  连续 hash/authority 链，预算上限与 canonical domain 严格验证。本里程碑不包含 Memory
  持久化、Host provider adapter 或真实召回质量验收。
- **2026-08-31 — Harness conversation recall-text authority candidate**：Conversation metadata/receipt/registration
  升级为 strict schema v3；Host 从 verified item authority 一次派生 pointer/hash/privacy/attributes/classification，
  registration exact 复算。未授权 item 保留原始 evidence 但不可进入 Short-Horizon index，旧 v2 wire fail closed。
- **2026-08-31 — Harness S3 lifecycle-authority candidate**：新增 Procedure observation 与 Prospective signal
  两套 Host-owned strict protocol；阻断调用方自证 terminal success、clock due 或 event occurrence，冻结 exact
  scope/revision/trigger/receipt/transition/Run-operation commitment、strict current wire、半开有效期和 replay identity。
  Memory repository 的原子 consumer、scheduler outbox 与状态机仍是下一跨仓门禁。
- **2026-08-31 — Harness S1 action-authority candidate**：新增 Host-owned exact
  action authority、无循环 whole-plan/operation-intent commitment、canonical operation index、ref-only mutation wire、严格旧 wire
  拒绝，以及 `COMMITTED` / `NEEDS_USER_CONFIRMATION` / `REJECTED` typed result。CONTEST
  不获得覆盖或删除权；Memory consumer 的原子 replay fence 与 CONTEST target-state exact-unchanged
  验证仍是下一跨仓验收门禁。

<!-- last-updated: 2026-09-05 -->

## Delivery audit source leaf — 2026-09-05

SDK-owned delivery now records original outbox versions and actual dispatcher
handoff/settlement facts in the authority transaction. Missing settlement remains
unknown; claim/expiry do not prove send or non-send. Immutable CAS receipts prevent
post-commit competing-owner substitution.95 adjacent PASS, real old072 middle
writer remains a history gap; fixedeea3c19 independently scoped ACCEPT.
Delivery-only public parent-ref normalization now joins captured heads (11 PASS). Whole producer
source and successor artifact are not complete. See
[Delivery contract](../plans/2026-09-05-run-operation-audit/DELIVERY.md).

## Canonical audit source associations — 2026-09-05

Terminal projection preparation, Provider projection receipt, wait blocker and
continuation-consumed context staging are included via canonical Run relations.
Five decisive source omissions red→green;136 adjacent PASS. Existing bytes/receipts
are reused, with no extra Provider accounting. SDK-owned committed-turn/release
port-call history and complete producer review still prevent all-operation/artifact
completion; Memory SDK internals remain outside this leaf. See core progress.

## Harness committed-turn port audit — 2026-09-05

SDK MemoryDispatcher records actual handoff/receipt with original owner/claim epoch;
logical APPLIED and receipt REJECTED_ERASED remain distinct. Same-transaction facts
survive legitimate outbox cleanup via final-hash tombstone. Transaction-local CAS
results prevent competing-owner substitution.31 directly related PASS plus6 leaf
PASS; exact072 middle call remains missing-epoch gap. This is source-only, fixed
review pending. No-Run Context prepare/release staging still requires the explicit
schema/association contract in
[Memory port contract](../plans/2026-09-05-run-operation-audit/MEMORY-PORT.md);
whole operation coverage and successor artifact are not yet complete.

## Committed-turn independent P1 correction — 2026-09-05

ba1d6ec was BLOCKED for caller-payload substitution and whole-family loss after
cleanup. New correction checks the actual immutable outbox row in begin/settlement
and derives a committed-turn hash in the canonical terminal receipt, independent
of audit-family retention. Consumed legacy cursor proof is reused; absence stays
unverified. Original terminal receipts are not restamped.35 direct/67 terminal
adjacent/1 cleanup replay PASS; fixed-source independent re-review required.
No schema2 WIP or artifact completion claim is included. See MEMORY-PORT contract.

## noRun audit schema2 source candidate — 2026-09-05

Implemented default stage-domain Context/release recording, explicit audit1→2,
actual consumed Run association and stable pagination. Direct schema/kernel/start/
page tests79 PASS4.90s; overlapping stage positive5 PASS0.28s. Real exact072 structural
claim/complete/cleanup remains recorded with missing-call/result gaps. No paid Provider,
native, full suite or wheel build. Independent fixed review pending; new terminal
payload's real Host installed exact comparison remains mandatory. This is not whole
Agent-operation completion or Memory-internal coverage. See MEMORY-PORT.md/RESULTS.md
under plans/2026-09-05-run-operation-audit; original counterexamples retained locally.

2026-09-06 fixed review follow-up: noRun9756 has two P1s; current successor adds actual
Run stage-cut revalidation and structural generation anchors. Late old release keeps
its real outcome without settling a recreated queue. Narrow45 PASS; overlapping11
PASS after exact generation-result proof. Independent revalidation still pending,
artifact freeze prohibited until closed. See MEMORY-PORT.md original red indexes.

2026-09-06 stage pagination cost correction: saved cut/binding validation now uses
only exact stage-event PK lookups and, for continuations, one actual continuation PK
owner lookup. Public SQL trace/query-plan oracle and old-binding rejection pass;
23 directly adjacent PASS overlaps the 2 indexed root/cut cases, plus 1 continuation
case. Fixed independent review pending; no new ledger/wheel or weakened Host compare.
