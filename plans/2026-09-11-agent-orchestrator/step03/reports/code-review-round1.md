# step03 代码独立 review 原文（子代理 claude-opus-5，只读，审 e7be878..9ddb3dd，2026-09-11）

> 中文摘要：P0 ×2（`stop_task` 对 READY Task 抛非法转换、整合副本目录被另一实例 rmtree）、P1 ×9（验证期租约不复核、TOCTOU、迟到结果采集未实现[已在 b02b113 修]、列出受保护路径可绕过保护、intent 里的 attempt_id 是预测值、critic 派发热旋、三处空断言、两个新崩溃点无测试、跨实例并发上限未强制）、P2 ×10。结论 SHIP-AFTER-P0-P1。处置见 journal.md §3。

---

I've completed the review. Here are my findings.

---

# Step-3 code review — `agent_orchestrator` (e7be878..9ddb3dd)

## Findings

### P0

**1. `stop_task` on a READY Task raises `IllegalTransition` and kills `run()` — the runtime `artifact_conflict` stop is dead code**
`src/agent_orchestrator/orchestrator/commit_service.py:1607-1621` handles `VERIFYING` (→ACTIVE→FAILED) but nothing else; `§25.1` has **no** `READY→FAILED` edge (verified: `assert_task_transition(READY, FAILED)` raises).

