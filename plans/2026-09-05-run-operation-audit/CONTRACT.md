# Harness public Run operation audit V1 — implementation contract

2026-09-05; base2b8428465cbd41032ba024a0b7199183161f5ecd.
Isolated feat/run-operation-audit; no changes to frozen main/package version/wheel.
Scope follows Host primary-candidate/plans/2026-09-05-agent-operation-audit/PLAN.md.
Implementation authorized; bounded V1 implemented and metadata P1 independently accepted.
Stable page successor is implemented in source; see PAGINATION.md. Harness scope only.

## Public surface

`RunOperationAuditPort.read_run_operation_audit(run_id: RunId, *, limit: int = 256)
 -> RunOperationAuditSnapshotV1`

V1 is one bounded, atomic SQLite read snapshot, NOT a live cursor or historical
multi-page promise. No per-page all-head hash scans. Limit validated/capped; query
LIMIT+1 per source and globally bound exported operations. Over-limit returns
`truncated=True`, never `complete=True`; caller can request a larger supported bound.
Unknown Run is typed unavailable. Other stores must explicitly report unsupported,
not build a snapshot from best-effort observability. Hash commits exact exported
facts+coverage and excludes read wall time. Close/reopen unchanged facts hash equally.

Strict metadata/ref DTO: SDKRun/root/parent, run state/version, schema, snapshot hash,
operations with namespaced identity/source identity+version+hash, call/turn/effect/
invocation/attempt links, recorded state, recorded timestamps, result/request hashes,
safe reason code and receipt identities. Keep proposed tool, authorized operation,
physical handoff and result distinct. No raw request/result payload, secrets, nonce,
private reasoning, arbitrary error text or unrestricted metadata in the projection.
No claims covering Host post-turn calls, Memory, Service or Host request authorization.

Coverage includes source availability/gaps and `truncated`. A full bounded current
snapshot is not a claim of full historic transition coverage. Legacy missing ingress/
attempt facts are explicit `legacy_partial`; missing data cannot mean success/no-op.
Usage retains source/provenance, unknown tokens and costs, budget reservation/estimate
vs actual reported values. No zero substitution, estimated charge called invoice, or
replay double-counting. Unknown only resolves through exact existing reconciliation.

## Durable source/writer choice

Reuse existing canonical provider/effect/decision/control/terminal/reconciliation
records. Add SDK-owned pre-effect tool boundary facts in existing append-only Run
events: original intent before preparation/authorization rejection, explicit outcome
or wait. Never create an execution_effect row for a rejected operation. Record request
hash/identity only, not raw credentials/arguments. Stable event identity makes replay
idempotent; differing intent on same key conflicts. Existing lease/fence validation
still applies; audit facts do not authorize work.

For covered provider/effect transition writes, append safe source-bound transition
facts in the SAME transaction as canonical state CAS, reusing run_events. This avoids
another execution state machine and schema bump. Reader exposes actual attempts and
receipts where recorded, and marks old incomplete history. Recording failure prevents
the covered transition/handoff rather than silently claiming a complete audit.
Provider/effect execution and authority semantics remain unchanged.

All new production SQLite paths default on. No NoopSink/ring/JSONL dependence. Full
coverage of additional workflow/context producers must be inventoried explicitly;
V1 cannot label unsupported boundaries as recorded. No new main version/wheel here.

## Decisive tests

Real SQLite kernel: provider/tool successful and failed outcomes; immediate deny
with effect=None; REQUIRE_USER durable decision then exact allow/duplicate; timeout/
unknown + existing authorized retry/reconciliation; cancel/terminal; close/reopen
with no Provider/effect resend. Check identities and state/usage against public
canonical readers and deterministic physical call counters, not log counts.

Disable/overflow diagnostics and retain identical durable facts; arbitrary credential-
shaped arguments/errors must not be exported. Fault between canonical CAS and audit
append rolls back both. Same-time calls/raw ID reuse across turns remain distinct.
Missing Run, legacy coverage, bounded overflow/truncation are typed and explicit.
Concurrency must return one DB read snapshot, no torn state/usage combination.

Exact tests/evidence and any uncovered kernel paths will be documented before review.
No paid Provider/native/full suite; independent Dirac challenge requested.

## Pending MUST — full-Run audit continuation (user clarification)

1. Stable pagination is a required successor, not an optional optimization. Increasing
   the V1 limit does not close every-operation full-Run audit. Pagination must bind an
   immutable source boundary/retained versions and exact source identity; no mixed live
   pages, no repeated whole-Run scan/hash as the lasting design. Contract/producer
   inventory must identify any missing durable watermarks before implementation.
2. Enumerate every Harness runtime boundary and its producer, durable source, public
   projection and oracle. Unknown-tool/argument validation, immediate/durable denial,
   effect dispatch/result, Provider/retry/reconciliation, context, control and terminal
   require individual coverage status. No global full-history claim from one new marker.
3. Public metadata separates `current_source_complete` (all records from the explicitly
   supported current source set returned without truncation/unavailability) from
   `history_coverage` and its missing boundaries. Neither means `fully_audited`: that
   additionally needs a versioned audit consumer/rules/findings receipt. Avoid a generic
   `complete` field. Truncated/unavailable snapshots cannot be current-source-complete.
