# Mandatory context action: bounded same-Run repair

2026-09-06. Authorized successor from H078 `bb9abfd`; no change to frozen wheels,
original userdata, existing no-recall receipts, ACK or mandatory-exit semantics.

## Decisive counterexample and authority

Native r16 registered/applied a real overdue timer and injected one pending
occurrence. Provider returned an ordinary answer without tools. Host raised
`sdk_no_recall_blocked_pending_occurrence` inside its Provider coordinator after
the SDK recorded physical success but before ReAct checkpointed the response.
Run failed. Presented and physically consumed remain unACKed, hence pending.
Two occurrence rows are phases of one occurrence, not two deliveries.

Only a public, identity-bound `MandatoryContextActionRequired` rejection for
`pending_prospective_occurrence` can request repair. Run ID, request fingerprint
and Provider turn must equal the checkpoint. No raw exception text/private action
content enters feedback. Foreign identity, malformed rejection, permission/source
errors, cancellation and budget failure propagate unchanged. No invented tool call,
route receipt, user message or ACK is permitted.

## Public seams and ordering

1. `ProviderInvocationCoordinator.prepare_context_use_terminal(...)` is an async
   post-response-checkpoint hook. The SDK default performs no Host business write;
   Host overrides it to call the existing typed terminal/no-recall sink. The
   original public `verify_context_use_terminal` still checks actual successful
   use and exact Host terminal receipt after this hook succeeds. Host removes its
   early terminal call inside `invoke`; physical success/usage stay successful.
2. The unprotected no-recall sink uses the same typed rejection contract. SDK
   catches it only at these precise terminal decision boundaries. All other
   rejection paths remain terminal. A routed request follows existing route/ACK
   enforcement; route itself does not fabricate an ACK.
3. Persist each rejection in ReAct checkpoint with ordinal, request ID/hash,
   response hash, stable reason and repair ordinal. At most TWO repairs per Run;
   the third rejection produces an explicit bounded failure with pending intact.
   The normal turn/tool/cost/deadline budget is never reset or extended.
4. Persist a dedicated repair phase before any feedback append or next request.
   Idempotent SDK Context append uses the rejected request identity. Crash/reopen
   resumes that phase without another physical send of the old request or a new
   rejection count. Original successful Provider response stays in its durable
   invocation and checkpoint history; it is not a final answer or fake tool result.
5. The next `RunContextAuthorityRequest` carries typed, payload-free feedback.
   Host includes the SDK's exact SYSTEM control message in its bounded snapshot;
   SDK verifies exact inclusion before reservation. The fresh snapshot, actual
   Memory grants, current source/disclosure checks and physical guard all run
   again. No injection after request hashing. Generic no-authority consumers get
   the same message through ordinary durable Context.
6. Subsequent zero-tool output must pass the original sink again, including newly
   pending occurrences. Real `prospective_ack`/legal mandatory exit remains the
   only processing authority. Failed old r16 Run is not silently resurrected;
   a new actual Run can process its still-pending reminder.

## Durable compatibility

New repair-bearing checkpoint wire schema 8 is strict and preserves all existing
schema 6/7 fields. Untouched Runs retain their original wire. H078 rejects schema8
before executing that checkpoint, rather than dropping repair counters. No table
or old receipt/hash migration is required for this checkpoint-only extension.
Existing invocation UNKNOWN semantics remain no-resend. Snapshot wire/hash and
Provider identity algorithms do not change. A successor candidate is allocated
only after scoped source review; no in-place H078 relabel/build.

## New decisive controls only

- Actual durable Provider: first zero-tool success -> feedback/new request in same
  Run -> real ACK tool -> final answer; Host main authority/sink path is required.
- Crash after response/rejection/feedback and reopen: no first-request resend,
  stable rejection identity/count and no budget reset.
- Repeated zero-tool output: two repairs then real bounded failure, no ACK.
- Foreign/stale typed rejection and unrelated error: no repair or extra send.
- Privacy revoked during repair: fresh authority/physical guard prevents send.
- Strict serialization/old decoder refusal; generic no-Memory behavior preserved.

Source controls are not native proof. Actual native re-open/retry is coordinated
by main after artifact and Host integration; old failed Runs/evidence remain.
