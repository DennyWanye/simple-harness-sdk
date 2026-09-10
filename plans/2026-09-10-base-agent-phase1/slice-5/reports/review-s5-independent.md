# Slice 5 独立 review 原文（子代理 claude-opus-5，2026-09-10）

> 中文摘要：P0 一项（K1：BaseAgent driver 异常后无人再唤醒 Run、且无重试上限）；P1 两项（M1：备份路径已有外来文件被接受；C1：工具信号量在委派等待期间被持有会死锁）；P2 三项（M2 校验范围、E1 升级重试的取消/截止再检查、E2 成功也记录升级历史）；推测项 A1（companion 内 stage 抛错）。处置见 journal.md §3。

---

I have everything I need. Here is the review.

```json
{
  "findings": [
    {
      "id": "K1-no-wake-no-cap-on-base-agent-driver-exception",
      "severity": "P0",
      "file": "src/simple_harness/runtime/kernel.py:3231",
      "evidence": "The new branch drops authority and returns, but nothing ever produces the 'next wake' the comment promises. `_abandon_run_authority` does not add the run to `_pending_wakes` (the only thing `_wake_drain` retries), and `list_runs_with_open_agent_turns` is consulted **only** in `recover()` (kernel.py:2051), which runs at startup or on an explicit `reconcile()`. Repro (scratchpad/review5/repro_stall.py: `_run_turn` raises RuntimeError unconditionally): 2 drive attempts, then `agent.wait_turn(timeout=3)` raises AgentTurnTimeout and the durable state is `run.state=running, turn.phase=running, continuation=claimed` — permanently, with the Run stuck in RUNNING (not WAITING) and the lease released. Second half: there is no attempt cap or backoff anywhere, and the deterministic driver exceptions in `agents/execution.py:193` (`raise ValueError('base_agent_input belongs to another Agent')`) and `:196` (`raise TypeError('base_agent_input requires a message object')`) are exceptions, not `_binding_failure` results — so a poison input is now silently invisible forever (previously it terminalized the Run as FAILED, which was at least observable) and re-drives once per `recover()`/restart with no ceiling. `_binding_failure` (FAILED DriverResult) is still the path for the checks that use it, but not for these raises.",
      "fix": "In the new branch, after `_abandon_run_authority(run_id)`, register `self._pending_wakes.add(run_id)` so the existing `_wake_drain` re-drives it, AND add a bounded retry: persist a per-turn driver-exception attempt count and, once it exceeds a cap, settle the turn as a visible failed AgentTurn result (`failed_outcome` with e.g. `base_agent_driver_exception`) with backoff between attempts, instead of leaving the input claimed indefinitely. Separately, convert the deterministic input-validation raises in `agents/execution.py:193,196` to `_binding_failure(...)` so they never take the exception path."
    },
    {
      "id": "M1-foreign-retained-backup-accepted-source-image-never-saved",
      "severity": "P1",
      "file": "src/simple_harness/execution/sqlite/base_agent/migration.py:160",
      "evidence": "`if not backup.exists():` skips backup creation when anything already sits at `backup_path`; the only check on the retained file is `_validate(saved) != 9`, i.e. 'is some v9 library'. Unlike the mirrored precedent `short_context_migration.migrate_execution_to_v9`, which computes `root = _root(writer)` from the source and refuses with `_root(saved) != root` (short_context_migration.py:211) and re-verifies it on replay (`:157`), the v10 receipt has no `source_root_hash` field at all and `_receipt` (migration.py:96) only checks `backup.is_file()` + `_bytes_hash`. Repro (scratchpad/review5/repro_backup.py): source `a.db` carries a unique `host_precious_marker` table; an unrelated v9 library is placed at `a.db.bak` beforehand. Output: `migration returned receipt: True`, `receipt.backup_sha256 == foreign bytes: True`, `backup contains a's marker table: False`. The v10 DDL is applied and the pre-upgrade image of `a.db` is never written anywhere, while the receipt attests to a valid backup.",
      "fix": "Mirror the v9 migrator: add `source_root_hash` to `ExecutionBaseAgentUpgradeReceiptV1`, compute it from the writer before the DDL, and in both the create-backup path and `_receipt`'s replay path reopen the backup and refuse unless `_validate(saved) == 9 and _root(saved) == source_root_hash`."
    },
    {
      "id": "C1-tool-semaphore-deadlocks-delegation",
      "severity": "P1",
      "file": "src/simple_harness/agents/tool_registry.py:120",
      "evidence": "`ExposureGuardedTool.limited()` holds a permit of the runtime-wide `tool_semaphore` for the entire handler, and `AgentDelegateTool.handle` blocks *inside* the handler polling for the child's result for up to `limits.delegation_wait_seconds` (agents/tools/delegate.py:400-417). Children inherit the parent's tools minus `agent_delegate` (delegate.py:250), so a child that calls any tool needs a permit from the same semaphore. Repro (scratchpad/review5/repro_deadlock.py, `delegation_wait_seconds=3.0`): at `max_concurrent_tool_calls=1` the trace shows `agent_delegate` acquires the semaphore, then `echo` arrives with `sem=[locked]`; the parent times out and the effect rows are `('agent_delegate','failed'), ('echo','succeeded')` with the parent's final answer wrong. Control run at cap=2 gives `('agent_delegate','succeeded'), ('echo','succeeded')` and the correct answer. Generally, deadlock whenever concurrently-waiting delegations reach the cap. `max_concurrent_tool_calls` has zero test coverage (`grep -rn max_concurrent_tool_calls tests/` is empty); the BA35 test only exercises the model cap. Note the other checks in this area are clean: asyncio.Semaphore is FIFO on this 3.14.7 interpreter and does not leak a permit on a cancelled acquire (scratchpad/review5/fifo.py), `async with` releases on exception, and the `_reject_with` path correctly takes no permit.",
      "fix": "Release the tool permit around the delegate's condition-wait — e.g. have `ExposureGuardedTool` skip the semaphore for tools that declare themselves 'waiting' (a spec flag), or have `AgentDelegateTool.handle` acquire/release explicitly around only its own work and drop the permit before the poll loop. Whatever the mechanism, add a regression test with `max_concurrent_tool_calls=1` plus a delegation whose child calls a tool."
    },
    {
      "id": "M2-validate-drops-catalog-and-audit-schema-checks",
      "severity": "P2",
      "file": "src/simple_harness/execution/sqlite/base_agent/migration.py:70",
      "evidence": "The module docstring claims it 'Mirrors short_context_migration.migrate_execution_to_v9', but `_validate` keeps only descriptor-row membership + `integrity_check`/`foreign_key_check` + a one-table partial-library probe. It drops the precedent's `_catalog(connection) != _catalog(expected)` comparison against a rebuilt reference schema and `audit_schema.validate_audit_schema` (short_context_migration.py:99-107). In the M1 repro an extra `host_precious_marker` table was added to the v9 source and the upgrade accepted it silently. The partial-library probe checks only `base_agent_bindings_v1`, not the other three v10 tables (collisions on those would only be caught later by the DDL failing, which does roll back).",
      "fix": "Add the catalog comparison against a freshly built `legacy_v9_descriptor()` reference (and the audit-schema validation) to `_validate`, or drop the 'mirrors' claim from the docstring and state the reduced guarantee explicitly."
    },
    {
      "id": "E1-escalation-retry-ignores-durable-cancel-and-drops-the-token",
      "severity": "P2",
      "file": "src/simple_harness/agents/execution.py:409",
      "evidence": "The `continue` re-enters `while True`, but the durable cancel-intent read (`read_agent_turn_cancel`, execution.py:246) and the turn-deadline check are both *before* the loop, so a cancel issued during attempt N is not observed and the escalated provider call proceeds. Worse, the `finally: self.turn_cancellations.pop(turn_id, None)` runs on the way out of the try before `continue`, so the token a concurrent `cancel_turn` may have just fetched (agents/runtime.py:726) is discarded and a fresh one is installed on the next iteration — the in-process cancel is silently lost for that window. Everything else in the escalation loop checks out: `attempt` and `escalations` are locals of `start`, the cap is `min(output_cap*2, ceiling)` so it never exceeds `max_output_tokens_ceiling`, `_settle_failed_turn` keeps `provider_turns_reserved_total` so each failed invocation still consumes a provider-turn ordinal, and `_TurnCancelled`/`TerminationBudgetExceeded` `return` rather than looping.",
      "fix": "Re-read the durable cancel intent (and re-check the turn deadline) at the top of each `while True` iteration, and move the `turn_cancellations.pop` out of the per-attempt `finally` so the token lives for the whole turn."
    },
    {
      "id": "E2-escalation-record-lost-on-success",
      "severity": "P2",
      "file": "src/simple_harness/agents/execution.py:425",
      "evidence": "`escalations` is only serialized into `error_payload['output_cap_escalations']` on the final-failure path; the success path (`committed_outcome`, execution.py:458) carries no trace, so a turn that silently doubled its output cap one or more times is indistinguishable from one that did not. Also, `attempt` resets on every `start` invocation, so a turn resumed after an UNKNOWN/authorization wait gets a fresh escalation budget — the cap is per admission, not per turn as the F-BA-1 comment implies.",
      "fix": "Attach `output_cap_escalations` to the committed outcome too (or emit a run event per escalation), and persist the attempt counter on the turn row if the budget is meant to be per turn."
    },
    {
      "id": "A1-companion-outcome-identity-verified-no-defect",
      "severity": "P2",
      "speculative": true,
      "file": "src/simple_harness/agents/execution.py:487",
      "evidence": "Not a defect — recorded because it was the headline question. The staged outcome is byte-identical to `start`'s: `result_hash` covers only `AgentTurnResult.to_json()` (turn_id, agent_id, seq, state, public_output, usage_refs, delegation_count); `provider_turn_ordinal_from/to` are `**extra` on `AgentTurnOutcome` and are NOT hashed (runtime/agent_turn.py:62). `seq` matches because `turns.submit_input` injects `payload = {**continuation_payload, 'seq': record.seq}` (base_agent/turns.py:473) — the kernel's `signal_base_agent_input` payload has no seq of its own. `usage_refs` derives from the same `response` object. `delegation_count` cannot drift: the factory runs synchronously immediately before `checkpoint.cas` and `start` recomputes it after `loop.run` returns with no intervening await, and the final CAS only happens after a response with no tool calls, so no `base_agent_delegations_v1` row can be inserted in between. Legacy ReAct is unaffected: `final_companion` defaults to None and `react_checkpoint._write` passes `**extra = {}`, so `cas_react_checkpoint` is called without the kwarg at all. The residual risk is a `stage_result` raise inside the CAS transaction (turn already committed/failed): it rolls back the checkpoint too, leaving the ReAct checkpoint stuck in an in-flight phase, which `_check_turn_identity` would later turn into a permanent `base_agent_turn_identity_conflict` FAILED for the Agent. I could not construct a reachable path (a lease takeover trips `_require_runtime_lease` first, and `cancel_turn` does not write the phase), hence speculative — but the ordering is worth a guard.",
      "fix": "Optional hardening: have `_stage_companion`'s inner closure catch `UnitOfWorkConflict` from `stage_result` and re-raise a marker the loop can convert into a checkpoint-preserving failure, so a raise can never leave the run-level ReAct checkpoint wedged in an in-flight phase."
    }
  ],
  "verified_green": "6 failed, 367 passed, 3 skipped in 18.86s — all 6 failures (tests/execution/test_audit_schema.py::test_old_empty_execution7_gets_explicit_audit_schema_and_reopens, test_command_ingress.py::{test_fresh_v5_schema_and_command_admission_replay_conflicts,test_normal_open_rejects_v4_without_writing_any_bytes}, test_execution_schema_v3.py::test_fresh_v7_is_one_identity_with_conversation_tables, test_execution_v3_to_v4_migration.py::test_completed_null_continuation_resolves_unique_pair_and_preserves_facts, test_stage_audit_schema.py::test_exact_audit1_upgrades_atomically_and_readonly_never_upgrades) are in plans/2026-09-10-base-agent-phase1/baseline-known-failures.txt; tests/unit/contracts/test_public_api.py::test_public_api_matches_frozen_snapshot was baseline-red and is now green"
}
```

Two items from the brief came back clean and are not in the findings list:

- **v9 tolerance (topic 3)** — `_base_agent_tables_present()` is the only unguarded kernel-wide scan; every other base_agent UoW call in `kernel.py` (lines 2086, 3003, 3353, 3401, 3430) sits behind `driver_kind == BASE_AGENT_DRIVER_KIND`, which a v9 library can never have.
- **Public API (topic 6)** — `public-api.json` adds exactly `migrate_execution_to_v10` + `ExecutionBaseAgentUpgradeReceiptV1` to both the root and `simple_harness.runtime`, with no other drift; root `__all__` has 341 entries, no duplicates, and neither `agents` nor `simple_harness.agents` appears. The commit also rewrote the frozen `public-api-0.7.10.json` (10 Agent\* names added, `__version__` reordered), but that file is only used as `set(old) <= set(current)`, so the edit tightens rather than weakens the guard — flagging it only as a record-keeping note, not a finding.