4. V1 is the first bounded SDK slice only. Full-Run audit and cross-SDK coverage remain
   open until these MUSTs and owning-domain integration are verified. No paid calls.

## Implemented V1 consumer contract (fixed source pending review)

- Synchronous port/UoW signature remains above; public facade is
  `await runtime.client.read_run_operation_audit(run_id: RunId, *, limit=256)`.
- Package-root exports: RunOperationAuditPort, RunOperationAuditSnapshotV1,
  RunOperationAuditV1, RunAuditUsageV1, RunAuditUnavailable. Root lazy exports remain.
- Snapshot: schema_version/run_id/root_run_id/parent_run_id/run_state/run_version,
  operations, truncated, source_set, current_source_complete, history_coverage,
  coverage_gaps, snapshot_hash. `to_json()` is the safe projection.
- Operation: operation_id/kind/record_type/state/source_id/source_version/source_hash,
  handoff_attempt/rehandoff_count, operation_name, error_code/error_code_hash,
  created_at/handed_off_at/settled_at, request_id/call_id/raw_call_id/turn_ordinal/
  call_ordinal/effect_id/provider_invocation_id, request_hash/result_hash/evidence_ref_hash/
  authorization_ref_hash, optional typed usage. Names require actual registry provenance or a fixed SDK label; codes use a closed SDK vocabulary;
  arbitrary messages/payloads are not exposed. Unavailable values are None.
- `handoff_to_settlement_seconds` is computed only from recorded timestamps in order.
  For unknown it measures time until unknown was recorded, not proof of physical
  completion. Rehandoff uses that attempt's timestamps, not initial claim→latest end.
- Typed usage separates actual reported tokens (possibly None) and SDK budget kind/
  amount. No billing attribution is invented; only current head contributes to totals,
  never count its transition facts again as new billed calls.
- Link to a Provider parent only after actual ReAct identity, Run/request, public
  response call ordinal/raw ID/tool/argument hash verification. Custom/standalone
  unbound effects retain None. This is not timestamp/ordinal-only matching.
- Missing Run/store/port/corrupt source yields RunAuditUnavailable with stable reason;
  invalid RunId type or limit (1..4096) rejects. Truncated results are explicit, not
  silent best-effort diagnostics. Read transaction uses deferred BEGIN (no write lock).
- New event identities bind source/phase and payload hash; same logical requested
  identity with changed intent conflicts, exact replay deduplicates. Source CAS and
  transition append share the canonical transaction; injected append failure rolls
  both back. It cannot undo physical I/O or rewrite unknown to success.

Exact limitations and MUST successors are enumerated in COVERAGE.md.

Terminal effect result replay retains the original executor full intent validation and
does not create a new requested fact or demand a fresh write lease. Existing durable
terminal source remains the audit authority; legacy facts are not restamped.


## Fixed-source metadata P1 correction (supersedes earlier label wording)

The first 0eb1d15 candidate incorrectly treated a regex and credential keyword blacklist
as a metadata whitelist. Dirac demonstrated arbitrary ToolResult.error_code leakage;
that candidate is BLOCKED, not accepted. The correction has no blacklist expansion:

- Only the explicit SDK error code vocabulary is exported in error_code; all other
  Host/tool/provider strings have error_code=None and retain error_code_hash.
- Tool names require the executor's successful real registry.get and its registered
  ToolSpec.name. The append-only requested fact records that provenance. Unknown
  model candidates have operation_name=None and operation_name_hash; old facts with
  only operation_name are not retrospectively certified as registered. Registration
  is the Host's explicit public catalog boundary, not a claim that every arbitrary
  name-looking string is harmless. Generic events use fixed run.event plus kind hash.
- operation_id/source_id/request_id/call_id/effect_id/provider_invocation_id are opaque
  domain-separated hash references. Same entity uses the same domain: provider head
  source_id and effect provider_invocation_id join; tool/effect operation_id and
  effect_id join. These references cannot be passed to execution/mutation APIs as
  canonical IDs. raw_call_id=None; raw_call_id_hash also binds Run/turn/call ordinal,
  so a Provider's reused raw ID cannot associate unrelated operations.
- run_id alone remains the exact caller query identity for Service ownership checks.
  root_run_id/parent_run_id are opaque run-domain references derived from the ledger,
  not claimed to have been explicitly provided by that caller. No new authorization
  is inferred from audit refs or hashes.
- Readers filter historical V1 audit facts too. Original durable rows/event hashes
  remain byte-for-byte intact. source_hash still verifies the complete original
  source, never the scrubbed metadata. snapshot_hash covers the final safe DTO.

This is a correction to the unshipped source candidate, not a changed frozen wheel.
Service was notified of reference semantics; installed consumer acceptance is separate.


## Stable pagination successor status

The pagination MUST above now has a source implementation, six decisive tests and an
immutable derived-snapshot contract in PAGINATION.md. Independent fixed-source review
is pending; installed Service/Host consumer and full producer enumeration remain open.
The bounded read API itself remains a single-snapshot compatibility surface. New
open_run_operation_audit / read_run_operation_audit_page remove its 256 truncation
barrier for complete traversal of the declared source set, subject to explicit capacity
unavailable. They do not certify missing historical operation producers.