Two callers hand it a READY Task:
- `event_handler.py:1155-1167` — `merge_accepted` raises `ArtifactConflict` **before** `create_attempt`, i.e. while the Task is still READY (this is exactly D's first Attempt in a `A→(B‖C)→D` graph when B and C disagree on a path). `next_task(READY, FAILED)` → `IllegalTransition` → propagates through `_decide` → `_cycle_inner` → `_cycle` (which catches only `StoreBusy`) → `run()` dies with an unhandled exception instead of the documented `MissionStopReason.ARTIFACT_CONFLICT` stop.
- `event_handler.py:1262-1284` — `BudgetExhausted` on the **task** account for a Task's *first* Attempt (the reserve in `create_attempt:899` happens before any `update_task`, and the transaction rolls back, so the Task is still READY). Reachable with a real Planner: `"max_attempts": 0`, or `max_cost_micros` normalised to `mission//n` while `max_tokens` is large and a `price_table` is configured — `_reservation()` derives cost from tokens and can blow the cost dimension on attempt #1.

Failing scenario: commit a graph where B and C both write `notes.md` with different content without declaring `outputs` (the static check in `task_graph.py:207-222` only sees *declared* outputs, so nothing catches it at commit time). When D is allocated, the orchestrator raises `IllegalTransition: illegal task transition READY -> FAILED` and the Mission is left ACTIVE forever.

Note the runtime conflict path has **zero test coverage** — `grep artifact_conflict tests/` only hits the static `test_task_graph.py:114`.

Fix: in `stop_task`, promote `READY→ACTIVE→FAILED` the same way `VERIFYING` is promoted (both are legal edges), and refuse/`CANCEL` for `BLOCKED`. Add a test that drives the runtime `merge_accepted` conflict end to end.

**2. `WorkspaceManager.integrated_copy` destroys a Mission-scoped directory that another instance may be running pytest in — S3-04 can produce a wrong `mission_criteria_unmet` verdict**
`artifacts/workspace.py:178-181` (`rmtree` then `mkdir` on `<mission_id>-verify`), called from `event_handler.py:1321`.

`_decide` (`event_handler.py:1093-1098`) tests `mission.status` on the **stale** `Mission` object captured by `_active_missions()` at `event_handler.py:280`, so with two instances both can enter `_judge` for the same Mission. Instance A: `integrated_copy` → `await run_pytest(str(copy.root), …)`; while A is parked on that await, B enters `_judge` and reaches `integrated_copy` without yielding (`_reimport_unsettled`, `merge_accepted`, `integrated_copy` are all synchronous) and `rmtree`s the tree A's subprocess is running in. A's pytest then fails, A calls `judge_mission` with `met=False` first, and the Mission is judged **FAILED / mission_criteria_unmet** even though everything passed. `test_s3_04` (`test_multi_scheduler.py:35`) asserts `MissionStatus.COMPLETED` and is therefore a latent flake with a genuine wrong-outcome failure mode.

`verification_copy` (`workspace.py:210-213`) has the identical shape for `<attempt_id>-verify` and is exposed by the same-attempt double-verify window in finding 3.

Fix: make the judgment/verification copy per-owner or per-invocation (e.g. `<mission_id>-verify-<owner>` / a temp dir), or gate `_judge` on a re-read of the Mission plus a Mission-level lease claim.

---

### P1

**3. A stale lease owner can commit: `_verify` renews once and never re-checks before `accept_result` / `fail_result`**
`event_handler.py:886-894` renews the Attempt lease at the *start* of verification, then runs `rule_check` + a Critic turn (`critic_wait_seconds=120.0` by default, `event_handler.py:112`) + `code_test` before `accept_result` at `event_handler.py:942-948`. With the default `lease_seconds=60.0` the lease lapses **during** verification. A second instance's `renew_lease` (`commit_service.py:1135-1141`) then succeeds, it re-verifies the same result (double spend on `run_pytest`; the Critic subject is idempotent so at least the model call is shared), and both call `accept_result`. `accept_result` is idempotent, so state stays sane, but whichever verdict lands first wins — a *stale* PASS can beat a *fresh* FAIL. Neither `accept_result` nor `fail_result` takes an `owner` argument or checks `attempt.lease_owner`.

Fix: pass `owner` into `accept_result`/`fail_result` and assert the lease inside the transaction; renew periodically during long verifications (or make `critic_wait_seconds < lease_seconds`).

**4. `_verify`'s terminal-attempt guard is a TOCTOU that turns a benign race into an unhandled exception**
`event_handler.py:938-941` reads the attempt outside any transaction; `accept_result` (`commit_service.py:1449`) then does `next_attempt(attempt, COMPLETED)`. If a sibling candidate was accepted (or a cascade fired) between the read and the transaction, the attempt is `SUPERSEDED` → `IllegalTransition` → propagates out of `_cycle` (which catches only `StoreBusy`, `event_handler.py:250-256`) and terminates `run()`. Same shape applies to any `CommitRejected` raised outside `_next_attempt`'s handler.

Fix: catch `IllegalTransition`/`CommitRejected` around the accept/fail calls (drop the verdict, as the guard already intends), and make `_cycle` treat `CommitRejected` as a skipped cycle rather than a fatal error.

**5. D3-6'' is not implemented: `_close_attempt` closes the intent of a still-running turn, so `record_late_result` is unreachable**
Plan §7 D3-6'' requires: "SUPERSEDED/CANCELLED 的 Attempt 若 SDK turn 仍在运行，其 intent 保持 SUBMITTED 直到 turn 结束再采集…Mission 终态后循环仍采集这类 intent；未提交的 intent 立即关闭".

The code does the opposite: `commit_service.py:674-681` settles **every** intent in `{PENDING, CLAIMED, AGENT_CREATED, SUBMITTED}` to `FAILED`, and `_cycle_inner` collects only `list_intents("SUBMITTED")` filtered to non-terminal Missions (`event_handler.py:270-273`). Consequence: the late-result branch in `_collect_attempt:722-736` and `commit_service.record_late_result:1330` can never run, and the loser's usage is only recovered by `_reimport_unsettled` on the *next* `recover()`.

This is what makes the S3-05 assertion vacuous (finding 9).

Fix: in `_close_attempt`, close only intents in `{PENDING, CLAIMED, AGENT_CREATED}`; leave `SUBMITTED` open; keep collecting such intents in `_cycle_inner` regardless of Mission terminality.

**6. A downstream Worker can register a rewrite of a protected upstream file by *listing* it — protection is Planner-policy-dependent**
`event_handler.py:825-833`:
```python
referenced = [
    artifact for artifact in artifacts
    if artifact.path in listed
    or (artifact.path not in guarded and initial.get(artifact.path) != artifact.content_hash)
]
```
`artifact.path in listed` bypasses `guarded` entirely. The only backstop is `rule_check` (`deterministic_checks.py:70-72` on `tampered`, plus the verification-copy hash mismatch), and `validate_graph` never *requires* `rule_check` — a Planner that emits `verification_policy: ["format_check", "code_test"]` disables the backstop completely. Then the tampered artifact becomes one of `task.accepted_artifacts`, and because the tamperer *depends on* the original producer, `merge_accepted` (`versioning.py:102-107`) treats the override as **legal** and propagates the smuggled content downstream and into the Mission judgment tree. That is exactly the D3-7' / review-P0-5 cheat path re-opened.

Fix: `and artifact.path not in guarded` must apply unconditionally (a listed guarded path should be reported as a rule violation, never registered); and/or force `rule_check` into every Task's effective policy.

**7. `intent_config["attempt_id"]` is a *predicted* id that can diverge from the real Attempt id**
`event_handler.py:1168-1188` builds a `placeholder` with `ids.attempt_id(task.id, len(attempts) + 1)` and stores it at `event_handler.py:1247` as `intent_config["attempt_id"]`; `create_attempt` computes its own `ordinal = len(existing) + 1` (`commit_service.py:896`) and does **not** overwrite `attempt_id` in the config (`commit_service.py:943`). `_bind_agent` (`event_handler.py:439-448`) then binds the SDK agent to `config["attempt_id"]`.

With `candidates_per_task >= 2` and two instances, instance B can insert candidate #2 between A's read and A's transaction; A's Attempt becomes `…:attempt-3` while its gateway binding still points at `…:attempt-2` — two Workers writing one workspace, and `_collect_attempt`'s `workspaces.get(attempt.id)` snapshots an empty tree → spurious "artifacts not in the workspace" → RETRY_WAIT. (Safe today only because `candidates_per_task=1` makes the open-attempt guard reject the second creation first.)

Fix: have `create_attempt` write the authoritative `attempt_id` into the intent config it inserts.

**8. `_run_critic` hot-spins when another owner holds a live claim on the critic intent**
`event_handler.py:1057-1061`:
```python
while intent.state in {"PENDING", "CLAIMED", "AGENT_CREATED"}:
    await self._dispatch(intent)
    intent = self.store.get_intent(intent.intent_id)
```
`_dispatch` returns `False` without doing anything when `claim_intent` refuses (live lease held by the other instance, `commit_service.py:1002-1008`). There is no sleep and no deadline, so the coroutine spins at 100% CPU until the other owner's lease lapses — and if that owner died holding a `CLAIMED` intent, it spins for the whole `lease_seconds`.

Fix: `await asyncio.sleep(self._poll)` on a no-op dispatch and bound the loop by `self._critic_wait`.

**9. Vacuous / self-defeating test assertions behind three acceptance claims**

- `tests/orchestrator/step03/test_static_dag_closure.py:214-223` (S3-03, "the repair Attempt started from the upstream inputs again"):
  ```python
  assert (... / "textkit/__init__.py").read_text() == TEXTKIT_SEED.get(
      "textkit/__init__.py",
      (... / "textkit/__init__.py").read_text(),   # default is the value under test
  )
  ```
  `TEXTKIT_SEED` (`fixtures.py:273-290`) has **no** `textkit/__init__.py` key, so the default branch always fires and this reduces to `x == x`. It proves nothing. It should compare against A's accepted artifact hash (`TEXTKIT_CONTRACT`).
- `test_static_dag_closure.py:286` — `assert late == [] or late[0].payload["reason"] == "superseded"`. Per finding 5, `late` is **always** `[]`, so S3-05's "旧执行迟到结果不被接受 / 有取消回执" late-result half is unproven. `:281-284` (`find_result_for_attempt(loser) is None or verdict != "PASS"`) is weak for the same reason. (`count_events(TaskCompleted) == 1` at `:285` does carry the real weight.)
- `tests/orchestrator/step03/test_multi_scheduler.py:66-71` — comment says "both dispatched something", assertion is `assert dispatched["orch-1"] or dispatched["orch-2"]`, trivially true if one instance did everything. Nothing in `test_s3_04` forces the two instances to contend for the same intent (no holds), so S3-04's "两个 Orchestrator 同时领取 / 每个 Attempt 只有一个 owner" is not demonstrated under contention. `attempts[0].lease_owner in {"orch-1","orch-2"}` (`:58`) is also trivially true. The load-bearing evidence is `provider.calls_by_key == CALLS` and `sdk["agents"] == 6` / `turns == 1` — keep those, but add a held Attempt so both instances race on one intent, and assert `AttemptClaimed` was emitted exactly once per Attempt.

**10. The two new fault points from D3-6' are never exercised**
`after_accept_before_supersede` and `after_task_completed` are declared (`event_handler.py:98-99`) and placed (`commit_service.py:1471`, `event_handler.py:949`), but `grep -rn "arm_fault" tests/` shows only `after_submit` (step 3) and the step-2 recovery matrix. So D3-6''s central claim — accept+supersede+unblock is atomic and replays idempotently — has no test. Also note `after_accept_before_supersede` sits *inside* the transaction, so firing it rolls the whole accept back; the name suggests a partial commit it cannot produce. Worth a comment and a test that asserts "nothing was written, replay yields the same receipt".

**11. `max_concurrency` is unenforced across instances**
`allocate` (`scheduling/allocator.py:103`) bounds `open_total` per instance, but `create_attempt` only re-checks `candidates_per_task` inside the transaction (`commit_service.py:889-895`). Two Orchestrators therefore admit up to `2 × max_concurrency` in-flight Attempts, and `max_concurrent_model_calls` (sized as `max_concurrency × candidates_per_task`, `assembly.py:88-90`) will be over-subscribed — the semaphore-waiting turns are exactly what D3-4' tried to keep out of the stall detector. Not in the S3 acceptance table, but the D3-4 wording ("受 Mission max_concurrency 限制") is not met. Fix: enforce the Mission-wide open-Attempt count inside `create_attempt`'s transaction.

---

### P2

12. `commit_service.py:1459` — `accept_result` calls `_settle_subject` unconditionally, while every other settle path guards with `has_unknown_usage` (`_close_attempt:686`, `mark_attempt_lost:1173`, `_settle_if_known`). An accepted Attempt with an UNKNOWN provider charge releases its reservation anyway; inconsistent with ORCH §12.2 fail-closed.
13. D3-17 says unbind must happen *at* the transition and *before* `cancel_turn`; in practice `_close_attempt` never unbinds and the unbind is a follow-up in the event handler (`_verify:951-953`, `_release_mission`). After a crash at `after_task_completed`, `heal_mission` reports no `closed_attempts` (the siblings are already SUPERSEDED), so those SDK turns are never cancelled at all. `cancel_mission` (`commit_service.py:746`) also has no `_release_mission` counterpart.
14. `event_handler.py:1113-1114` — `break` on a terminal granted Task skips the remaining grants of the cycle; `continue` is what's meant.
15. `_protected_files` (`event_handler.py:987-995`) silently drops upstream inputs whose artifact row/file is missing or non-UTF-8 — those paths end up unprotected. Prefer failing closed.
16. `_bind_workspace` (`event_handler.py:421-424`) raises `ArtifactConflict` straight out of `_dispatch`; nothing catches it, so a moved/edited evidence tree kills `run()` rather than stopping the Task.
17. `commit_service.py:906-907` — `try: … except BudgetExhausted as error: raise error` is a no-op; delete it.
18. `recover()` (`event_handler.py:195`) runs outside `_cycle`'s `StoreBusy` guard; a lock contention at start-up (both instances calling `heal_mission`) terminates `run()`.
19. `_next_attempt`'s mission-pool routing (`event_handler.py:1269-1279`) maps `dimension == "attempts"` to `MAX_ATTEMPTS_REACHED` even on the Mission account, where D3-12' literally says `budget_exhausted`. `test_s3_08c:390` codifies the code's behaviour. Sensible, but the plan text and the code disagree — pick one.
20. `next_versions` is computed per-snapshot from `list_mission_artifacts` (`event_handler.py:796`), so two candidates of one Task that both change a path both get `v_n+1`; `upsert_artifact` has no `(mission, path, version)` uniqueness. Harmless today, cosmetic drift from D3-8'.
21. S3-07a/b rely on real wall-clock `asyncio.sleep(0.7)` against `lease_seconds=0.6` (`test_multi_scheduler.py:101,106,149`). Safe in the slow direction, but `lease_seconds=0.6` also bounds `_verify`, which shells out to pytest — on a loaded machine the lease lapses mid-verification. Fine while single-owner; brittle if the test ever gains a second instance.

---

## Verdict

**SHIP-AFTER-P0-P1** — the architecture is sound and the happy paths are genuinely proven, but finding 1 makes a documented stop reason (`artifact_conflict`) crash the loop instead of stopping cleanly, finding 2 can hand back a *wrong* Mission verdict under the S3-04 configuration the step claims to support, and finding 5 is an outright §7 promise (D3-6'') that the code inverts. Findings 3/4/6/7 are all narrow-window but real state-machine/security holes.

## What I verified as correct

- **Graph commit (D3-1/2/3, S3-02/S3-08a)**: `validate_graph` order — dedup → Kahn → sibling-output → per-node contract → per-dimension and summed budget → shape; whole-proposal rejection with the write transaction rolled back and `TaskGraphRejected` emitted *outside* it (`commit_service.py:464-472`); deterministic topological id assignment; replayable receipt; `graph_version` in `final_report` (D3-18).
- **D3-2' normalisation**: even split of `max_tokens`/`max_cost_micros` (`count*(parent//count) <= parent` always holds), inheritance of `max_attempts`/`max_concurrency`/`max_runtime_seconds`, rejection feedback fed back as `planning_rejected`, `max_planning_attempts` knob. `test_s3_02` proves the feedback actually reaches the second Planner package.
- **D3-16 dedup**: reject on repeated key or (goal ∧ deps ∧ criteria); warn otherwise. Tested both ways.
- **D3-5' derived Task status**: `record_result` ACTIVE→VERIFYING only when ACTIVE; `fail_result`'s `others_submitted` check correctly keeps a Task VERIFYING while a sibling is still submitted and returns it to ACTIVE otherwise; `accept_result` walks ACTIVE→VERIFYING→COMPLETED as two legal edges in one transaction; open-candidate cap enforced *inside* the transaction (so two instances cannot exceed `candidates_per_task`); candidates counted against `max_attempts`; losing SUBMITTED candidates keep their `StoredResult` (REJECTED/superseded) and their artifact rows.
- **D3-6' atomicity**: accept + settle + supersede-siblings + unblock-dependents are genuinely one `Store.transaction()`; `_unblock` re-reads inside the transaction so it sees the just-written COMPLETED task; rollback on any exception verified in `store.py:231-240`; all `_emit` calls nest into the enclosing transaction (`store.append_event` re-enters via `_depth`).
- **D3-13' cascade**: `_cascade_stop` promotes VERIFYING→ACTIVE→CANCELLED and leaves BLOCKED untouched; proven by `test_s3_08b:370` and `test_s3_08c:394-396`.
- **D3-7'/D3-7'' artifact flow**: `ancestors` closure is correct on a DAG; `merge_accepted`'s override rule (legal only along a dependency chain, identical hashes never conflict, independent branches fail closed) is right, and ordinal order is a valid topological order because ids are assigned topologically; upstream inputs frozen into the intent with `(task_id, path, hash, artifact_id)` and hash-verified at `_bind_workspace`; `outputs` carves the legitimate stub→implementation rewrite out of the protected set; the static sibling-`outputs` check is correctly scoped to mutually independent tasks.
- **D3-8'**: `(mission, path)` lineage via `list_mission_artifacts` + `next_versions`; `test_s3_01:134-139` proves A's stub is v1 and B's implementation v2.
- **D3-9'**: judgment runs on the integrated tree of *all* tasks, not one terminal task; `pytest:`/`file:`/free-text routing; `judge_mission` requires all Tasks COMPLETED, covers every criterion in order, reports every Task, and is idempotent on a terminal Mission (so "Mission 恰完成一次" holds even with two instances); `code_test` no longer runs Mission-level pytest targets per Task (the fix that makes B independent of C).
- **D3-10'**: `OWNER_SCOPE` constant shared, per-instance `owner` used for both the orchestration lease and the SDK `owner_id`, `lease_seconds >= 2 × sdk_lease_ttl_seconds` enforced in `__post_init__`, `StoreBusy` wrapping + cycle skip. `claim_intent`'s CAS correctly refuses a live foreign lease and permits same-owner replay with a `replays` counter.
- **D3-4'**: `max_concurrent_model_calls` raised to `max_concurrency × candidates_per_task`; stall timing gated on `liveness.state == RUNNING` and `not blocked`.
- **D3-12'**: `BudgetExhausted` routed by `account_id` — mission account → `fail_mission` (no Task blamed, `failed_task_id` absent), task account → `stop_task`. `test_s3_08b/c` cover both.
- **S3-01 parallelism** is genuinely proven (two simultaneously held `RUNNING` Attempts, `max_inflight == 2`, D BLOCKED, `AttemptCreated(D)` strictly after both `TaskCompleted`), **S3-03 no-rerun** is genuinely proven (`calls_by_key` per key, B's Attempt list unchanged), **S3-07a same-turn takeover** is genuinely proven (same attempt id, same `turn_id`, one SDK turn per agent, zero `AttemptLost`), **S3-07b LOST→retry** likewise (`retry_of`, reservation SETTLED, exactly one `AttemptLost`).
- Terminology is used in its defined sense throughout: `frontier()` really is "READY ∧ all deps COMPLETED", the Allocator only decides *which/how many* under bounds, `Supersede` is used only for losing candidates and orphaned Attempts under a COMPLETED Task, `Commit` remains the single logical writer (no direct `Store` mutation from the event handler), and `Lease` is per-Attempt with `owner_scope` reserved for the SDK.