# Core producer implementation checkpoint — 2026-09-05

Original C1–C8 acceptance in CORE-BOUNDARIES.md remains authoritative. This is
implemented source for independent review, **not full source-leaf completion**.
No package version, frozen main, schema descriptor or wheel changed.

## Production changes and evidence

- Real kernel/Provider/Context/batch/envelope/route call intervals now persist
  started and disposition under the actual runtime lease. Safe fixed names and
  closed error codes expose failures before there is an invocation/effect row.
  On lease loss a recording failure preserves the original exception classification;
  the unmatched start stays unverified. Physical sent/unknown semantics are unchanged.
- Every actual public Provider tool proposal has a separate ordinal-bound audit
  record, including duplicate raw call IDs and pre-effect rejection. A proposal has
  no fabricated effect identity and is not a physical attempt or success.
- Explicit canonical control/child/workflow sources include lifecycle, native
  operations, decision consumption, fork, spawn and terminal receipts. Actual
  parent/child relationships come from canonical owner columns and joins.
- Canonical event witnesses share the original transaction, including both direct
  workflow adapter event writers. Mutable continuation/signal claim CAS now records
  each claim epoch before the historical head can be overwritten.
- Coverage pairs each started/settled interval by operation/name/input/epoch/owner/
  contract/start time, checks actual birth/event/activation facts, current canonical
  Provider/effect heads, proposal dispositions and claim-epoch continuity. Exact
  custom drivers do not inherit the official recording claim. Missing evidence is
  not backfilled. The page normalizer is v2; incompatible old spools fail explicitly.

Raw evidence root (ignored): `.local-test-evidence/2026-09-05/run-operation-audit/`.

| Oracle | Actual result / local log |
|---|---|
| Fixed7062 source against new duplicate/context/preflight/route cases | 4 FAIL, each missing durable boundary; `core-rejection-red.log` |
| Same four real SQLite/kernel cases with producers | 4 PASS; `core-rejection-1.log` |
| Envelope refusal, estimator failure, budget denial, restart/custom driver, four corrupt interval-pair negatives | 13-case core module PASS; `core-restart-1.log` records the pre-proposal revision; included in the adjacent run below |
| Directly affected audit/pages/ReAct/recovery/noop/child/adapter/cancel/barrier/continuation set | 196 PASS in4.47s; `core-adjacent-2.log` |
| Real canonical workflow resume/fork replay projection | 2 PASS,43 intentionally deselected in0.17s; `core-workflow-relations.log` |
| New WAITING -> exact installed old0.7.2 resume -> new reader | Old physical tool1, Provider1, completed; new v2 birth remains but coverage unverified; `old072-resume-2.log` |

Old wheel SHA256: `53bded3fea87168e5d2ad9e49fea5f99e1c1edb1d6077b2a52dd62716692f9ed`.
Reproducible old-runtime oracle is `scripts/acceptance/run_audit_old_runtime_resume.py`,
with explicit `--old-python` and fresh `--database`; subprocess removes PYTHONPATH
and verifies installed0.7.2/site-packages before execution. It imports only the public
runtime test fixture, never mocks checkpoint/route/ledger restoration.

The former ordinary large-page `partial` expectation was changed only after the
new-runtime restart positive, exact old-runtime negative and interval-pair negatives.
It now asserts no gaps, introduction v2 and verified current intervals together.
This does not certify command or unsupported driver histories.

## C5 remaining production storage gap — not waived

`CommandIngress.claim_next/transition/retry/reject` currently overwrite
`conversation_commands` (claim/attempt count and last error). A start command can
retry or reject before a real Run exists. `run_events.run_id` has a real Run foreign
key; creating a fake Run or placing audit history in terminal-cleared raw payload
would violate authority/data contracts. Current reader explicitly returns
`command_transition_history_unverified` for a Run with such commands.

An existing immutable canonical command history source has not been found. The
minimal SDK-owned pre-Run recording storage/migration contract is under independent
challenge. No implicit schema table, fabricated history, or source-complete claim
substitutes for this MUST. Full workflow producer continuity also remains subject to
the fixed-source review of actual execution intervals, not merely table enumeration.

After source closure: independent review, verify unused successor version once,
one reproducible dual build, then independent installed public consumer. No paid
Provider, native, full suite or successor artifact was run for this checkpoint.
