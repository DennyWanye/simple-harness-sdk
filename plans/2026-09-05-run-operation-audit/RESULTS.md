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


## Metadata P1 correction — independent review requested

0eb1d15 is independently BLOCKED: real ToolResult.failed with arbitrary error_code
`tok_demoAuditCanary_4931` exported that string in both head and transition. The regex
was not an export authority. `metadata-red.log`: 1 failed, 15 deselected, 0.14s,
exit1 reproduces it through the actual SQLite runtime with one physical tool call.

Correction: closed SDK codes; external code hash only; actual registry name provenance;
unknown candidate name hash only; opaque join references and Run/turn/ordinal-bound
raw call hash. Original exact source hashes and original rows remain intact. One new
historical V1 append fixture verifies reader filtering without rewriting source bytes,
including caller-like reference canaries; kernel unknown-name/raw-call and arbitrary
error-code tests verify live export. Existing registered-name, SDK error-code and exact
Provider/effect join positive oracles remain. See CONTRACT metadata section.

**74 passed in 1.57s, exit0** using the same six-file command above: 17 dedicated plus
57 adjacent. Selected changed-file Ruff passed. No paid Provider/native/full suite,
no frozen main/version/wheel changes. Fixed correction review remains pending.

| File | SHA256 |
|---|---|
| metadata-red.log | 7e0ad5b11b891f3b3e46dae926c66562a0eb70a71683d0a949644bf816da7228 |
| metadata-final2.log | 389e303ba0c1e77857746e3b42e04a6982d59b59e3f5d14f94c60e441873c450 |

The previous Service five-case source overlay used 0eb1; it is not proof of the new
reference semantics or an installed artifact. Service was notified, no Service code
or frozen artifact was edited here. Pagination/full coverage MUSTs are unchanged.

## Stable page source successor

Metadata base6a8b0e4 has Dirac's scoped ACCEPT (same real kernel canary probe no
longer exports the code, one physical tool/Run completed; reviewer did not rerun74).

Stable open/page API implemented; exact contract/resources/derived retention and
six new oracles are in PAGINATION.md. **80 passed in2.02s, exit0**, selected changed
Ruff passes, diff whitespace check passes. 6 page +17 audit +57 adjacent tests.
The new wait-snapshot oracle fails against an exact git-archive of6a8b0e4 source
with AttributeError for missing public open_run_operation_audit after real kernel
authorization wait (1failed3deselected0.25s, exit1). It passes on the new source.
The >256 case uses60 distinct valid tool arguments; the initial identical-argument
fixture correctly hit the existing repeated-tool limit and was corrected without
loosening production termination or authorization checks.

```text
PYTHONPATH=src /Users/denny/projects/simple-harness-sdk/.venv/bin/python -m pytest tests/integration/runtime/test_run_operation_audit_pages.py tests/integration/runtime/test_run_operation_audit.py tests/integration/runtime/test_react_sqlite_runtime.py tests/integration/runtime/test_h13_provider_recovery.py tests/integration/runtime/test_h13_tool_recovery.py tests/unit/observability/test_observability_v1.py tests/integration/execution/test_effect_reconcile.py -q -p no:cacheprovider
```

| File | SHA256 |
|---|---|
| pagination-red.log | 4dc7a1892c4e1ccbff2bd33c54953c29614ff9e4f664114cb0c64f01307a0d3b |
| pagination-final2.log | 41c9c95fed12c0af0cae5d7dd4670e78ea04e4d064d3a8d7b5b18d806031591f |

Fixed pagination source review pending. Original bounded API and its truncation
semantics remain; consumers must explicitly adopt page APIs to traverse a full current
source snapshot. No installed consumer/artifact/native acceptance, no full-suite run,
no full producer/history coverage or automatic optimization audit claim.


## Pagination incarnation P1 correction

6b5d was independently BLOCKED: filesystem namespace survived same-path copyfile
replacement and old cursor leaked a previous Run snapshot. New format2 pins the actual
immutable Run/start/session-owner anchor and captured append-only event cut, checking
both on each page. No schema/epoch invention; old manifest version rejects.
Four real replacement tests red: **4failed6deselected0.23s exit1**. Fixed seven-module
command above: **84passed2.00s exit0** (10 pages +17 audit +57 adjacent). Source only;
fixed independent re-review pending before successor wheel preparation.

| File | SHA256 |
|---|---|
| incarnation-red.log | 7ca3fefb5629a9a540457a945bf09a92d62d57488199137c8ad89f212be40efc |
| incarnation-final.log | 565be27be4075c65f492497b0dab0db10d17a44878173113680a1037fd51580a |

Read-only version survey: local branches/tags show highest0.7.2; remote ls-remote was
saved locally. Next proposed successor0.7.3 is not allocated/built until pagination
review passes. Frozen0.7.2, main and tags remain unchanged.

## Async audit production seam

Host consumer identified the old async facade's synchronous build blocking its event
loop. Dedicated mode=ro audit connections plus worker offload fix it without sharing
runtime transactions or asking Host to call private storage. Actual kernel cancellation
can commit while a gated audit source transaction is active; returned page stays at
waiting. Red **1failed10deselected0.67s exit1**; green same seven-module command
**85passed2.09s exit0**. Existing WAL concurrent-writer oracle now traces the real
independent reader connection. No test assertions removed; the original writer is not
used as an audit connection. Independent fixed-source review still pending.

| File | SHA256 |
|---|---|
| async-red.log | 721dbaf74e16a30e9705d8f3c4928a025238f38d3f64a8f921c7e28d9b9cf56d |
| async-final.log | b9c67ed1a7c47a9ea93f6a30b9e043b97f56147653ef08896b8c021499b2d835 |
