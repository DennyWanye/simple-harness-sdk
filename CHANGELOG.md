## 0.10.0 — agent_orchestrator 0.10.0: isolated execution and real, controlled delivery (P3.2)

Model-written code now runs through a **sandbox executor port** (`runtime/sandbox.py`).  The
receipt of every run says what really happened: whether it was isolated, which limits are
hard (CPU time, file size, wall clock, output) and which are soft (memory, process count —
sampled and reaped, because this platform has no cgroups), and whether every process of the
run is gone, measured after the reaping.  The macOS **seatbelt adapter** denies the network,
denies file content outside a read whitelist computed from the interpreter it runs, allows
writes only into the run's own throw-away copy and scratch directory, and denies signals,
mach lookups and information about other processes.  Its processes are found by *sandbox
identity* (a per-run canary plus `sandbox_check`), so a grandchild that left the process tree
with `setsid`, a double fork, `chdir("/")` and closed descriptors is still found and removed.
`ProcessOnlyExecutor` is **not** a sandbox and says so (`isolated=false`) — for trusted code.
A capability probe (8 behavioural checks) must pass before a deployment may call itself
sandboxed.  `DeploymentPolicy.code_execution` is `off` / `sandboxed` / `process_only`; the old
`local_code_execution` switch keeps working in both directions.

**Artifacts are content-addressed** (`artifacts/store.py`): bytes are written once, read-only,
under `<evidence_root>/artifacts/sha256/`, and every reader goes through one entry that
re-checks the hash and never follows a symlink.  They outlive the workspace.  Model-written
code runs in a **throw-away copy**, so what it writes never reaches the Attempt's own tree,
and the **verification copy is rebuilt from the recorded bytes** — what is verified is what
was recorded and what an approval binds.  Every copy the system makes refuses a symlink
(`workspace_symlink`) instead of dereferencing it.  Workspaces are **registered** (schema v7):
a rebind compares the registered identity, never the directory's content, and finished
Missions' directories are cleaned after a retention period while the registry rows and the
bytes stay.

**Real delivery**: `FilePublishConnector` publishes one verified Artifact into a directory the
user authorised.  The commit point is a single `os.link` — it fails if the name is taken, so
nothing is overwritten — and the connector writes its intent to its own ledger before it, so
every crash is decidable.  A connector now declares `lookup_authority`; an action at L2 or
above may only run on an authoritative one, and "I found nothing" from a best-effort lookup
leaves the action UNKNOWN for a person instead of being retried.  **Compensation** is a new
business action (`<action>#comp-<n>`) with its own approval and idempotency key — the original
fact stays as recorded — and `MissionControlV1.propose_compensation` is the way in.  A
candidate names *which* Artifact to publish; the system binds its identity.

Recalled Journal text no longer reaches a model as a SYSTEM instruction: it arrives as a USER
message inside an untrusted-history frame it cannot close early.

Correction to the 0.9.11 notes below: the Task budget floor is a *necessary condition at
reservation time* — it assumes one turn settles within its reservation — not a promise that a
first Attempt and its Critic will both fit (review P2-2).

## 0.9.11 — agent_orchestrator 0.9.4: P3.1 follow-up fixes (Phase3)

`agent_orchestrator` 0.9.4 (same wheel).  Fixes found by the Host's native acceptance
(plans/2026-09-12-phase3/p31-fixes).  **Task budget floor** (F-ORCH-1): the Graph Manager
refuses a Task whose effective token budget cannot carry a first Attempt and its Critic —
`k × (base + critic)` with k candidates per Task from the Mission's bound policy, the critic
part only when the policy names critic_review, `base` the largest
`default_max_output_tokens` of the config and every profile (`OrchestratorConfig.
min_task_tokens`: None = derived, 0 = off).  Initial graphs and graph changes alike;
refused with `task_budget_below_floor`, the Planner and the Manager are told the floor in
their input packages and why the proposal failed — nothing raises a model's number
silently; system tasks are exempt; the floor is a necessary condition, not a promise that
repairs will be affordable.  Only the Orchestrator injects the floor — a bare
`CommitService`, `validate_graph` or `validate_change` behaves as before.  **Artifact
verification status** (F-ORCH-3): an accepted result's artifacts become VERIFIED and a
failed result's REJECTED, each in its commit transaction
(`Store.update_artifact_verification`); superseded candidates stay UNVERIFIED.  An
Attempt's RETRY_WAIT after its Mission ended is by design (§25.2: a failed Attempt's
terminal state) — documented, unchanged.  (0.9.8–0.9.10 were the Host-support slices: the
local code execution switch, the P3.1 external control facade and their review fixes;
recorded in plans/2026-09-11-agent-orchestrator/host-support-0.9.8/.)

## 0.9.7 — agent_orchestrator step 9: learning from history and controlled promotion (source candidate)

