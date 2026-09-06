# Receipt-bound Provider use — implementation contract

2026-09-06. Authorized successor from frozen H073 source0282fa; package version
unchanged until a later artifact allocation. Existing H073/wheels and Host are untouched.
Local candidate inventory has no version newer than0.7.3. Original design is Host
context-use leaf01e9e062/NEXT-HARNESS-CONTEXT-USE.md; this file freezes the implementation
oracle before product code. No plan-test skill/machine gate.

Required identity: trusted authority scope + subject + Run + real durable turn and
continuation + actual Provider request + handoff ordinal. Hash domain
`simple-harness/provider-memory-attempt/v1` over those fields, canonical JSON
UTF8/sorted keys/compact separators, envelope `{domain,payload}`. requested_at and
the complete snapshot/fragment/intents are immutable checkpoint data, not part of
the stable attempt ID; changing them under an existing attempt is a conflict.

Missing/legacy carrier != empty attestation. Snapshot schema2 explicitly supplies
subject plus a tuple of intents (possibly empty); schema1 cannot authorize a
required runtime. Generic no-Memory runtimes retain schema1 without injecting new
kwargs into legacy providers. Once configured, the authority requires schema2 on
every new attempt and cannot silently downgrade on reopen/missing dependencies.

Receipts are actual Memory port returns. Context payload and receipt-free fragment
hashes are frozen first. The receipt lives only in an execution sidecar, never
backfilled into its own snapshot/hash. Per-result fragments and whole actual message
commitments bind the selected payload to the final Provider request. Host owns source
decomposition; SDK owns canonical identity, reservation, unique receipt association
and consumption. No user-supplied counter or metadata grants authority.

Before the external authorization await, persist the exact request/time and intent
under the canonical Run lease. Reopen repeats those bytes, even after Memory grant
commit but before Harness claim. Grant bundle + all receipt links + budget claim
commit atomically. A handoff checks that same bundle/expiry and consumes it by the
existing CAS, never a separate counter. Existing-record fast paths compare the
same sidecar. Stale lease/current continuation after await refuses.

SUCCEEDED replay returns stored response with no second send; uncertain HANDED_OFF
becomes UNKNOWN and never blindly replays. Trusted CONFIRMED_NOT_STARTED keeps the
existing retry cap, allocates a new handoff ordinal and obtains a new Memory grant
before atomic rearm. Suppression blocks that fresh grant. A consumed receipt never
becomes unused. Cancellation propagates and preserves uncertainty/owned cleanup.

Execution schema successor must reject downgrade to H073. Public explicit additive
upgrade accepts exact valid schema7+known audit catalog, requires a same-directory
WAL-aware backup, preserves all old rows/hashes/checkpoints/receipts and records an
idempotent upgrade receipt. Current schema no-op, repeated backup exact receipt reuse,
unknown/future/partial corruption readonly refusal. No userdata switching or reset.

Decisive tests (NOT_RUN until fixed source review and assigned145 shared lock):
- Real Memory public2item admission/mutation/recall/page/use and real Harness
  consumer: receipt first then suppression can send once; duplicate/reopen has
  same receipt/response and zero further physical calls; suppression first zero calls.
- Actual public continuation identity cannot borrow the old grant; clean continuation
  obtains a fresh one; changed snapshot/attempt/subject/receipt reuse fails.
- Crash before/after Memory commit, claim, handoff, response commit/ACK; concurrent
  owners; durable sidecar and physical invocation counts agree.
- UNKNOWN no replay, confirmed-not-started new grant/generation, suppression before
  retry refuses, untrusted reconciliation refuses. Preserve existing provider recovery.
- Nonempty schema7 migration, WAL-only commit, reopen/same backup, integrity/unknown
  schema/old binary refusal. No fake rebuilt old database identity.
- Public no-Memory consumer remains compatible; configured required consumer rejects
  omitted/legacy carrier. Host production binding remains a separate downstream leaf:
  exact typed occurrence only, no exemption for raw USER/history/standalone short.

## Concrete source interfaces — 2026-09-06

Root exports: `RecallContextUseIntentV1`, `ProviderContextUseAttemptV1`,
`ProviderContextUseGrantV1`, `ProviderContextUseViewV1`,
`RecallContextUseAuthorityPort`, `migrate_execution_v7_to_v8`,
`ExecutionContextUseUpgradeReceiptV1`.

`ConsumerRuntimePorts(run_context_authority=..., recall_context_use_authority=...)`
uses the same coordinator as direct `ProviderInvocationCoordinator(...,
context_use_authority=...)`. The authority scope is configured by the trusted Host, never taken from model
metadata. `run_context_use_requirements` atomically pins generic/required scope
with SDK legacy/Host-control admission, public start-command acceptance and
root/child materialization. It links the existing source admission/command/start
hash without changing old RunStart canonical bytes. Replay verifies that same
source and scope. Startup validates active Run/pending-command requirements before
reconciliation or driving; missing/changed authority refuses without terminalizing
the accepted Run, including before any ReAct checkpoint exists. A checkpoint's
scope remains an additional exact binding, not the first authority pin. Old
unproven active Runs are explicit legacy: no typed adoption or synthetic empty
attestation. Generic no-Memory recovery remains compatible.

