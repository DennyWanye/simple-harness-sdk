# Harness audit V1 coverage inventory and required successors

2026-09-05. This first slice exports bounded **current canonical sources**. It is not
a claim that every historical operation or every SDK domain is fully audited.

| Boundary | Producer/source in this slice | Export/oracle | Remaining gap |
|---|---|---|---|
| Run admission/state/terminal | Existing run_admissions, runs, run_events | Current states + exact event refs/hashes; kernel completion/cancel | Other workflow/child terminal receipt interpretation is not a full inventory |
| Provider claim/handoff/settle | Existing provider_invocations CAS + same-transaction audit.transition.v1 event | Current head, transition/attempt, usage provenance, timestamps, safe code | Pre-claim policy/rebuild failures have no invocation row; no fake attempt |
| Provider recovery/retry | Existing reconciliation_resolutions, reauthorization/handoff CAS + transition event | Exact receipt, original unknown preserved, one permitted second handoff, kernel restart | All specialized legacy producer paths not globally certified |
| Tool dispatch preparation | EffectExecutor requested event before registry/schema/authorization | Unknown tool, malformed arguments and immediate deny remain visible with effect=None | ReAct barrier/TaskExecutionEnvelope validation before EffectExecutor is outside this writer; MUST add explicit boundary coverage |
| Tool authorization | requested/waiting/outcome event + existing decisions | Open/allowed/denied/cancelled distinct from physical effect; duplicate/reopen adjacent cases | No arbitrary authorization metadata/nonce export |
| Effect prepare/handoff/settle | Existing execution_effects CAS + transaction event | All outcomes, exact internal/raw call identities, hashes, safe grouping/timestamps | Standalone/custom-driver effects may have no Provider parent; None is explicit |
| Tool recovery | Existing reconciliation receipt/current effect + same-transaction completed/retry/refresh transition | Existing H13 adjacent tool recovery checks | Full runtime tool-unknown→reconciliation→rehandoff scenario not separately added here |
| ReAct call parent | Verify current ReAct v1 internal IDs + actual provider response ordinal/raw call/name/arguments hash | Cross-Run repeated raw ID still links distinct effects to actual invocation | Other driver lineage cannot be inferred; None |
| Context | Existing immutable workflow_checkpoints | Exact checkpoint refs/hash/version, no copied content | Not a semantic inventory of every recall/route selection; runtime/Memory decision domains retain ownership |
| Command/continuation | Existing conversation_commands and continuations | Stable IDs/state/version/hash, safe kind | No cross-Service principal binding or full control transition history certification |
| Diagnostics | Independent Noop/Recording/queue | Overflow changes no audit snapshot; no sink required | Diagnostics never claim canonical completeness |

`source_set` names exactly what the reader queries. `current_source_complete` means no
truncation/unavailability for that set in one read transaction. `history_coverage` stays
partial with explicit gaps; this slice never certifies global legacy history by seeing
one new event. `fully_audited` is not a field or implied result.

## Pending MUST (not waived by this delivery)

- Stable pagination backed by retained source boundaries/versions, with explicit
  invalidation/recovery semantics; raising limit is not a full-Run solution.
- Complete per-boundary producer inventory including pre-runtime/pre-claim barriers,
  context/recall decision selection, all workflow/child/control variants; reliable
  per-domain introduction markers and historical coverage classification.
- Host/Service owning-request checks, HostRun↔SDKRun binding, installed artifact and
  actual application composition; Memory/Service records are not covered by this API.
- Versioned audit rules/findings receipts and resume/idempotency belong to the audit
  consumer. A snapshot is evidence input, not an optimization finding or full audit.

No original acceptance is reduced. No frozen main, package version or wheel changed.


Metadata correction: arbitrary source codes and unknown candidate labels are not
public labels. Fixed SDK vocabulary/actual registry provenance govern readable labels;
opaque refs preserve joins. Original 0eb1 metadata candidate BLOCKED; see CONTRACT and
RESULTS for corrected 74-case source verification and pending independent re-review.

Stable pagination MUST now implemented in source (PAGINATION.md), 6 decisive cases
plus prior74 pass; fixed review pending. Captures every row in the declared source
set in one snapshot subject to explicit resource-unavailable, then exhausts immutable
pages with original prefix across terminal mutation/reopen. This removes the bounded
reader's truncation limit for consumers adopting open/page. It does not close the
remaining producer enumeration or historical recording gaps above.

## User scope clarification and next execution

CORE-BOUNDARIES.md is the actual-call-chain worklist C1–C8 for the remaining required
production boundaries. New supported core Runs must get explicit recording-contract
coverage after all producers close; legacy-unverified is not permanent new-Run policy.
The current blanket historical gaps remain truthful only for this first slice and will
be replaced by validated per-Run/per-domain coverage. Successor artifact freeze waits
for source-leaf closure; do not build a new version for every small fix.

## Current source checkpoint — 2026-09-06 (supersedes pending statuses above)

| Source requirement | Fixed source / review | Boundary retained |
|---|---|---|
| Safe metadata + stable all-page prefix | 6a8b0e4, 698e13e/fd4a785, independently scoped accepted | Bounded capture can return typed unavailable; complete pages do not mean consumer findings complete |
| C1–C4 actual runtime producer + C8 continuity | 8a4b824 independently scoped accepted | Exact old072 middle activation remains unverified; custom/replaced producer capabilities are not inferred |
| C5 command transitions/pre-Run domain | 93ce163 + b24c135 independently scoped accepted | Command namespace is separate; no fake Run for rejected/noRun commands |
| Delivery physical boundary/CAS/parent | eea3c19 + bd710e9 independently scoped accepted | Claim/expiry is not physical send/not-sent; missing response remains unknown |
| C6/C7 canonical child/workflow/control sources | 996e4ba independently scoped accepted for four added joins, earlier canonical readers retained | Current mutable heads are not an invented immutable all-transition archive; missing historical facts stay gaps |
| Committed-turn Memory port + terminal anchor | a6b0a7e independently scoped accepted | Memory receipt is not internal materialization proof; actual SDK terminal field changes installed Host comparison |
| noRun Context/release + actual Run consumption | 9756ff1 fixed, independent review pending | Explicit audit2; old unleased release call with neither witness nor state change cannot be reconstructed |

Remaining freeze gates: noRun fixed-source review and any resulting decisive fixes;
final declared producer/coverage inventory challenge; verify next unused successor
version and build one exact combined source artifact; independent installed public
consumer plus **real Host exact terminal evidence** comparison of new sdk_memory_outbox.
No weakened Host comparison and no old072 PASS substitution. Raw ba1/a6 probe indexes
are in RESULTS.md. Counts from successive subsets overlap and must not be summed.

Harness source visibility does not close Host/Memory/Service operation producers,
consumer audit findings/idempotency, native product adoption or historical erasure.
No all_operations_recorded field is introduced. The source DTO enumerates runtime
boundaries, auxiliary recording domains and explicit history limitations.

2026-09-06 update: noRun source9756 was blocked and corrected by018e135/d6c1951.
Dirac independently scoped ACCEPT d6c1951 with no remaining leaf P0/P1; its review
closes the noRun fixed-review gate above. All artifact/installed/Host exact terminal
comparison and declared legacy scope limits remain in force. See MEMORY-PORT.md.