`agent_orchestrator` 0.9.0 (same wheel).  Step 9 of ORCH-BUILD-v1.0 — the last step of the
original design's stage four (§28: "收集 Trace → 离线训练或规则改进 → 生成新策略版本 →
Offline Evaluation → A/B Test → 审批后上线"; "不要让在线 Agent 直接自我修改核心安全和调度
规则").  **Policy registry** (orchestrator schema v6): a policy is the promotable layer
over the deployment configuration — the §29.3 allocator weights, candidates per task,
exploration slots, a per-Mission concurrency under the deployment cap, the Manager
thresholds, the aging window, routing overrides and each role's prompt version — always
stored resolved and content-addressed; safety boundaries, budgets, the deployment
policy, ablations, timeouts and layer switches can never be part of one.  Proposals
carry their provenance; evaluation → human approval (nonce, receipt) → promotion →
rollback follow a closed state table, with a bounded step, a cooldown
(`DeploymentPolicy.policy_cooldown_seconds`) and no widening under backpressure; a
rollback is immediate and returns only to a previously ACTIVE version.  **Binding**:
every Mission is bound to one version in its creation transaction (`MissionCreated`
carries it, Replay projects it) and runs under it to the end — allocation, routing,
Manager thresholds and prompt templates read the bound version, never a later promotion
or another configuration; a production library seeds the resolved built-in policy,
records configuration drift and interpreter drift, and evaluation libraries take pinned
policies only.  **Rule improver** (`governance/learning.py`, `rules-v1`, a heuristic —
never called a trained model): reads history read-only, refuses untrustworthy
(replay / attribution), mixed or thin history and registers nothing then; R1 moves
allocator weights on contested allocations, R2 routes a failing task kind to its
escalation target; reputation per role × prompt × profile is evidence only.  **Gates**
(`governance/gates.py`): candidate vs ACTIVE on held-out cases in new evaluation
libraries, the step-8 statistics (per case and side, harness errors → INSUFFICIENT,
clearly-worse cost only), task-identity leakage refusal; PASSED means non-inferior
within the samples.  Online Agents cannot set policy: a Worker's `policy/` file or a
Manager's configuration operation is refused on record.  `PolicyApi`, CLI `policy
propose / evaluate / approve / reject / promote / rollback / list / show / status`, `demo
--scenario policy-promotion`.  No SDK (`simple_harness`) API change.

## 0.9.6 — agent_orchestrator step 8: contribution attribution, replay and policy evaluation (source candidate)

`agent_orchestrator` 0.8.0 (same wheel).  Step 8 of ORCH-BUILD-v1.0 — explaining one run
and comparing strategies on evidence (original §23, §28 stage four).  **Replay**
(`observability/replay.py`) rebuilds facts that already happened: a pure fold of a
Mission's events (ordered by seq, deduplicated by event id) into the formal state —
Mission, Tasks, Attempts, Results, knowledge, conflicts, actions, approvals, overrides —
compared field by field with the library, which is only ever read through a copy opened
read-only (`Store.open_readonly`); for this build's libraries the coverage is 100 %,
missing or older events are reported as `not_covered` with structural gap rules and the
missing event ids, never back-filled; a failure timeline tells what went wrong in order.
New events `ActionSuperseded` / `ActionCancelled` put every action state change on
record; events are read page by page.  **Attribution** (`observability/traces.py`)
follows the final products (the integrated tree) to the Tasks, Attempts, Agents, roles,
models and prompt versions that produced them and the layers that passed them, adds the
knowledge path, actions and people, lists everything else as exploration with its
reason, and splits the imported usage row by row (Attempt, its Critic, planner /
manager / judge) with an unclassified bucket that must stay empty; unpriced money is
null.  **Policy snapshot** (`governance/policies.py`): every configuration field
classified, every version constant with its source, role templates, profiles, routing,
connectors and provider identity without credentials; `snapshot_diff` names each
difference's source; evidence adds `attribution.json` and `policy_snapshot.json`.
**Ablation** is an explicit change of the effective policy (`OrchestratorConfig.ablations`:
critic, blackboard, graph_changes; safety boundaries refused): an ablated layer is
recorded `NOT_REQUIRED` with `ablated=true`, never a silent PASS.  **Evaluation**
(`observability/evaluation.py`): cases × strategies × trials, every run a new Mission in
its own new directory and library, whitelisted strategy overrides, test services only,
harness errors kept out of the denominator, success rates with Wilson intervals,
comparisons by Fisher's exact test and non-overlapping ranges, hidden oracles for
verification misjudgment, fixture results marked as a mechanism check, cases derived
from old evidence only when the charter hashes to the recorded spec.  CLI `replay` and
`evaluate`, `demo --scenario evaluate-policies`.  Code review round 1: an evaluation
refuses any service but the local test service on every case and again on what each run
is handed (`EvaluationRefused`, never a harness error); an ablation that leaves a Task no
verification layer is an `ERROR`, never a zero-layer PASS; a missing outcome event is a
structural gap and its field becomes `not_covered`; attribution flags refuted claims on
the path (`claim_refuted`) and `reconciled` also checks the budget ledger; comparisons
are paired by case and say "insufficient evidence" on opposite directions or uneven
harness errors; `replay` / `evaluate` answer bad calls with exit 2.  No SDK
(`simple_harness`) API change.

## 0.9.5 — agent_orchestrator step 7: human-in-the-loop and controlled real actions (source candidate)

`agent_orchestrator` 0.7.0 (same wheel).  Step 7 of ORCH-BUILD-v1.0 — the "scale and
safety" stage of the original design (§14.4, §15, §21–22, §24).  An Agent never performs
a real change: it writes an action candidate (`actions/<name>.json`: connector, operation,
target, params, reason) declared in its Task's outputs.  Verification always checks a
candidate (schema, deployment policy, the Mission's `action:<connector>.<operation>:<target>`
scope, declared outputs) and the accept transaction re-reads the stored bytes and
registers it in an action ledger (`actions`, schema v5) under a stable business action id;
changed content is a new version that supersedes only an *open* one — a handed-off or
executed version is never superseded (`action_in_flight` / `action_already_executed`).
Risk levels follow original §22 (L0/L1 automatic, L2 one approval, L3 two; connectors
declare levels, deployments may only raise them; connectors are off unless a deployment
enables one, and L2+ needs idempotency and reconciliation).  Approvals (`approvals`,
`approval_decisions`) bind mission, task, action id, version, params hash and artifact
hash; decisions come only from a caller's `Principal`, carry a nonce and a receipt hash,
are counted once per receipt (and once per principal for L3 by default — a deployment
convention), and can be rejected, revoked, expire or be cancelled with the Mission.  The
Action Executor (`runtime/actions.py`) hands off only as the last step of the Mission
judgment: `begin_handoff` re-checks the binding, reserves `action:<key>` (one tool call)
and writes HANDED_OFF with owner, lease and decision receipts in one transaction before
the connector is called in a thread under a timeout; a mismatching or lost receipt is
UNKNOWN (reservation held), reconciled by idempotency key (COMPLETED → SUCCEEDED,
CONFIRMED_NOT_STARTED → one re-hand-off with the same key, otherwise a person rules with
evidence); an ended Mission is still reconciled.  The judgment runs in two stages (non-action
criteria booked once per integrated tree; waiting for a person is no progress, so `run()`
goes idle; the runtime cap excludes human waiting).  Human review is deployed as the sixth
verification layer: a result waits SUSPENDED for a person, resumes reusing the layers that
passed, and a Critic may answer `needs_human` (never short-circuiting the code tests, one
escalation per Task).  Verifier conflicts go to a person as arbitration (a Conflict Task
out of attempts; a judge Critic disagreeing with the Task Critics).  Takeover (stop /
retry with a note), comments and rulings are `HumanOverride` / `HumanCommentAdded` events
with their basis and never widen scope.  `api/approvals.py`, CLI `approval
list|approve|reject|revoke|comment|review|arbitrate|takeover|resolve --as`, `demo
--scenario approval-action` (local test configuration service; `--pause-for-approval`),
evidence `actions.json` / `approvals.json` and trace / metrics sections.  A dedicated test
service passing grants nothing for production.  No SDK (`simple_harness`) API change.

## 0.9.4 — agent_orchestrator step 6: many Missions, many models, backpressure, isolation (source candidate)

`agent_orchestrator` 0.6.0 (same wheel).  Step 6 of ORCH-BUILD-v1.0 — controlled
concurrency.  Missions share one orchestrator under an optional Global Budget
(`budget:global` above every Mission account, §18.2; unnamed dimensions inherited) and
two new budget dimensions, tool calls (reserved per Attempt, settled on the gateway's
count, capped at the gateway) and wall-clock runtime; a pool that cannot fund planning
stops the Mission visibly.  `scheduling/backpressure.py` registers §18.5's six caps and
raises / clears backpressure on high / low watermarks (hysteresis) — state and the
`BackpressureRaised` / `BackpressureCleared` events are written in one transaction; while
raised the Allocator halves Worker concurrency, expands only conflict and starving Tasks
plus one exploration slot, shrinks reservations and refuses `add_task` from the Manager.
Verification runs in a bounded set of tasks (`verifier_workers`).  `runtime/model_router.py`
maps runtime profiles (provider, model, price, output caps) to one `AgentRuntime` and one
execution library each; the route is frozen into the dispatch intent (`ModelRouted`) and
proven by the provider echo; failures climb the §9.3 ladder to a stronger profile with the
earlier Attempt kept; an unavailable profile falls back or makes the Task wait a bounded
time (`runtime_unavailable`); a restart only resumes an Attempt in its own pool.  Tool
permissions are Mission ∩ Task ∩ Role ∩ Deployment; the Tool Gateway checks in §21.1
order (identity, permission, schema, policy, rate) and every refusal is a
`ToolCallRejected` event; upstream inputs are read-only in a downstream workspace;
`Artifact.workspace` records the producing workspace.  An undeployed verification layer
is refused at commit (`verification_policy_undeployed`) and blocks at run time
(`verifier_unavailable`).  Evidence adds `trace.json` (per-Attempt versions: profile,
requested and echoed model, prompt, context, retrieval, allocator, router, verifier),
`metrics.json` and `scheduler.json`, and is scanned for credentials before it is written.
CLI `demo --scenario multi-mission`.  Schema v4 (`scheduler_state`, tool-call columns).
No SDK (`simple_harness`) API change.

## 0.9.3 — agent_orchestrator step 5: the Task DAG changes on execution evidence (source candidate)

`agent_orchestrator` 0.5.0 (same wheel).  Step 5 of ORCH-BUILD-v1.0 — the dynamic Task
Graph (§6.2) without touching the §25 state machines.  A non-candidate Result Envelope
(blocked / failure / no_progress / proposed_subtasks) is kept as history and never
verified; it opens exactly one deduplicated management decision per trigger.  The Manager is
a BaseAgent role (`manager-v1`) that sees the trigger, the Verifier's feedback, the
affected subgraph, the graph version and the hard limits, and answers with a
`<graph_change_proposal>` from a system-defined operation vocabulary (add_task,
supersede_task, retarget_dependencies, set_priority, pause/resume_task, cancel_task,
set_role).  `commit_graph_change` validates the whole proposal against the merged graph
(cycles, depth, task count, proposals per source Attempt, the Mission budget pool counting
settled and in-flight tokens of superseded work, duplicates, sibling outputs, goal drift,
§25.1 legality) and applies it in one transaction with a graph_version CAS (disjoint stale
proposals are rebased, overlapping ones refused), an idempotent receipt and a
`graph_changes` ledger (schema v3): executing Tasks are superseded by a new entity
(ACTIVE→CANCELLED, late candidates history only), BLOCKED dependents are rewired in place,
completed Tasks are only referenced.  Artifact lineage now orders ancestors topologically
from the edges.  Repeated no-progress must end in a change of approach (§29.2 worker
variants explorer / exploiter / simplifier / connector / failure_analyst) or an explicit
stop (`no_progress`, `management_exhausted`); a refused proposal is fed back once.  The
Allocator ranks the frontier with §29.3's starting formula (weights verbatim, versioned
input scales, conflict Tasks first, starvation guard after an aging window) and freezes the
score on the Attempt.  Evidence adds `graph_history.json` (v1 → v2 with basis and old work).
CLI `demo --scenario dynamic-dag`; kill switch `dynamic_graph`.  Review round 1: `pause_task`
is legal only for READY/BLOCKED Tasks; a Manager decision is also requested after a PASS that
still carries `proposed_tasks` and after `manager_after_failures` verification failures; the
manager intent settles only once the change is durable; `AllocationDecided` carries the frozen
score; an `add_task` without a budget gets a bounded share.  No SDK (`simple_harness`) API
change.

## 0.9.2 — agent_orchestrator step 4: team knowledge, conflict arbitration, synthesis (source candidate)

`agent_orchestrator` 0.4.0 (same wheel).  Step 4 of ORCH-BUILD-v1.0 — the Blackboard (§11)
in four layers.  Claims stay proposals with typed fields (`key`, `stance`, per-claim
`evidence`, `supersedes`, `contradicts`; `status` may only be PROPOSED) and are graded by the
system inside the accept transaction from the verification that actually ran: VERIFIED only
when a cited `pytest:` target was run and passed by `code_test` (a whole-tree run covers no
claim), SUPPORTED with trusted evidence, otherwise unsupported (untrusted external sources
count for nothing).  Only VERIFIED claims are projected into a separately stored Verified
Knowledge table with full provenance (source Task/Attempt/Result/Agent, evidence, verifier,
dependencies, used_by, supersedes/superseded_by, disputed_by/confirmed_by/resolves).
`used_knowledge` is a checked reference (VERIFIED, same Mission, not SUPERSEDED — re-checked
inside the Commit) that records the reuse chain (`KnowledgeUsed`); supersession is explicit
and legal only VERIFIED→SUPERSEDED.  Contradictions (explicit `contradicts` or same key with
opposite stance) are detected before grading: contested claims are capped at DISPUTED and
never projected, VERIFIED knowledge is only marked `disputed_by`, and a system-defined
Conflict Task is opened as a trailing leaf (funded from an explicit `conflict_reserve_tokens`
or deferred with `ConflictOpenDeferred`) whose Arbiter must resolve by an external check
reviewed by an independent Critic — never by a count.  The fixed synthesis Task
(`MissionSpec.synthesis`) depends on every Planner leaf, is budgeted in the §18.2 sum, is gated
by open conflicts (`SynthesisGated`, no rewiring) and must pass verification again.  Retrieval
is deterministic (relevance / trust / DAG distance / recency / reuse, deduplicated, superseded
marked, Mission-bounded; `retrieval-v1`), the Context Builder (`context-builder-v3`) carries
all eleven §10 items under worker / verifier / critic / arbiter / synthesizer visibility
templates, summaries are deterministic compressions that never change a claim's status, a
failed retrieval degrades or blocks explicitly (`RetrievalUnavailable`, stop reason
`retrieval_unavailable`), external content under `untrusted_sources` is marked data at the tool
gateway, and the final report carries the result's lineage.  Orchestrator schema v2 (in-place
upgrade with backup; artifact versions unique per (mission, path) — step-3 L3-2).  CLI
`demo --scenario knowledge-sharing`; kill switch `knowledge_sharing`.  No SDK
(`simple_harness`) API change.

## 0.9.1 — agent_orchestrator step 3: Planner-decomposed static DAG executed in parallel (source candidate)

`agent_orchestrator` 0.3.0 (same wheel).  Step 3 of ORCH-BUILD-v1.0: the Planner proposes a
whole Task graph (`<task_graph_proposal>`, budgets normalised then checked as a whole —
cycles, missing/self dependencies, duplicates, sum of task budgets within the Mission,
tools, shape, independent siblings declaring the same output path) and the Commit Service
applies it atomically with a replayable receipt (roots READY, the rest BLOCKED; a rejected
graph writes only `TaskGraphRejected` and is fed back to the next Planner proposal).  The
control loop runs the Frontier through a bounded Allocator (`max_concurrency`,
`candidates_per_task`): parallel Attempts on independent Tasks, a downstream Attempt seeded
with every ancestor's accepted artifacts (frozen as inputs in the dispatch intent, protected
against rewrite unless the Task declared the path in `outputs`; independent branches that
disagree on a path are an `artifact_conflict`, never a silent pick), artifact versions per
(mission, path) lineage.  Accepting a result, superseding the losing candidates (their
results kept as history) and unblocking the dependents happen in one transaction; a stop
cascades to READY/ACTIVE/VERIFYING Tasks while BLOCKED Tasks end with the Mission (§25.1 has
no BLOCKED→CANCELLED edge); Mission-pool exhaustion blames no Task.  Two orchestrator
instances share the libraries (one `owner_scope`, one `owner_id` per instance, orchestration
lease ≥ 2× the SDK Run lease): a lapsed lease is taken over on the same Attempt and the
same SDK turn, a vanished executor is LOST and retried, `recover()` heals the frontier and
orphan candidates.  The Mission is judged on the integrated tree of every Task's accepted
artifacts (pytest / file / independent Critic).  CLI `demo --scenario static-dag`.  No SDK
(`simple_harness`) API change.

## 0.9.0 — agent_orchestrator step 2: the reliable single-Task Mission closure (source candidate)

New package `agent_orchestrator` shipped in the same wheel (modular monolith after the
design's §27; `simple_harness` root `__all__` unchanged).  Step 2 of ORCH-BUILD-v1.0:
Mission → Planner (one BaseAgent proposing exactly one Task Contract) → Commit →
Reserve → Attempt → Worker BaseAgent in an isolated workspace with four confined tools
(`workspace_read_file/write_file/list`, `run_tests` as a killable child pytest) →
Result Envelope (§13/§26.4, strictly parsed, identity-checked, system-derived id) →
Verifier Router (§14.1 order: format → rule → independent Critic BaseAgent → real code
test; NOT_REQUIRED never counts as PASS) → repair Attempts with feedback or a visible
stop (`max_attempts_reached` / `budget_exhausted`) → Task COMPLETED → independent
Mission-level judgment of `success_criteria` (runs its own Critic when a criterion
needs one) → MissionCompleted / MissionFailed.  Orchestration state lives in its own
`orchestrator.db` written only by the Commit Service (§15/§17.5): idempotent Mission
creation, receipts for proposals, CAS on every entity version, idempotent Events
(§16.2 names), dispatch intents freezing the full AgentConfig + input Message so a
crash at any of six cross-database instants replays the very same Agent/Turn
(S2-04/05), leases/heartbeats from real executor liveness (an UNKNOWN provider outcome
keeps the Attempt blocked and its reservation held, S2-08), two-layer budgets
(allocation Reserve/Settle vs. SDK invocation facts imported once each; `unpriced` is
never written as zero).  CLI `python -m agent_orchestrator` (mission / attempt /
artifact / demo with an evidence directory).  SDK: one read-only facade
`SqliteExecutionUnitOfWork.list_provider_invocations(run_id)`.

## 0.8.0 — BaseAgent: durable Agents, bounded Context, session recall (source candidate)

New public surface under `simple_harness.agents` (`build_agent_runtime`, `AgentRuntime`,
`BaseAgent`, `AgentConfig`, contracts) built on the existing Run kernel: a BaseAgent
Run never reaches a terminal state; every input is one durable AgentTurn with a
staged-then-committed result; `agent_delegate` creates one child Agent per turn;
`close` / `cancel_turn` are control-plane intents, never kernel cancels; idempotent
`create_many` batches; per-turn limits from durable baselines; a Journal-backed bounded
working Context (`ContextPolicy`, `TokenizerPort`) with structural summaries and
exact read-back; Agent-scoped hybrid recall (FTS5 trigram + words, injected
`EmbeddingPort`, RRF) with explicit degradations; `session_history_search/read`.
Execution schema v10 (fresh descriptor; v7/v8/v9 descriptors frozen) with the explicit
backup-first `migrate_execution_to_v10` (after `migrate_execution_to_v9`). Root
`__all__` adds only `migrate_execution_to_v10` and `ExecutionBaseAgentUpgradeReceiptV1`;
legacy Runs, `react_loop.py` policy semantics and the H079/H0710 artifacts are unchanged
(the ReAct loop gained an optional companion write in its final checkpoint CAS).
A driver exception inside an admitted AgentTurn is a visible failed turn
(`base_agent_driver_exception`); the upgrade receipt binds the retained backup to the
source image (`source_root_hash`); `agent_delegate` hands its tool permit back while it
waits for the child so `max_concurrent_tool_calls` cannot deadlock a delegation.
`migrate_execution_to_v9` now upgrades a v7/v8 library written before the explicit
audit schema existed (it bootstraps the audit objects under the write lock, after the
backup is retained) and is a no-op on a library already at v10, so a Host may call the
v9 and v10 upgraders unconditionally at startup.

## 0.7.10 — bounded nullable Tool schemas (source candidate)

Tool schema validation accepts exactly one existing non-null type paired with
null, in either order. Required, enum/const, resource bounds and non-null branch
constraints are preserved; the root remains a single object. No general unions,
combinators, input normalization or execution schema changes. Host owns the
unused-value interpretation. H079 artifacts remain unchanged; main owns the
single successor build and installed validation.

## 0.7.8 — concrete start-mode driver selection (candidate)

Add SDK-owned StartModeDriverRouter: select the actual ordinary or Host-control
driver from the validated durable start mode before invocation and recording.
Only the exact selected SDK implementation receives its existing recording
contract; custom routers/subclasses remain unverified. Host-control authority
validation is unchanged. Execution schema9 and all H077 artifacts are unchanged.

## 0.7.7 — authorization terminal proof and bounded expiry recovery

Root React tool-authorization expiry/denial now commits exact terminal evidence.
Explicit public eligibility/recovery supports proved legacy first-expiry roots;
exact public terminal metadata replaces Host SQL. Original decisions and receipt
bytes are preserved; unknown legacy states are rejected. Execution schema9 is
unchanged. H075/H076 artifacts remain frozen.

<!--
SPDX-FileCopyrightText: 2026 DennyWanye
SPDX-License-Identifier: Apache-2.0
-->

# Changelog

## 0.7.6 — 2026-09-06 (isolated candidate)

- Permit TIME_DUE from actual RESCHEDULED to TRIGGERED; retain exact authority/ref and time checks. No event/recurring or wire/schema changes.

## 0.7.4 — 2026-09-06 (isolated candidate)

- Bind real Memory use receipts to immutable Provider request/time, atomic claim
  and consumed handoff; exact replay and confirmed-not-started fresh grants.
- Add typed Context sidecars, payload-free use views, admission scope pins and
  terminal/recovery checks without synthesizing task/no-recall route receipts.
- Explicit backup-first execution schema7→8 migration; old binaries refuse8.
  Host migration/wiring is separate; no user data is implicitly upgraded.

## 0.7.3 — local successor candidate

### Added
- SDK-owned operation-audit producers and metadata-only public Run, command and pre-Run
  stage snapshots with stable page continuation. Actual Provider/tool/effect, pre-effect
  rejection, core runtime, command, delivery and Memory-port boundaries retain unknown
  outcomes and exact canonical associations. Existing child/workflow receipts are reused.
- Explicit observational audit schema2, separate from execution7; exact audit1 is extended
  atomically on write-open and read-only never migrates. Legacy writer gaps remain visible.
- Exact ordinary/root terminal evidence exposes separate payload and row hashes; pages
  revalidate actual terminal state/unique source through indexed reads. Host must retain
  exact Run/event/state/payload comparison, including SDK-owned terminal outbox metadata.

### Limits
- Current-source completeness is not all-operations recording or consumer audit completion.
  Legacy absent witnesses and unleased calls without state changes are unreconstructable;
  mutable canonical heads are not invented immutable all-transition histories. Host,
  Memory and Service operation producers/optimization findings are separate domains.
- Frozen0.7.2 and original failed candidates are unchanged. No public release/push/tag is
  implied. Independent installed public consumer and actual Host terminal composition are
  distinct gates; see plans/2026-09-05-run-operation-audit/ for evidence and scope.

## 0.7.2 — candidate

### Fixed
- Resume after a successful Context route change now checks the immutable initial checkpoint
  against the start route, while preserving the current route and reserved work. Previously a
  later authorization resume could fail because the current route differed from the initial one.
- Recovery validates checkpoint payload hashes and Run identity for both initial and current
  records. Missing or conflicting initial anchors remain rejected.

### Compatibility
- StartSnapshot v7 and checkpoint v6 wire formats are unchanged; legacy no-initial checkpoints
  remain readable. `ReactCheckpointPort` implementations must provide
  `read_initial_react_checkpoint(run_id)` returning the immutable version-zero checkpoint.
  The SQLite implementation reads existing append-only records without migration.
- This is a local candidate under the approved S5b route-recovery exception, not a release.

## 0.7.1 — candidate

### Added
- Adds `ContextRouteReceipt` v3 with origin-specific `context_tool` and `host_initial`
  provenance. Host-initial routes require exact TaskScope/binding receipts and Host authority while
  forbidding fabricated tool call/effect identities.
- Adds ordinary `StartSnapshot` schema v7 and ReAct checkpoint schema v6 so an initial Host route
  is frozen before the first Provider turn and recovered with conflict detection.

### Compatibility
- Start snapshot schemas 1–6 still decode without an initial route; Host-control snapshots remain
  schema v6. Context route receipt v1/v2 decoding is unchanged.

## 0.7.0 — candidate

### Breaking
- Replaces unconditional pre-Provider Memory recall with an explicit, same-Run Context route
  barrier. Host-issued Context snapshots become the sole Provider request authority.
- Adds versioned Human Memory, TaskScope, evidence, disclosure, route receipt, and
  per-effect Task execution authority contracts.
- Adds strict typed workspace-root identity, Manual authorization challenge/decision, Host-issued
  Auto mode snapshot, Host-verified grant, and append-only binding-set receipt contracts.
- Main-model analysis executors now return a strict result envelope with a separately verifiable
  Host-durable delivery receipt; Memory validation/application receipts retain their distinct role.
- Replaces free-form cognitive mutation/recall drafts with schema-v2 typed Episode, Semantic,
  Procedure, and Prospective payloads, exact Evidence spans, revisioned targets, canonical DAGs,
  and Host-bound Recall selectors.
- Upgrades cognitive mutation to strict schema v5 with explicit Semantic `claim | relation` payloads;
  V1 exposes only `applies_to`, exact existing/same-plan-created endpoints and a closed type matrix.
- Replaces Recall Decision schema v2 with the strict v3 wire. `RECALL` contains selected memories
  only; `NEEDS_USER_CONFIRMATION` contains typed conflict-group candidates only. There is no legacy
  Recall Decision decoder during the prototype phase.
- Moves `PrivacyClass` and `InformationAttribute` to one dependency-free classification protocol,
  while preserving the existing Memory imports as re-exports. Evidence item authority is now a
  Host-only schema-v3 record, versioned by public `EVIDENCE_ITEM_AUTHORITY_SCHEMA_VERSION`, with mandatory privacy, information attributes, and classification
  authority; no v2 decoder or optional classification default exists.

### Safety
- Route-required effects cannot cross the route barrier in the same Provider tool batch.
- Durable Provider and effect replay bind exact Context, capability, root, and binding-set
  revisions while excluding raw hidden reasoning and transport credentials.
- Context snapshot revisions and snapshot ID/payload bindings are monotonic across Provider turns.
  Public response recovery rejects extra fields, private metadata, unsupported content blocks,
  and non-integral schema values before replay.
- Generic Tool authorization receipts and model metadata are not workspace authority. Project
  route receipts and per-effect envelopes bind the exact binding-set receipt identity/hash in
  addition to its frozen revision and root identity.
- Binding-set receipts commit the sorted unique root identity set: genesis uses the canonical
  empty-set parent digest and every later receipt verifies exact parent-set union one new grant
  root. Context route schema v2 carries this authority; legacy v1 decoding is standalone-only.
- Self-consistent analysis delivery DTOs are not provider-result authority: consumers must verify
  the exact Host durable delivery record, including issuer, request/result, attempt, and response.
- Typed observations cannot replay across admitted evidence; conversation Tool causal parents must
  precede the current item in the authenticated causal group.
- Span verification requires the exact Host `AdmittedEvidenceAuthority` type, validates the v3 item
  authority hash/bindings, resolves admitted evidence once, and returns that verified item authority
  so downstream classification joins reuse the same proof.
- Typed observations accept only Tool/Trusted Tool or External/External Source provenance and always
  require the exact resolved receipt. Mutation DTO validation binds epistemic labels to evidence:
  in particular, `VERIFIED_EXTERNAL` requires an external typed observation and source-verified
  state, while user/model/Context evidence cannot self-grant external verification.
- Recall rejects expired Context, selector removal, evidence-lineage drift, and unknown, external,
  or untrusted disclosure. Conflicting candidates pass the same disclosure gate, require at least
  two unique candidates per group, and cannot overlap selected results. Outcome-specific reason
  codes prevent rejected/no-recall decisions from impersonating positive dependencies; both deny
  outcomes expose a zero candidate count. The unreferenced private v1 Recall Decision decoder is
  removed. Mutation plans require authority-resolved `strict_atomic` apply receipts covering every
  canonical operation and one base-to-committed revision transition. Semantic relation plans reject
  missing/forward dependencies, non-CREATE producers, relation endpoints, illegal types, self loops
  and malformed wire before they can create partial Memory state.

### Compatibility
- Terminal committed-turn outbox delivery remains available for the staged Memory SDK cutover;
  automatic pre-Provider recall and recall-release calls are no longer part of the production loop.

## 0.6.4 — 2026-08-29

### Fixed
- Reissuing a user-confirmation nonce now deeply thaws already-frozen authorization metadata
  before constructing the replacement request. Nested lists and objects therefore survive the
  durable confirmation boundary instead of failing strict JSON validation.

### Compatibility
- Public APIs and execution schema v6 are unchanged. This is a patch-only contract repair.

## 0.6.2 — 2026-08-25

### Fixed
- Runtime capability search now adds a conservative six-character prefix for long lowercase
  English words, so common morphology differences such as `translation` versus `translate` do
  not hide an otherwise exact Skill or Tool match. Short words, identifiers, and non-Latin text
  retain exact matching.
- Tool handler failures now emit a privacy-safe SDK diagnostic containing only the Tool name,
  exception type, and an allowlisted stable code. Exception messages, response bodies, paths,
  and credential-bearing values remain outside both logs and model-facing results.

## 0.6.1 — 2026-08-25

### Fixed
- `build_react_driver` now accepts a Run-local Tool exposure resolver and passes the resolved port
  into every `ReActRunInput`. This closes the public composition seam required by a Host to use
  same-Run progressive exposure; the rejected 0.6.0 prepublish artifact was never released.

## 0.6.0 — 2026-08-25

### Added
- Provider-neutral Runtime capability records for executable Tools, Skill resources, and Workflow
  profiles, with bounded search/describe, nonce-bound activation receipts, and executable-only
  Provider projection.
- Run-local Tool exposure checkpoints. Each new ReAct `ready` attempt reprojects direct plus
  activated Tools, while a `provider_reserved` request replays its exact frozen schema snapshot.
- Fresh execution schema v6 and an explicit backup-first v5-to-v6 migrator. The legacy
  ProviderToolSpec fingerprint remains separate from the complete v6 catalog envelope digest.

### Safety and compatibility
- Catalog visibility never invokes a handler or grants authorization. Target execution still goes
  through ToolRegistry, authorization, EffectExecutor, and the durable effect ledger.
- Terminal activation receipts deterministically restore visibility after restart; forged,
  cross-Run, stale, out-of-order, missing, changed, or extra handler identities fail closed.
- Normal loading still rejects legacy schemas. Operators must close the Runtime and explicitly run
  `migrate_execution_v5_to_v6` with a caller-selected adjacent backup path.

## 0.5.2 — 2026-08-24

### Fixed
- Durable start commands now deeply thaw their frozen JSON input before constructing `RunStart`.
  Nested capability snapshots and message metadata therefore retain their canonical JSON shape
  instead of being rejected after Memory Context staging.

### Compatibility
- Public APIs, command schema version, and fresh execution schema v5 are unchanged. There is no
  migration or legacy-data compatibility path.
- Released from source commit `136b3539b938516156a9b336f38c6b4404d3adb8`; the frozen wheel
  SHA-256 is `09ba6041a0220cdd952f50e6ec8defcf1d8bbe60d9a044d9d7fc9106985639de`.

## 0.5.1 — 2026-08-24

### Fixed
- Durable start commands now carry a closed Tool catalog fingerprint through the public intent,
  execution database, `RunStart`, and `StartSnapshot`; restart resolves the exact catalog snapshot
  and fails closed before Driver/Provider handoff when that authority is unavailable or drifted.

### Compatibility
- Fresh execution schema v5 and the public command schema version remain unchanged. There is no
  migration or legacy-data compatibility path.
- Released from source commit `441709e518aae041829e64796cabe13ab265abbf`; the frozen wheel
  SHA-256 is `1298aa7d44f748ebf39265920d648f40c130bf014452dce3c6ae049aa5f6590c`.

## 0.5.0 — 2026-08-24

### Added
- A closed durable command API: `submit_start`, `submit_continue`, `submit_cancel`, and
  `get_command`, with typed receipts, snapshots, and stable errors.
- Fresh execution schema v5, which commits command acceptance before Memory preparation,
  Provider, Tool, or delivery work begins.
- Durable FIFO acceptance sequencing, replay/conflict detection, cancel fencing, lease recovery,
  and terminal command-output projection for restart-safe product integration.
- Closed command output convergence: running is `PENDING`, valid completed output is `PRESENT`,
  failed/cancelled is `ABSENT`, and missing, corrupt, or conflicting completed output is
  fail-closed `UNKNOWN`.

### Fixed
- An expired command owner can no longer kill the sole command pump by attempting to settle a
  claim already converged by its takeover owner; each command failure is isolated.
- Legacy starts perform readiness and complete typed preflight validation before their permanent
  run-mode reservation, while still reserving before any external call.
- Root and continuation output writes now expose explicit before/after fault cuts inside the same
  terminal transaction.
- A completed command can project only the `conversation_outputs` row that names that exact
  command; earlier commands in the same Run remain `ABSENT` after a later continuation owns the
  final output.

### Compatibility
- Agent Memory v1, observability v1, and the legacy RunClient surface remain compatible.
- Normal open of schema v4 fails closed without writing; 0.5.0 creates fresh schema v5 only and
  provides no v4-to-v5 migration.
- Released from source commit `ac2e2add7e6f5efb5d4dd7b26fb138f9d750d334` after the Memory
  SDK 0.5.1 exact-wheel compatibility matrix passed (receipt commit `0b25ac54`).

## 0.3.0 — candidate

### Breaking
- Execution persistence now uses fresh schema v4. The normal loader rejects schema v1-v3;
  schema v3 requires the explicit backup-first offline migrator and a complete identity map.
- The public query/sink and reserved query/write Memory ports are retired. Consumers pass one
  `AgentMemoryPort` implementation, such as Memory SDK 0.4 `MemoryManager`, to the official
  consumer or production builder; no public Memory adapter or manual recall/write lifecycle
  remains.

### Added
- Official `AgentMemoryPort` v1 contract with trusted four-part identity, personal/family
  scopes, canonical recall/release/committed-turn DTOs, stable errors, failure policy, and
  explicit borrowed/runtime ownership.
- `ConversationContextProviderPort` for product-owned non-Memory Context and automatic
  `RunClient.start_conversation()` / continuation preparation.
- `ConversationContinuationInput.context_source_snapshot_ref` lets each continuation bind its
  own product Context snapshot. When omitted, the SDK derives a deterministic content-addressed
  reference from that continuation's current message.
- Fresh execution schema v4 with immutable Agent identity bindings, richer Context staging,
  durable recall-release retry, and a terminal-only canonical committed-turn outbox.
- Lease/epoch-fenced committed-turn dispatch with restart replay, bounded backlog cleanup,
  transient retry, permanent/conflict dead-letter, and privacy-safe `REJECTED_ERASED` settlement.
- Backup-first explicit execution v3-to-v4 offline migration with a digest-verified neutral
  manifest, deterministic four-way legacy event classification, target identity remapping,
  and a versioned cursor for post-migration continuation supersession.
- A packaged PEP 561 `py.typed` marker so strict type checking follows the public Agent Memory
  and execution migration contracts from an installed wheel.

### Changed
- `build_consumer_runtime` is the official easy composition root and accepts one Memory
  instance; recall failures degrade to a frozen empty partition and replay does not recall
  again.
- Old query/sink and reserved query/write ports are retired from public exports. Consumers
  migrate to one `AgentMemoryPort`; manual preparation helpers, adapter-facing Memory DTOs and
  `ContextPreparationMode` are private implementation details rather than compatibility exports.
- Conversation start and continuation enqueue no longer create tentative Memory writes. A
  completed root or continuation commits its user+assistant pair atomically with terminal facts;
  failed/cancelled turns produce no outbox row and replay rejects missing, added, or changed turns.
- Context preparation persists the effective root or continuation snapshot reference in the
  durable claim before invoking the product provider. Continuation replay reuses that exact
  reference; changing either the reference or payload for an existing continuation ID conflicts.
- A duplicate caller now waits through the bounded Context request/lease horizon. If the durable
  owner disappears, the waiter deterministically takes over the expired lease and freezes the same
  stage instead of failing after a fixed one-second polling window.
- Existing execution schema v1-v3 databases remain fail-closed in the normal loader. A closed,
  exact-v3 database can be upgraded only through the explicit offline migrator with a complete
  legacy identity map and a caller-selected same-directory backup path.

## 0.2.0 — candidate

**Focus:** Durable conversation Memory integration without replacing the 0.1.5
structured-message, tool-catalog, Provider-budget, or projection authorities.

### Added
- Typed conversation turn/continuation/output DTOs and bounded recall/apply Ports.
- Fresh execution schema v3 with immutable user/session ownership, durable private context
  staging, and a transactionally coupled conversation Memory outbox.
- Four atomic root/continuation commands, lease-based Memory dispatcher recovery, and
  SDK- or consumer-prepared context modes whose private snapshots replay byte-for-byte.
- Strict production composition that requires all authorities and owns projection, Memory,
  and SQLite lifecycle resources.

### Changed
- StartSnapshot schema is v5; schemas v1–v4 remain readable. New conversation fields are
  additive and generic runs remain supported when conversation Memory is disabled.
- Existing execution schema v1/v2 files now fail closed and require a fresh v3 storage set.
- CI builds one authoritative candidate and tests the exact wheel on Python 3.11–3.13.
  Release publication is manual and uploads the tested bytes without rebuilding.

## 0.1.5 — candidate

**Focus:** Durable per-Run context authority for SDK-first product integration.

### Added
- Typed structured message content (`ContentBlock` / `MessageContent`) with canonical
  persistence, StartSnapshot/ReAct recovery, and OpenAI-compatible serialization. Structured
  content is never coerced through `str(list)`.
- Nullable Provider usage dimensions for cached and reasoning tokens.
- Atomic per-Run Provider resolution via `ProviderBindingResolver`, binding the physical
  Provider, optional frozen estimator, budget policy, and restart-checkable fingerprint.
- Immutable, content-addressed tool-catalog generations persisted in SQLite and resolved by
  exact generation/fingerprint across WAITING and process restart.
- A transactionally coupled Provider settlement projection outbox with stable cursor reads.
- Optional `deskpet_public_progress` normalization; missing, blank, or wrong-type metadata is
  stripped without blocking business tool arguments.

### Changed
- SQLite schema version is 2. Existing 0.1.4 databases migrate in place.
- StartSnapshot schema version is 4 and remains backward-readable for schema versions 1–3.

### Integration boundary
- Product code remains responsible for enforcing the 8 MiB per-content-block and 16 MiB
  aggregate-per-Run ingress limits before constructing SDK messages.
- Catalog generations are retained indefinitely in 0.1.5; a future GC may delete them only
  after every referencing Run is terminal.

## 0.1.4 — candidate

**Focus:** Release-blocking hardening of the consumer facade and the release/CI pipeline.

### Fixed
- Delivery no longer fabricates `DELIVERED`: the no-op `_DefaultDeliverySink` was removed
  from the production namespace. `build_consumer_runtime` now accepts an optional
  `delivery_sinks` mapping; when omitted, no sink is registered and deliveries stay PENDING
  (fail-closed). `DeliveryDispatcher` now permits an empty sink set. A test-only
  `NoopDeliverySink` lives in `simple_harness.testing`.
- Tool calls now pass a real execution context (`run_id` / `request_id` / `call_id`) to
  `ToolExecutorPort.execute` instead of an empty dict.
- `build_consumer_runtime` is documented as a demo/basic facade; production consumers
  should assemble `RuntimePorts` directly (the facade uses a zero-cost price estimator and
  no-op reconciliation).
- The Database opened by `build_consumer_runtime` is now closed on Runtime shutdown via a
  `close_hook` (registered by the facade, not by the generic `Runtime.close()`), so a
  consumer-built runtime no longer leaks its SQLite connection.
- Added a driver-failure terminalization regression test: a raising driver durable-
  terminalizes the run to FAILED, the failure log carries `run_id` via `extra` (no
  secondary logging `TypeError`), and the public payload never exposes `private_cause`.

### Changed
- `MemoryQueryPort` / `MemoryWritePort` are marked `reserved` (declared but not yet wired
  into the Runtime); consumers must not assume recall or working memory is active.
- Release/CI hygiene: `ci.yml` now runs the full pytest suite plus scoped ruff/mypy;
  `release.yml` gates publish on a same-file `test` job (full pytest + conformance) since
  `needs` cannot reference a separate manual workflow; hardcoded version literals were
  removed from `release-candidate-conformance.yml` and `verify_release_gate.sh` in favour
  of the single `src/simple_harness/version.py` source.

### Backward compatibility
- `build_consumer_runtime`'s new `delivery_sinks` argument is optional; 0.1.3 consumers
  build and run unchanged. `Runtime`/`build_runtime` gain an optional `close_hook` (default
  `None`, no behaviour change).

## 0.1.3 — candidate

### Observability (post-release, 2026-08-19)

- Added structured stdlib `logging` events on the SDK's core execution paths so hosts
  can observe the engine: `run.start` / `run.complete` / `run.fail` / `run.cancelled` /
  `run.admission_denied` (kernel), `provider.invoked` / `provider.usage_untrusted` /
  `provider.charge_unknown` / `reconcile.unknown_settled` (dispatch), `tool.invoked` /
  `tool.authorized` / `tool.denied` / `tool.effect_settled` (executor), and
  `budget.refused_on_unknown` / `budget.exceeded` (budget).
- Events follow a `<module>.<action>` name with structured `extra` fields; tool
  arguments are logged as keys only and never as values.
- Added a regression suite (`tests/unit/runtime/test_logging_observability.py`) that
  locks the observability contract via caplog behaviour tests, AST existence checks,
  and redaction assertions.

**Focus:** Fix two consumer-layer design defects.

### Fixed
- `ConsumerRuntimePorts` now accepts `model` (default `"consumer-model"`); the consumer
  provider adapter uses it as `ProviderTarget.model` instead of the hardcoded constant.
  This lets real consumers whose `ProviderPort` echoes a real model name have their usage
  trusted, instead of always landing in `BudgetCharge.unknown()` and refusing multi-turn runs.
- `ConsumerRuntimePorts` now accepts `tool_schemas` (a name → closed input schema mapping);
  tools with a declared schema accept their arguments. Tools without a schema keep the
  fail-closed no-argument default (the SDK JSON-Schema subset forbids `additionalProperties`).

### Backward compatibility
- New fields are appended after existing fields and carry defaults, so 0.1.2 consumers build
  and run unchanged.

### Semantics note
- When usage is trusted, it is recorded as `TRUSTED_USAGE` at the consumer price estimator,
  which is currently a frozen zero-price estimator — trusted usage therefore books at zero
  cost. This is intentional for the consumer facade and not a pricing path.

## 0.1.2 — candidate

**Focus:** Ease of integration for external projects.

### Post-release fixes (2026-08-19, docs/examples only — no SDK code changes)

- Fixed `examples/minimal-consumer/`: the demo previously printed
  `Run completed: None` (it printed `wait_idle()`'s `None` return), always
  exited 0, and could not be re-run (hardcoded IDs + persistent
  `execution.db`). The demo now reads the real terminal state via
  `client.query(run_id)`, exits 0 only on `COMPLETED`, and uses fresh
  run/session IDs plus a temporary database per invocation.
- Fixed the example's mock provider to the real 0.1.2 provider contract
  (`Message`/`CallId`/`ProviderUsage(input/output/total_tokens)`), and
  documented three integration gotchas discovered while repairing it:
  1. `RunStart.input` must set `max_output_tokens`, otherwise the provider
     reservation is unpriceable and the run fails with `react_cost_exceeded`.
  2. The consumer adapter pins `ProviderTarget(model="consumer-model")`;
     a provider response whose `model` does not match gets its usage recorded
     as an unknown charge, also tripping `react_cost_exceeded`.
  3. The consumer adapter registers placeholder tool specs
     (`additionalProperties: false`, no properties), so tool calls with
     non-empty argument mappings fail schema validation; consumer-level tools
     effectively cannot take arguments in 0.1.2 (use the 10-Port
     `RuntimePorts` API for real schemas).
- Rewrote `docs/quickstart.md` for the real 0.1.2 API: installation section
  now states the only acquisition path (clone repository + `uv build` +
  `pip install dist/simple_harness_sdk-0.1.2-py3-none-any.whl`), and exactly
  one self-contained runnable ```python block (all other snippets marked
  `python fragment`).
