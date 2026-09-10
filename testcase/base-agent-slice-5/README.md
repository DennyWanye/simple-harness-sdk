# BaseAgent Slice 5 · 并发、恢复、迁移与发布 · testcase 归档

- 验收标准：`plans/2026-09-10-base-agent-phase1/slice-5/acceptance.md`（AC1–AC9）
- 回归入口：`.venv/bin/python -m pytest -q tests/agents/test_slice5_recovery.py tests/execution/test_execution_v9_to_v10_migration.py`

| AC | 脚本 |
|---|---|
| AC1 BA31 | `test_slice5_recovery.py::test_crash_after_final_cas_never_regenerates_the_answer` + S1 `test_turn_finalize.py` |
| AC2 BA32 | `test_input_arriving_while_the_idle_drive_releases_is_not_lost` |
| AC3 BA33 | `test_stale_lease_holder_cannot_overwrite_the_new_owner` |
| AC4 BA34 | `test_request_ids_stay_unique_across_restarts_and_turns` |
| AC5 BA35 | `test_concurrency_caps_are_enforced_fairly_across_agents` |
| AC6 BA36 | `test_history_queries_are_bounded_tool_calls` |
| AC7 BA37 | `tests/execution/test_execution_v9_to_v10_migration.py` |
| AC8 BA39 | `test_lost_tool_response_is_never_blindly_retried` |
| AC9 BA40 | wheel 构建与干净 venv 安装证据见 journal §4 |
