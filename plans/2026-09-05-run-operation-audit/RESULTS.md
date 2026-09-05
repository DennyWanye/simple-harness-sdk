# Harness bounded Run audit — source candidate results

2026-09-05. Isolated feat/run-operation-audit from exact
`2b8428465cbd41032ba024a0b7199183161f5ecd`. No frozen main, package version, schema
version, installed wheel or Host/Memory/Service code changed. No paid Provider/native.

## Implemented and tested first slice

Public synchronous RunOperationAuditPort/SqliteExecutionUnitOfWork reader and async
`runtime.client.read_run_operation_audit(RunId, limit=256)` facade; package-root DTO
exports. Canonical source set, bounded atomic snapshot, explicit truncation/unavailable,
current_source_complete distinct from partial history coverage. Safe operation grouping,
recorded phase timestamps/duration, codes/hashes, call/effect/Provider links and typed
reported usage/budget provenance are preserved. Audit is not a billed-cost assertion.

Pre-effect requested/waiting/failure/denial facts use SDK-owned run_events even when
no effect exists. Provider/effect CAS writes and transition audit append are in the
same transaction; exact replay deduplicates, changed intent conflicts. Audit payload
hash binds event identity. No diagnostic sink or second execution state machine.

**73 passed in1.48s, exit0**; 16 dedicated audit cases plus 57 directly adjacent
real runtime/authorization/route/recovery/observability cases. Ruff selected changed
files: **all checks passed, exit0**. No full suite or artifact acceptance claimed.

Dedicated oracles:

- Real SQLite kernel REQUIRE_USER/open→deny or allow, actual physical counter0/1,
  public audit reads and DB reopen do not dispatch again.
- Actual Provider timeout→unknown→close/reopen→existing exact not-started receipt→
  one second handoff→success. The original unknown transition remains, attempt2 is
  distinct from the logical invocation, physical Provider calls2. Recorded tokens
  7/3/10/cache1/reasoning2 stay exact; absent first usage stays None. Per-attempt
  handoff→settlement duration4s does not include offline/restart time.
- Immediate authorization deny, unknown tool, malformed arguments all persist pre-
  effect facts; no execution_effect row is manufactured and physical tool calls0.
- Real handler failed/partial/unknown states remain exact; unknown Run waits, failed
  tool may coexist with a subsequently completed Run. Secret-shaped result/error
  canary is absent from export, original durable source is not deleted/restamped.
- Reused raw call ID across two Runs produces different internal call/effect IDs;
  actual ReAct response/ordinal/name/argument hashes verify Provider parents.
- Cancellation of open authorization never becomes a tool handoff.
- Recording/queue overflow cannot change the durable snapshot. Default no-sink
  runtime is independently used by the other kernel cases.
- Fault after audit append during reconciliation rolls back both source success
  and audit; previous unknown snapshot/hash remains unchanged.
- Concurrent WAL writer settles Provider mid-read: current snapshot stays wholly
  at the earlier transaction; next read observes the new state/hash.
- Missing Run, invalid bounds and explicit audit payload corruption fail closed;
  limit1 is truncated/current_source_complete=False. New events never certify all
  old history; history_coverage remains partial with declared gaps.

- Immutable terminal effect replay with an expired lease returns the original result
  without new audit facts; changed arguments still fail exact intent validation.
  The decisive pre-fix test failed on the new wrapper requesting a write lease.

## Red and evidence

`kernel-red2.log`: 2failed0.23s, exit1, actual kernel reached authorization wait then
failed on missing public read_run_operation_audit. An initial collection-path error
is retained separately and is not the behavior red. Later development failures
(import/DTO, frozen arguments, fixture waits) are retained in the same ignored folder.
The effect=None oracle explicitly requires tool boundary rows in the real database;
it cannot pass by enumerating effects or replaying diagnostics.

From this worktree:

```text
PYTHONPATH=src /Users/denny/projects/simple-harness-sdk/.venv/bin/python -m pytest tests/integration/runtime/test_run_operation_audit.py tests/integration/runtime/test_react_sqlite_runtime.py tests/integration/runtime/test_h13_provider_recovery.py tests/integration/runtime/test_h13_tool_recovery.py tests/unit/observability/test_observability_v1.py tests/integration/execution/test_effect_reconcile.py -q -p no:cacheprovider
```

This runs the isolated source via PYTHONPATH, not a frozen wheel or installed-artifact
claim. Raw files remain ignored in `.local-test-evidence/2026-09-05/run-operation-audit/`.

| File | SHA256 |
|---|---|
| final3.log | 68d1dbd64f68ed1851104b3743f8ccaf83eb09243ef1a12a67920146fe193a8a |
| replay-red.log | 818c44d3d2565d28741ea7b390d85bd6d53af6d11bc4c0aa6a8fd568eaab2d4f |
| kernel-red2.log | a5b26e61fb34b64ebc8731564629859cf6bc73f379c1cda452c7e1803a3ec056 |
| lint-pass.log | 82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18 |

## Not completed / required successor

Stable pagination, complete per-boundary producer inventory/markers, pre-runtime and
pre-claim barriers, full context/recall semantics and all workflow/child/control variants
remain MUST successors. See COVERAGE.md; raising limit is not a substitute. No
fully_audited claim, cross-SDK audit, automatic optimization findings, Host Service
ownership integration, wheel/version bump, or native production completion.

Independent Dirac contract challenge accepted bounded V1 as a first slice; fixed-source
review pending. Coordinator owns promotion/combination and later artifact preparation.