- Promoted `build_consumer_runtime` as the recommended integration path in
  `docs/integration-guide.md` and `docs/api/ports.md`; the full 10-Port
  `RuntimePorts` API is now labeled advanced usage.
- Added `examples/minimal-consumer/verify_from_zero.sh`: clean-clone gate that
  extracts build/install commands and the runnable example verbatim from
  `docs/quickstart.md`, executes them, and runs the demo twice (structured
  PASS/FAIL, exit-code gated).
- Added `examples/minimal-consumer/conformance_host.py`: a consumer-level host
  for the SDK conformance protocol covering the `provider` and `tool` suites
  (exercises the provider/tool contracts directly, so it is unaffected by the
  two 0.1.2 consumer-adapter limitations above).
- Added `scripts/verify_release_gate.sh`: one-shot release gate that installs
  the `dist/` 0.1.2 wheel into a clean venv, runs `minimal-consumer`, and runs
  `python -m simple_harness.testing --suite provider,tool` against the
  conformance host (structured PASS/FAIL, exit-code gated).
- Recorded 0.1.2 provenance in `dist/BUILD_INFO.txt` and `dist/SHA256SUMS`
  (wheel built locally at commit `cb1f245`, before `896b685`'s observability
  fix; wheel SHA-256
  `387c8d1d97c0f89e4664347fb57ca6a43a0e7fa772b07a0f34c6f3a6e86efd4c`).

