# step02 代码独立 review 原文（子代理 claude-opus-5，2026-09-11）

> 中文摘要：P0 ×1（Worker 可改写验收测试骗过验证）、P1 ×5（重启时活租约抛异常、重启后工具绑定丢失、停滞检测未实现、cancel 未清理 intent/预留/结果、priced 模式 UNKNOWN 费用被当 unpriced 写零）、P2 ×12。处置见 journal.md §3。

---

All 34 orchestrator tests pass. Findings below are from code reading plus four reproductions I ran in the scratchpad (repro scripts left in the session scratchpad, nothing in the repo was modified).

## P0

**1. A Worker can rewrite its own acceptance test and get a false `VERIFIED` delivery.**
`src/agent_orchestrator/artifacts/workspace.py:166` (`verification_copy` copies the Worker's tree verbatim) + `src/agent_orchestrator/verification/deterministic_checks.py:112` (`code_test` runs pytest on that copy) + `workspace.py:155` (`create` re-seeds only files that do **not** already exist, so tampering survives into the repair Attempt).
Repro: a Worker whose script is `write parse_kv.py = "return 'not a dict at all'"` then `write tests/test_parse_kv.py = "def test_nothing(): assert True"` yields `mission: COMPLETED verification_passed`, `layers: format_check/rule_check/critic_review/code_test = PASS`, and the broken file is registered as `accepted_artifacts`. The Critic also passes because it only sees the tampered copy (and, per finding 9, no test output).
This makes the design's central rule ("Agent proposes / system verifies; only VERIFIED is knowledge") and S2-01's "真实测试结果" false against a non-cooperative model — exactly the case step 2 exists to close.
Fix: record the sha256 of every `workspace_seed` file (and of any file named by a `pytest:` / `file:` criterion) at Mission creation; re-materialise those files into the verification copy from the seed before `code_test`, and FAIL `rule_check` when a seeded path's hash differs from the seed.

## P1

**2. Restart while an Attempt turn is in flight kills `run()` (uncaught `CommitRejected`).**
`orchestrator/commit_service.py:775` raises when `attempt.lease_owner` is another owner with a live lease; `orchestrator/event_handler.py:367` calls `renew_lease` with no handler.
Repro (default-ish lease): process 1 dies with the intent `SUBMITTED`; process 2's first `run()` raises `CommitRejected: attempt … is leased to orch-1`. The recovery-matrix tests only avoid this because they set `lease_seconds=0.3` and sleep 0.35 s (`tests/orchestrator/step02/test_recovery_matrix.py:56,65`); with the 60 s default (`runtime/assembly.py:54`) any restart inside a minute crashes the loop.
Fix: in `_observe_liveness`, treat a live foreign lease as "not mine yet" (skip, retry later) instead of propagating; only take over after lapse.

**3. After a restart the tool-gateway binding is not restored for a resumed turn.**
`event_handler.py:204` dispatches only `PENDING/CLAIMED/AGENT_CREATED` intents, and `_bind_workspace`/`_bind_agent` (`:314`, `:323`) run only inside `_dispatch`. `recover()` → `runtime.recover_pending_turns()` resumes turns whose intent is already `SUBMITTED`.
Repro printed `binding for the running agent after restart: None` while the Attempt was `RUNNING`. Any tool call the resumed Worker makes is answered `tool_not_bound`, so the Attempt degrades or fails for a reason unrelated to the work. Not covered by the six fault points (all of them fire before `record_submitted` or after the turn committed).
Fix: rebind every `SUBMITTED` attempt/critic intent in `recover()` before `recover_pending_turns()`.

**4. D6' stall detection is not implemented; `stall_seconds` is dead config.**
`runtime/assembly.py:56` (declared, echoed into `baseline.json` at `:109`) — no other reference. `_observe_liveness` (`event_handler.py:352`) marks `LOST` only when the turn/agent is *missing*; a turn that exists but makes no progress returns `False` forever, `_has_inflight()` stays true, and `run()` spins to `max_cycles` and returns with the Mission `ACTIVE` and `stop_reason=None`. Nothing ever sets `TIMED_OUT`. Repro reached exactly that state (`mission: ACTIVE None`).
Fix: implement D6' — track `provider_turn_ordinal_to` per attempt and mark `LOST`/`TIMED_OUT` when there is no blocker and no advance within `stall_seconds`.

**5. `cancel_mission` leaves the dispatch intents, reservations and pending results live.**
`commit_service.py:456` cancels Mission/Task/Attempt only. `event_handler.py:204` iterates intents with no mission filter.
Repro: cancel while the Attempt is `PENDING`, then `run()` → the intent is claimed, a real Agent is created and `submit()` is issued (money spent on a cancelled Mission), then `run()` dies with `IllegalTransition: illegal attempt transition CANCELLED -> RUNNING`; the reservation stays `RESERVED`. The same shape hits `_verify` → `start_verification` for a result left `PENDING` at cancel time.
Fix: in `cancel_mission`, move every non-terminal intent to `FAILED`, settle each reservation, mark pending results `REJECTED`; and filter `_cycle`'s intent/result scans by non-terminal mission.

**6. In priced mode an UNKNOWN provider charge is settled as unpriced-zero and the reservation is released; the D10' `model_echo_mismatch` fail-fast does not exist.**
`runtime/agent_worker.py:134` (`amount = None if unpriced else charge.amount_micros`) → `governance/budgets.py:269` stamps `unpriced=1` for *any* `None`, conflating `pricing_mode="unpriced_local"` with a genuinely unknown charge → `budgets.py:299` `settled_cost = 0 if cost is None else cost` and the chain reservation is released. `event_handler.py:436` only logs a note on `has_unknown_charge` and proceeds. This directly contradicts D10' and the plan's own risk row ("预留保持占用，不写零、不释放").
Reachable in practice: `execution/dispatch.py:648` turns any `response.model != ports.model` into `BudgetCharge.unknown()`, and D10' required a `model_echo_mismatch` fail-fast for exactly that — `grep -rn "model_echo"` finds nothing in the package. The next call is then refused (`refuse_on_unknown`), the turn FAILs, `_collect_attempt:450` settles → zero, released.
S2-08's test only exercises the "turn never commits" flavour, so acceptance for the committed/failed-with-UNKNOWN flavour is unproven. There is also **no test anywhere that constructs a `PriceTable`/`hard_cap_micros`**, so `OrchestratorConfig.policies()` and the D10' hard-cap sinking are entirely unverified.
Fix: carry the pricing mode into `UsageFact` (`unpriced` vs `unknown`); make `settle()` refuse while any imported fact is `unknown` in priced mode; add the `response.model` echo check with fail-fast.

## P2

7. `mark_attempt_lost` (`commit_service.py:789`) settles without importing usage first (`event_handler.py:375`) — a LOST attempt's real spend is recorded as zero.
8. `_settle_intent` (`event_handler.py:381`) writes to the store outside `CommitService` and appends no event (D2/§15 single-writer). `mid_commit` is listed in `FAULT_POINTS` (`event_handler.py:77`) but never armed or fired anywhere — the D14' six-point matrix is only five points deep.
9. The Critic never sees the test output: the router runs `critic_review` before `code_test` (§14.1 order), so `run_critic(test_output)` at `verification/verifier_router.py:113` always gets `None`. D9 promised the Critic "只读产物副本 + 测试输出".
10. `format_check` (`deterministic_checks.py:49`) unconditionally returns PASS — a required layer that cannot fail; the `client_result_id` it records is never compared to anything.
11. `run_tests` option injection: `tool_gateway.py:183` calls `workspace.resolve(path)` and discards the result, then `run_pytest` appends the raw model-supplied string to the pytest argv (`:95`). A `path` like `--rootdir=…` is accepted. Pass the resolved path after a `--` separator and require it to exist.
12. `renew_lease` emits one `HeartbeatReceived` per poll while blocked (`event_handler.py:361`, key includes the ms timestamp) — with `poll_interval=0.05` that is ~20 events/s for exactly the S2-08 UNKNOWN-blocked case. Throttle to ~lease/2.
13. `usage_refs` from the envelope are stored (`store.py:75`) but never cross-checked against the SDK ledger, contrary to D7/D10' ("只做交叉核对" implies it happens).
14. `artifact.version` is always 1 (`workspace.py:113` is called with `versions=None`); D19 wants per-(attempt, path) increments, and `UNIQUE(attempt_id, path, version)` (`storage/schema.py:155`) is not covered by `upsert_artifact`'s `ON CONFLICT(artifact_id) DO NOTHING` (`store.py:661`) — a genuine second version would raise `IntegrityError`.
15. `__main__.py:92` leaves a paid `--provider env` run in `unpriced_local` mode when `SH_PRICE_*` is unset; D10 requires a price table for a paid provider. `tests/orchestrator/step02/test_real_provider_single_task.py:40` does the same.
16. `claim_intent` (`commit_service.py:636`) applies the live-lease guard only when `state == "CLAIMED"`; an `AGENT_CREATED` intent can be taken from a live owner.
17. `cancel_mission` does not settle reservations (permanent budget leak in `costs.json`) and emits no Task/Attempt-level event; D15' asks for VERIFYING→ACTIVE→CANCELLED as two commits with separately visible events — it is one transaction with one `MissionCancelled`.
18. Dead/cosmetic: `Workspace.resolve`'s symlink guard (`workspace.py:64`) tests `is_symlink()` on an already-`resolve()`d path and can never be true (containment is what actually blocks escapes — that part is correct); `ALIVE_STATES` in `agent_worker.py:27` is unused; D2's `leases` table does not exist (lease columns are inline — fine, but the plan text is stale); `accept_result` never re-checks `attempt.task_version` as D6' requires (and cannot usefully, since `create_attempt` bumps `task.version` on every attempt).

## Test gaps vs acceptance
S2-03's eight idempotency items are covered. Not covered anywhere: cancel-then-run; restart with a non-toy lease; priced mode / hard cap / UNKNOWN-on-a-committed-turn; a Worker that tampers with the acceptance test or the seeded files; a stalled-but-alive turn.

## Verdict
No. Step 2 should not ship as-is: the P0 makes the verification loop defeatable by the very actor it is meant to check, and P1s 2–5 mean an ordinary restart or a cancel either crashes the control loop or hangs the Mission with no stop reason. All six are small, localized fixes (seed-hash pinning, one `try` around `renew_lease`, a rebind pass in `recover()`, a stall check, cancel cleanup, and an `unknown` vs `unpriced` distinction). With those fixed plus a test each — especially a priced-mode test and a tampering test — Step 2 can ship; the P2s can ride into step 3.