# Harness-owned Memory port audit and remaining no-Run boundary

2026-09-05. This audits SDK calls, not Memory SDK internals, actual materialization,
Host consumer optimization, or success inferred from observability.

## Committed-turn source leaf

Actual terminal _insert_committed_turn writes the introduction fact in its authority
transaction. Repository claim/release/settle record full original row hashes, actual
claim_epoch/attempt_count and owner hash in run_events. Dispatcher validates the
current claim before recording a durable handoff; Memory invocation is subsequent.
No durable settlement means unknown. A validated typed receipt is hashed and bound
to exact turn/payload/owner/epoch in the same settlement transaction. APPLIED outbox
is only logical acknowledgment: receipt rejected_erased/conflict/already_applied
stay distinct public outcomes. Original physical ambiguity/retry authority is unchanged.

Each CAS returns its immutable transaction-local result, not a later owner's row.
Cleanup writes a tombstone with the actual final row hash before deleting the outbox
head. Reader enumerates immutable facts even after cleanup; legacy observed heads
have introduction gaps and are not manufactured creation. Missing claim epochs,
whole handoff, or final settlement are explicit unverified coverage, not zero calls.
Source page cuts include these existing run_events, and parent links use the actual
memory_port root reference. No raw payload, receipt ID, credential or error text is
exported; closed memory codes/hashes retain useful grouping.

Evidence in ignored .local-test-evidence/2026-09-05/run-operation-audit/:
- memory-port-red.log:2 real terminal/call counterexamples missing public attempt.
- memory-port-adjacent.log:31 PASS0.54s, only directly affected Memory outbox/barrier.
- memory-port-pair.log:6 leaf PASS0.24s including whole-start corruption and CAS race.
- memory-port-old-middle.log: exact installed072 really calls once between new
  epochs1/3; missing2 remains memory_outbox_claim_history_unverified.
No paid Provider/native/full suite. Fixed source review required.

## Remaining explicit no-Run storage contract — not a silent waiver

ContextProvider.prepare_once is called from _prepare_agent_context before a Run is
created. A failed stage can have no materialized Run and no command at all (legacy
public start); memory_recall_releases also belongs to this real stage and can survive
before Run consumption. Existing run_events Run FK cannot store these facts. A
fake Run/command, caller-supplied target binding, or observer diagnostics is invalid.

Proposed minimal successor is explicit observational audit schema2 (execution7
unchanged), with append-only stage-domain source/call events. Validate exact schema1
before an atomic1→2 extension; read-only does not migrate. Old empty/exact1/fresh,
future/partial/altered/hash-unchanged rejection must have direct tests. Frozen072
writer is not assumed fenced. Stage structural transitions need same-TX SQL-owned
observations so an old writer's intervening lease takeover/cleanup cannot vanish
behind a new writer's current head. Only safe structural columns/hashes go into
those observations; raw lease token and private snapshot are not copied.

Actual Context prepare/release invocation facts bind the currently verified stage
lease/structural observation and exact request hash, with real result/failure or
unknown. Release's attempt_count is post-call retry metadata, not an invented lease
or pre-call attempt identity. Use a unique durable call ID, record before actual
public port await, and bind its result to that exact call. No retry-policy change.

Stage→Run association is recorded only from actual consume_in_transaction, with
root/continuation owning Run proven by canonical FK/context start authority. It
persists across legal cleanup. Pre-consumption queries stay stage-domain; real command
association, if exposed, must be explicitly proven from accepted command authority.
Public stage open/page is a separate domain with stable immutable source cut; cross
stage/domain/restore cursors reject. Run pages include only its actual consumed
stages. Unassociated stages never make a nonexistent Run query succeed.

The final coverage DTO must enumerate this introduction/history boundary separately
from current-source completeness. Pending schema/call coverage is a MUST and cannot
be hidden behind all_operations_recorded or arbitrary large snapshot limits.

## Fixed-source ba1 independent P1 corrections

ba1d6ec was independently BLOCKED: a caller-constructed claim could replace payload
while keeping real owner/epoch, and legal cleanup followed by loss of the whole
memory audit family erased the reader's expectation. Raw independent probes retained.

Begin/release/settle now compare immutable intent/Run/turn/principal/canonical payload
and hash against the actual transaction row. Receipt validation uses that actual
row, not caller fields. Same-owner/epoch does not authorize another payload.

Root/continuation terminal authority now records SDK-owned sdk_memory_outbox v1
with the actual committed-turn hash (or explicit None). The field is derived and
overrides caller terminal metadata; it is committed/hashed with the real terminal
receipt, independently of the removable outbox and its audit family. Reader also
uses the existing consumed legacy cursor's committed_turn_hash when available.
Missing expected memory facts after cleanup remain an explicit terminal-intent gap.
Legacy completed memory-enabled Runs without either retained proof are unverified.
Old receipt bytes are not rewritten: replay uses original payload when the additive
field is absent; new exact replay after cleanup validates the retained original hash
and performs no new dispatch/outbox creation. Changed intent still rejects.

Three decisive negatives red before correction. Direct Memory/barrier35 PASS;
root-terminal/continuation/failure-terminalization67 PASS; exact cleanup replay1 PASS.
Logs memory-port-p1-red.log, memory-port-p1-green.log,
memory-terminal-anchor-adjacent.log, memory-terminal-cleanup-replay.log. The stage
schema2 WIP is separate; this correction must be reviewed on its fixed source.