### Documentation
- Added comprehensive Integration Guide (`docs/integration-guide.md`) with step-by-step Port implementation examples
- Added Quickstart guide (`docs/quickstart.md`) for 10-minute first-run experience
- Added complete API reference documentation:
  - `docs/api/ports.md` — All Port interfaces with implementation examples
  - `docs/api/runtime.md` — Runtime lifecycle and error handling
  - `docs/api/workflow.md` — Official workflows and host services
- Added minimal consumer example (`examples/minimal-consumer/`) with working code
- Updated AI Phone handoff document with v0.1.1 changes and Memory integration guide

### API Surface
- Added `MemoryQueryPort` and `MemoryWritePort` interfaces for future Memory SDK integration
- Exported Memory ports from `simple_harness.runtime` module

### Developer Experience
- Created runnable minimal consumer example demonstrating:
  - Mock LLM provider implementation
  - Tool executor with calculator and echo tools
  - Authorization port integration
  - SQLite context persistence
  - Complete runtime setup and execution flow

### Internal Cleanup
- Removed unused `ModelPersonalWorkflowMatcher` class from `turn_authority.py`

## 0.1.1 — candidate

- Added typed provider/tool/runtime/workflow consumer operations with SDK-owned
  case verifiers; consumer Hosts provide observations but never verdicts.
- Added one shared CLI and pytest runner with fail-closed protocol/capability and
  required-case handling.
- Added redacted, fixed-schema conformance reports.
- Made `AuthorizationPort.bind_effect_handoff(...)` mandatory before every
  physical Tool handoff; Hosts that only implement the 0.1.0 `authorize(...)`
  seam must add decision and handoff receipt binders.
- Frozen the ReAct policy fingerprint in start snapshot schema v3 and added
  crash-window delivery/lifecycle recovery coverage.
- Added deterministic candidate build attestations and a non-publishing remote
  three-platform workflow contract.

## 0.1.0

- Initial durable runtime and workflow foundation.
