# LC2 legacy context coexistence — scoped design and oracle

2026-09-14. Implementation candidate, NOT TESTED. Parent owns all tests, Provider,
native UI, ARCHITECTURE updates and commits. No P34 comparison behavior changes.

## Actual hooks and identity

- `runtime/assembly.py::_check_intent_contexts` compares exact persisted context
  and external admission fingerprints. It must remain exact.
- `Orchestrator._service_config/_context_profile_for` freezes/checks external
  admission identity; legacy absent identity stays absent.
- `agents/runtime.py::assemble_runtime` uses `LocalProviderAdmission` when the
  external port is absent and a lifecycle fence exists. `AgentBridge` deliberately
  distinguishes `ports.provider_admission` from `effective_provider_admission`.
- `execution/dispatch.py` calls acquire, synchronous handoff around SDK CAS,
  finally observe, and recover on reconciliation. Request preparation and the
  original request/Agent/source/context contracts remain in this path.
- `ProviderBudgetGuard._acquire` counts RESERVED/HANDED_OFF/UNKNOWN in the shared
  Orchestrator Store transaction. Its fingerprint is the budget contract and
  must not acquire a deployment compatibility fingerprint.
- `Orchestrator._provider_handoff_fence` uses the same Store transaction as
  public Mission cancellation. SDK UOW exposes original/effective invocations,
  Agent bindings, turns, runtime leases and confirmed-not-started resolutions.

## Narrow extension and migration

Add an optional local admission port to AgentRuntimePorts, mutually exclusive
with external provider_admission, retaining the SDK local fingerprint. This is
necessary because passing a compatibility guard as the external port changes
AgentBridge accounting semantics and frozen intent identity. Assembly receives
local ports separately. The per-pool estimator mapping explicitly permits None
for a legacy pool; exact intent checks still reject old guarded identities.

Add a versioned Orchestrator-owned `legacy_provider_slots_v1` table, activated by
an explicit per-pool estimator map containing legacy None entries (normally a
mixed legacy/guarded deployment). It records physical identities and states,
never token estimates, costs, or intent mutations. New guards count this table
inside their existing reservation transaction; old calls count both tables in
the same transaction. No SDK execution schema or old source/context sidecar is
rewritten. Migration is additive/idempotent and scans all original legacy
invocations before ANY pool starts. Absence of a context policy is not evidence
of absent admission: Host inspects persisted admission identities separately.

## Proof invariants

1. Every possible physical call occupies one durable slot before SDK CAS. Under
   the Store transaction, new reservation is allowed only if token-grant held
   count + legacy-slot held count is below the deployment cap; legacy pool cap
   is also enforced. No await occurs in either transaction.
2. SDK CAS can commit before the Orchestrator handoff transaction commits. A
   previously committed RESERVED slot bridges this crash window; recovery checks
   the SDK ordinal and retains it as UNKNOWN. Missing evidence never releases it.
3. A pre-handoff reservation can release only with the exact SDK record proving
   the handoff ordinal was not reached and its execution owner is no longer live,
   or after the local synchronous handoff path returned without CAS. UNKNOWN is
   retained until exact terminal usage or confirmed-not-started evidence exists.
   A local callback also checks the original SDK owner/epoch before changing a
   slot: an expired owner's callback cannot release a successor's reservation.
4. Imported handed-off/unknown records occupy slots before startup; the same Run
   cannot acquire another call while an unresolved slot exists. Completed legacy
   usage remains legacy accounting. No synthetic token grant is created.
5. Existing intent/config/input/source/request/context identities remain byte
   identical; compatibility rows are deployment accounting only. Bad/missing
   bindings or admission fingerprints fail closed, not by editing old evidence.
6. Old Missions keep old routing and profile. New Host Missions default to the
   named 256K profile and may choose 512K; their own context sidecars and intent
   fingerprints remain frozen across restart/default changes.

## Parent-run oracle

Fixtures must open a genuine legacy Orchestrator with no context policy or
external admission from the start; never delete a modern context sidecar.
Cover zero-call frozen pre-handoff with exact request/config/input/source hashes;
handed-off UNKNOWN and restart without rehandoff; old completed Mission cold
read; simultaneous legacy + two guarded pools under one global cap; cancelled
slot wait and cancellation between reservation/CAS; missing original record,
wrong fingerprint/config/source, missing sidecar and guarded pre-context pool.
All test outcomes remain NOT_RUN until the parent executes them.

