# Remaining core runtime audit producers — executable sequence

2026-09-05. Source baseline fd4a785; metadata6a8 independently accepted, pagination
incarnation698 + asyncfd fixed review pending. Successor artifact/version allocation
is deferred until this source leaf closes. No frozen0.7.2/main/pin/consumer changes.

The target is each SDK-owned observable Agent operation, not Python statements,
transport audio frames, hidden reasoning, Host post-terminal calls, or Memory internals.
The audit reader alone is insufficient. An existing canonical receipt should be
normalized; only truly absent facts get an SDK-owned durable producer in the authority
transaction. No diagnostics-derived pseudo-complete state.

## Ordered implementation worklist

| ID / production call boundary | Exact source seam and minimum change | Decisive oracle / unchanged authority |
|---|---|---|
| C1 ReAct proposal/batch validation | runtime/drivers/react_loop.py ReAct loop after actual Provider response, before duplicate raw IDs and state.before_tool_batch; EffectBatchExecutor.execute hard batch check. Provider row already proves proposals: normalize exact proposal refs/call ordinals; append SDK batch validation acceptance/rejection outcome where no canonical outcome currently exists. Closed SDK reasons, actual response digest, no raw names/arguments. | Real duplicate-ID/repeated-tool/batch-cap rejection: proposed calls remain audit-visible, physical tools0, no manufactured effect or handoff; valid batch still succeeds. Crash at audit append aborts corresponding acceptance rather than silently executing. |
| C2 TaskExecutionEnvelope / route gate | EffectBatchExecutor.one before issue_envelope and exact Run/call/effect/route/binding validation. Current requested writer in tools/executor.py is later and cannot cover it. Introduce a pre-effect authority-request boundary keyed by actual internal effect/call/turn and Provider response; persist gate result before entering EffectExecutor. Reuse validated route/envelope receipt hashes when accepted. | Real missing/stale/wrong Run envelope and route receipt reject before physical effect, same logical call linked to proposed/gate outcome; valid dynamic route→effect still runs. SDK fence/Host authority checks are not weakened. |
| C3 Provider preclaim | execution/dispatch.py ProviderInvocationCoordinator.prepare_claim/resolve/_prepare_claim_with_binding: binding resolve, estimator and claim budget policy. Existing invocation row only starts after successful claim. Add durable preparation intent/outcome with Run/request fingerprint under active Run lease, then normalize successful canonical claim instead of duplicating physical attempt. | Real binding rebuild/estimator failure and budget denial: preparation denied/failed, invocation/handoff absent, delegatecalls0; successful claim/timeout still separates preparation from actual invocation/unknown attempt. No attempt number invented before claim. |
| C4 Context preparation/selection | react_loop.py services.run_context_authority.prepare_snapshot and _verify_context_authority_receipt; existing frozen checkpoint receipt proves accepted context and actual selection metadata. Normalize accepted receipt/hash with exact SDKRun/request/turn; add preparation started/failure/rejection fact before checkpoint where no acceptance exists. | Real context authority refusal/wrong receipt/freshness failure: next Provider delegate0 and durable failure; accepted context provenance bound to actual frozen request. No payload export, no claim of auditing Memory's own internals. |
| C5 Commands/admission/control | SQLite command ingress + uow.apply_start_command/apply_continue_command/apply_cancel_command, start/resolve_admission, enqueue/claim continuation, commit_runtime_state_and_ack_continuation. Normalize conversation_commands, continuation_progress_receipts and existing run_events/control receipts first; enumerate any mutable-only transition lost before adding same-tx outcome events. | Exact command duplicate/out-of-order/deny/cancel/reopen and continuation ack-loss; each durable command outcome once, zero replay dispatch, pending never success. Pre-Run rejected command has owning command namespace identity and unavailable Run association, never a fake Run. |
| C6 Child lifecycle | uow.claim_profile_launch_and_commit_child, _commit_child_terminal, finalize_child_and_enqueue_parent_signal, claim_next_child_signal, ack_child_signal_and_commit_parent_progress. Normalize child_commands/run_links/child_terminal_receipts/child_signals/child_signal_ack_receipts; parent and child Run association from canonical links, no bare ID guessing. | Actual child launch→terminal→parent signal/ack, duplicate delivery and parent cancellation/late child quarantine; exact relations preserved and no child/provider resend. If an existing receipt already proves the boundary, no new writer. |
| C7 Workflow operations/control | runtime/drivers/workflow.py start/cancel + SQLite workflow operation/native/checkpoint-effect/decision-consumption/start/resume/cancel/recovery/fork/spawn/terminal receipts. Add explicit safe normalizers with owning-Run joins for the listed canonical tables, retaining attempt/state/version/source hash. Only uncovered mutable-only CAS transitions warrant new audit events. | Existing real workflow operation replay, fork, cancel convergence, decision consumption, spawn completion and recovery receipt tests gain public audit assertions. Unknown effects retain unknown; workflow receipt is not another billed Provider operation. |
| C8 Coverage introduction + integration | Add immutable SDK core recording-contract marker in the original root AND child creation transaction (_create_start_on_connection and child commit), only after C1–C7 instrumentation is complete. Reader validates marker version and exact supported driver/capability set. | New real ReAct/workflow Runs report explicit supported core producer coverage; old no-marker Runs remain legacy-unverified. Marker cannot manufacture old facts or certify unknown/custom drivers. Fault between Run birth and marker rolls back both. Same-run restart preserves marker, coverage and replay identity. |

## Coverage contract required by C8

Separate at least these facts in public coverage metadata:
1. source_snapshot_complete: current declared source rows captured without truncation
   or unavailable (existing page snapshot_source_complete).
2. recording_contract_version / supported runtime boundary IDs / unsupported domains:
   immutable producer introduction, not inferred from one arbitrary audit event.
3. historical_coverage: no birth marker means legacy_unverified; missing historical
   transitions are never restamped. Older partial history does not force new supported
   core Runs to remain permanently partial.
4. consumer audit receipt/findings is a separate Host/Service responsibility. Source
   completeness alone is not completed optimization audit.

Every C1–C7 row must be checked against actual source before editing: the table identifies
minimum seams, not authorization to emit guessed operation states. Async calls have
explicit started/result/failure/unknown semantics. Existing canonical transaction
receipts take precedence over redundant audit events. Audit failure must not rewrite
sent/unknown physical work into definitely-not-started. Stable safe snapshot/page,
incarnation, closed codes and registered-name rules apply to every new normalizer.

## Final direct-dependency acceptance / artifact gate

Keep the current85 source cases (not a repeated full suite). Add only the concrete
negative/positive boundary oracles above and relevant existing workflow/child tests.
Include new-Run coverage and old-Run partial controls. Then fixed-source independent
review, one successor version allocation from verified highest unused, dual reproducible
build manifest, and independent installed public consumer: one Run with physical
success/failure + pre-effect deny, raw-metadata canaries absent, complete page traversal
and cursor restart with zero additional Provider/tool dispatch. No wheel/version churn
per small fix, no push/tag. Host installed composition remains separately owned.
