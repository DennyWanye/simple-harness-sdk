# H077 authorization expiry terminal proof — bounded contract

Date: 2026-09-06. Base: frozen H076 `bc2d42c73d83f7400fa9b1ebff9f9d0f1c461fd2`.
Branch: `feat/decision-terminal-recovery-077`. H075/H076 artifacts stay unchanged.

## Actual defect and authority

The r6 ordinary root `product-sdk-195446...` has failed/version 3, a single
`decision.expired` at 06:35:27 UTC, and **zero** run terminal events. Installed H075
public `read_run_operation_audit` returns `terminal_evidence=None`. r7 cold restart
exposes this existing gap; it did not duplicate a terminal. Runtime authorization
expiry maps to DENY/EXPIRED; `commit_decision` updates the Run to failed but only
writes the decision event. Ordinary stop/recovery must not invent a terminal.

Forensic reads are diagnostic only. Host consumes SDK public evidence; no Host
private-SQL fallback, arbitrary last-event selection, or fabricated terminal.

## Future decisions

For ordinary root decision transitions to denied/expired/cancelled, the same UoW
transaction must commit the decision, Run state, decision event, and one exact
`run.failed` / `run.cancelled` event bound to that decision/event/request/response
and resulting Run version. Fault before commit rolls everything back. Exact replay
returns the original decision and terminal; no second event. ALLOW is unchanged.
No tool/provider send or new delivery is introduced. Child workflow/admission
terminal domains are not silently reinterpreted by this leaf.

## Explicit recovery of the observed legacy gap

Public synchronous entries on `simple_harness.execution.sqlite.SqliteExecutionUnitOfWork`:

```python
witness = uow.read_expired_authorization_terminal_recovery(RunId(actual_run_id))
proof = uow.recover_expired_authorization_terminal(witness, now=trusted_now)
```

The read returns root-exported `ExpiredAuthorizationTerminalRecoveryV1` with
`run_id, decision_id, run_version, source_hash, original_resolved_at`, or None for
missing/nonfailed Runs. Unknown failed shapes raise `UnitOfWorkConflict`. Host
needs no SDK SQL to find decision/version. The witness is not a permission or
terminal; recovery re-reads every commitment in one immediate transaction and
returns the existing `RunTerminalAuditEvidenceV1` type.

Eligibility is deliberately the observed first-authorization root React shape:
complete original business sequence create(v0), activate(+1), decision.open(+1),
decision.expired(+1), with exact SDK event IDs/payloads, original same-transaction
SDK audit witnesses, start snapshot hash/profile/driver identity and monotonic
source times. It derives the current version from those actual version-changing
commands and rejects extra transitions or a different current version; merely
passing version3/current MAX or matching timestamps never authorizes repair.
Original terminal decisions must be uniquely expired tool_authorization/version1;
nonce, original deadline, DENY response, SDK/Host binding digest and SDK receipt
intent must all agree. The legacy same-clock plus version-increment negative is
explicit; unknown later-cycle expiry shapes remain unsupported rather than being
heuristically restored.

Actual r6 has **no execution_effect row**: REQUIRE_USER precedes effect admission.
Require all Run effects absent plus the exact original two requested/waiting tool
records, matching effect/call/operation/tool hash/argument commitment and their
SDK audit identity hashes before decision.open. No later tool record or business
transition is accepted. This proves the supported pre-effect no-handoff path;
existing prepared/handed-off/unknown effects are outside this recovery scope.
Future root React tool_authorization terminal decisions are covered in both UoW
branches; workflow/child and generic admission terminal domains are unchanged.

Success appends a `run.terminal_recovered` audit event and one `run.failed` terminal
with explicit recovery origin, original source hashes/resolved_at and actual new
recovery timestamp. It does not alter the original decision/event/Run state/version,
permission, checkpoint, receipt, or payload. No backdated event. The first recovery
identity is deterministic from exact existing sources; replay/reopen/concurrent
retry returns the same public terminal proof without another append, regardless of
later retry time. ACK loss after commit must replay that exact proof.

This is an explicit supported operation, never a database-open auto-repair. Host
can call it only for the exact affected Run/decision then read the normal public
audit terminal. Generic missing/multiple terminals continue to refuse. No arbitrary
repair service, new authorization authority, or bulk scan/migration is introduced.

## Decisive checks (new only)

- Real Runtime user authorization wait -> expired ALLOW / explicit DENY: no physical
  tool call, one matching terminal proof; reopen/stop/replay do not re-emit.
- Pre-fix SDK-generated expired database: public terminal absent -> explicit recovery
  -> exact public terminal; old rows unchanged, actual recovered_at distinct.
- Wrong version/Run, binding/source tamper, already conflicting terminal, missing
  expired source and effect already handed off: refuse without mutation.
- Recovery transaction fault before commit remains absent; post-commit ACK loss,
  reopen and second caller return exact original proof.
- Read-only WAL-aware copy of r6 via new public recovery; never original userdata.

No old lease10/analysis14 repeat. Source fixed -> Dirac challenge -> focused tests
under the default Host shared resource runner. New candidate version reserved077,
packaging only after review; no new wheel in this source step.

## Public Host terminal consumption

`uow.read_run_terminal_record(RunId(...)) -> RunTerminalRecordV1 | None` performs a
single read snapshot, strictly validates unique terminal kind/state, and returns
actual `run_id`, `event_id`, safe `error_code`, and the unchanged
`RunTerminalAuditEvidenceV1` as `terminal_evidence`. No terminal payload is exported.
Missing proof for a terminal Run or conflicting identities refuses; missing/nonterminal
Run returns None. Host can replace its existing SDK-private SELECT with this exact
metadata and original public proof comparison. Recovery and normal future expiry use
this same reader. This introduces no new authority or alternate terminal selection.