## Boundary

This upgrade requires the Host's existing single deployment owner/quiesced
upgrade boundary: an older binary continuing to call without compatibility
accounting cannot be retroactively fenced. Old UNKNOWN may consume all slots;
this is a specific durable unresolved-call condition, not blanket legacy denial.
No automatic UNKNOWN settlement, provider retry, token-estimate invention,
fingerprint relaxation, public Mission schema change or old intent migration.

## Implementation handoff — all NOT_RUN

Files owned by this change, relative to the SDK root:

- `src/agent_orchestrator/runtime/legacy_provider_slots.py` — tri-state frozen
  admission lookup, auxiliary migration, original-record binding, shared local
  slot acquire/handoff/observe/recover; no token/price columns.
- `src/agent_orchestrator/runtime/provider_budget_guard.py` — add the legacy held
  count in the existing transaction; guard fingerprint calculation unchanged.
- `src/agent_orchestrator/runtime/assembly.py` — separate local ports and preserve
  exact external admission/context checks.
- `src/agent_orchestrator/orchestrator/event_handler.py` — constructor/startup
  only: explicit None legacy selection, bootstrap before all pools, sticky
  compatibility activation. Other agents' Worker callsite edits are not ours.
- `src/simple_harness/agents/ports.py` and `src/simple_harness/agents/runtime.py`
  — optional mutually-exclusive `local_provider_admission` wiring, same local
  fingerprint, external ports remain None for legacy accounting semantics.
- `tests/orchestrator/lc2/legacy_fixture.py` and
  `tests/orchestrator/lc2/test_legacy_context_coexistence.py` — original legacy
  runtime, SIGKILL pre/after handoff, synchronous exit in the two-database CAS
  gap, source/request/config identity, UNKNOWN plus actual new long calls, both
  acquisition orders at global cap, cancellation, completed old Mission,
  bad/missing identity records, source corruption, missing new context sidecar,
  changed creation default and fresh-long None-estimator rejection.
- This design/oracle/handoff note.

Host files (relative to `/Users/denny/projects/simple_harness`):

- `backend/deskpet/orchestration/runtime_profile.py` — remove blanket pre-context
  early return; select old admission independently from its context policy;
  expose both named long pools to the existing Host creation-default selector.
- `backend/tests/orchestration/test_source_runtime_profile.py` — genuine original
  runtime fixtures for guarded and unguarded pre-context pools and read-only
  source option resolution. Existing optional pinned-tokenizer test retained.

Parent execution targets (use the attested editable-source SDK/Host environment;
choose a fresh local evidence base directory for each run):

```sh
# SDK cwd. This worker has NOT run this command, including collection.
.venv/bin/python -m pytest -q tests/orchestrator/lc2 --basetemp .local-test-evidence/2026-09-14/lc2-parent-review/pytest
```

Exact adjacent SDK regression files for the parent:

- `tests/orchestrator/p35/test_provider_budget_guard.py`
- `tests/orchestrator/p35/test_provider_budget_recovery.py`
- `tests/orchestrator/p35/test_provider_budget_identity.py`
- `tests/orchestrator/p35/test_context_cold_recovery.py`
- `tests/orchestrator/p34/test_mission_runtime_profile.py`

Host pytest target:
`backend/tests/orchestration/test_source_runtime_profile.py` (editable SDK).
No Provider, API, network, build, test collection/execution, commit or ARCHITECTURE
edit was performed by this worker. Only source reads, scoped patches and a
whitespace diff inspection were performed. No PASS or LC2 closure claimed.

Known limits for review: recovery currently scans original legacy invocation
history and validates its bindings; large-library scan cost is not measured.
The subprocess fault fixtures require POSIX process groups. Raw test evidence
must stay local/ignored. Unresolved old calls can exhaust the shared cap and must
remain held. The existing Host is the single deployment owner; concurrent old
binaries that never participate in this table are outside this bounded upgrade.
Parent still owns test outcomes, native UI acceptance and ARCHITECTURE updates.

## Parent observation v1 and bounded repair (not rerun)