`RunContextSnapshot(schema_version=2, recall_subject=..., recall_intents=tuple)`
retains its existing Provider payload hash and adds the sidecar hash to its receipt.
Each intent carries receipt-free full fragments and `(one_based_message_ordinal,
canonical_message_hash)` pairs. Decision/result/item/fragment hashes are derived
from these public bindings, not duplicated caller authority fields. The durable
attempt's `request_fingerprint` is also the existing context payload hash; these
are not two independently variable fields. New domains are listed above and all
use canonical object-envelope hashing, never NUL or a changed v4 Memory hash.

`RunClient.read_provider_context_use(RunId, RequestId)` and the coordinator's
same-named method return an immutable, **payload-free** `ProviderContextUseViewV1`
or None for a generic unprotected invocation. It includes exact invocation state,
version and consumed handoff count; scope, subject, Run/turn/continuation/request;
Provider/global/handoff ordinal, snapshot identity/revision, fingerprint/original
time, stable Memory attempt ID, intent/grant hashes, actual use requests/receipts,
and per-result whole-message bindings. It returns neither fragment payloads nor
source evidence bodies and confers no right to dispatch or to redisclose history.
A prepared intent without a claim has no use view. A corrupt/missing protected
association raises, rather than masquerading as an ordinary no-Memory invocation.

New public continuation ingress preserves `context_use_turn_id` from the actual
`ContinueCommandIntent.turn_id`; legacy signal_conversation uses its established
turn=continuation convention. After ACK, checkpoint active identities remain.
Legacy durable conversation carriers without that proof refuse typed consumption.
When a new typed runtime receives a command without a legacy AgentMemory prepared
stage, it appends only the SDK-admitted current USER and obtains the final complete
Provider snapshot from its configured schema2 authority; this does not synthesize
an empty recall carrier. Already reserved unresolved Provider work cannot be
relabelled to the new continuation.

The explicit **synchronous** migration is
`migrate_execution_v7_to_v8(path, *, backup_path, timeout=5.0)`.
Missing source paths are for the builder. Valid fresh8 returns None; migrated8
replays the same receipt and verifies the retained backup bytes. Exact valid7 plus
known audit1/2 catalogs is accepted; unknown objects/version/corruption refuse on
read-only validation. A same-directory WAL-aware SQLite backup precedes additive
DDL in a write transaction; no table rebuild, application reset or old row/hash
rewrite. The old7 descriptor row is retained alongside the new8 descriptor, which
old H073 rejects. The migration performs complete finite-catalog integrity/root
scans; no P99/bounded-total-work performance claim. The source test deliberately
retains a pre-commit backup and proves retry does not overwrite it.

## Evidence scope and remaining gates

NOT_RUN at the first fixed source handoff. Public tests build real S1, two CREATEs,
recall, two result pages, actual Memory authorization and a real Harness public
consumer. Continuation control uses public command ingress and a test-owned initial
WAITING driver boundary, then the production ReAct driver/coordinator. It does not
claim a real model/native/Host primary continuation. Separate source tests use
owned SQLite inspection and faults to examine original time, rollback, lost lease,
UNKNOWN, fresh grant retry, concurrent handoff and continuation binding. They are
not counted as public-only SQL-free proof.

No frozen H073/M0613 bytes, Host production module, native userdata, wheel or pin
is modified. Package version remains the base version during source review and
must not be mistaken for exact H073. Candidate version/artifact/API snapshot will
be assigned once source review and these controls converge. Host default wiring,
physical guard exact typed-occurrence exemption, 401 rerun and production promotion
remain separate; this source checkpoint closes no formal 401 cells by itself.


## First challenge corrections (fb0feaf retained as the original counterexample)

Dirac's read-only review identified two P1s before any tests ran:

1. M0613 authorization is unique per principal + provider_attempt_id, not per
   result. A physical attempt now derives a stable **per-result** Memory child ID:
   `E("simple-harness/provider-memory-result-attempt/v1", {provider_attempt_id:
   physical_id, decision_id, decision_hash, result_id, result_hash})`. It excludes
   time/receipt; full item/fragment/message intent remains immutable in the same
   checkpoint and prepared row, so changing it under that result still conflicts.
   All child grants link to one physical claim/handoff; no retained result is dropped.
   Public control uses two independent actual results, each with two real items,
   and checks both child IDs independently plus one send/reopen no second send.
2. Activation-only pin allowed a configured accepted Run to lose required mode
   on crash before its first checkpoint. The admission binding above now closes
   that window. Source crash controls use real public start or public command
   acceptance; pre-checkpoint/schema6/schema7 recovery without ports or with a
   different scope must refuse, same-scope recovery must proceed. The schema6/7
   controls explicitly release the abandoned owned lease before process exit;
   this is not evidence of arbitrary-expiry recovery or independent dual owners.

Migration tests verify the exact frozen H073 wheel SHA, installed package bytes,
directURL archive hash and prefix-owned import origin under `python -I`, not just
its version string. Same-owner concurrent coroutines prove only one handoff for
that owner; independent dual-owner fencing remains a separately labelled control.