Parent ran `.local-test-evidence/2026-09-12/p33-g/g-lc2-p32-observation-v1.log`:
three LC2 failures. This worker read that log and changed only the LC2 test file,
constructor/routing portions of event_handler, and this note; P32 remains parent-owned.

- UNKNOWN carries the SDK's exact `BudgetCharge.unknown()` object in usage_json,
  without actual usage or response. The oracle now accepts absent usage_json or
  that exact object, while retaining UNKNOWN/handed-off, one handoff, zero
  rehandoff and occupied-slot checks. No arbitrary usage object is accepted.
- Missing invocation was rejected earlier by SDK integrity validation. Its case
  now specifically requires `RuntimeError` with `SDK execution database failed
  integrity validation`, still before any Provider call.
- Genuine routing bug: whitelisted policy params do not store the deployment
  default. `_router_for` previously reused the current deployment default for an
  old unselected Mission. It now recovers a default only from mutually consistent
  frozen dispatch decisions whose reason is `default`, validates decision/profile/
  Agent profile identity, and includes that default in the router cache key.
  Conflicting/missing configured historical targets reject rather than reroute.
  Old intents lacking runtime_profile_id use canonical DEFAULT_PROFILE, matching
  assembly's existing interpretation, instead of the current creation default.
  The regression additionally creates a new unselected Mission under the new
  default, checks same-policy cache isolation and exact old intent config equality.

Exact rerun targets are the three named tests in
`tests/orchestrator/lc2/test_legacy_context_coexistence.py` reported by v1, followed
by the complete LC2 file and routing/policy regressions at the parent's discretion.
All repair results remain NOT_RUN. No test/API/build/commit executed here.

Routing evidence limit: historical Missions with neither a selected profile nor
any frozen `reason=default` decision do not contain proof of their old deployment
default; this bounded repair does not invent it or migrate their records. Changes
to deployment by-role/escalation/fallback rules remain the existing policy behavior.
Full preservation for that evidence-free case needs a separately reviewed explicit
deployment-routing binding contract. This limit is not a claim that LC2 is closed.

### Stable handoff: explicit evidence boundary and negative controls

Added parent-run controls (NOT_RUN by this worker):

- `test_old_mission_without_dispatch_has_no_proven_historical_default`: genuinely
  creates an old Mission without an intent, verifies its stored policy has no
  default, and reopens with a different deployment default. The helper must return
  no historical default evidence; the unchanged current-deployment fallback is
  explicitly a limitation, not original-default preservation or LC2 acceptance.
  If historical deployment/policy records do provide the original default, that
  evidence must be validated and bound explicitly before claiming preservation;
  these current fixtures contain no such record.
- `test_frozen_default_conflict_or_identity_mismatch_rejects_before_calls`:
  decision/profile mismatch, Agent/profile mismatch and two conflicting but
  individually self-consistent default decisions all reject with ContractError.
  Deliberate negative corruption applies only to the test's actual frozen intents.
- `test_actual_role_override_is_not_evidence_of_the_deployment_default`: a real
  Planner route by role uses 256K while deployment default is 512K; that decision
  must not become historical-default evidence.

Parent reports Mill's independent cap/recovery review ACCEPT within its scope.
That report is not test execution by this worker or closure of the evidence-free
historical routing boundary. Production code unchanged in this follow-up; only
the LC2 test file and this note changed. Writes paused for parent SDK tests.

## Parent verified source checkpoint - 2026-09-14 CST

After first48PASS8FAIL72.90s, bounded repairs passed72/72 in13.07s (runner13.51), including22 LC2 controls, actual action-wire context, protocol metadata, known/unknown billed-failure accounting and no resampling/tool execution. Adjacent budget identity/recovery/context/profile plus LC2/action56PASS13.64s (runner14.09). Host with pinned tokenizer45PASS0.52s (runner1.10), zero skip. SDK mypy120filesPASS; new LC2 formatting/imports and protocol detail typing repaired by parent. Native source acceptance and final cumulative audit remain pending. Evidence labels: g-lc2-p32-observation-v2, g-lc2-adjacent-v1, g-host-lc2-profile-v1 under SDK .local-test-evidence/2026-09-12/p33-g. Historical no-dispatch/no-default-evidence limit remains explicit. No packaging/installer/release/push.
