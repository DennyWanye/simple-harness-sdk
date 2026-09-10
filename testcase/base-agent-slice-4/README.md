# BaseAgent Slice 4 · AgentSession 混合召回与隔离 · testcase 归档

- 验收标准：`plans/2026-09-10-base-agent-phase1/slice-4/acceptance.md`（AC1–AC9）
- 回归入口：`.venv/bin/python -m pytest -q tests/agents/test_session_memory.py`
- 真实 embedding：`tests/agents/test_session_memory_real_embedding.py --run-real-embedding`（Host venv 的 WeMM-Embedding-2B；报告 `plans/.../slice-4/reports/`）

| AC | 脚本 |
|---|---|
| AC1 BA23 | `test_agent_a_secret_never_reaches_agent_b` |
| AC2 BA24 | `test_identifier_and_chinese_queries_hit_through_lexical_paths` + 真实 WeMM |
| AC3 BA25 | `test_degradations_are_explicit`、`test_fts_backfill_and_fts_partial_are_visible`、`test_error_jobs_are_retried_and_dimension_mismatch_is_visible` |
| AC4 BA26 | `test_vectors_of_another_embedding_generation_are_never_mixed` |
| AC5 BA27 | `test_zero_recall_is_allowed` |
| AC6 BA28 | `test_frozen_request_fingerprint_is_stable_across_index_updates`、`test_frozen_request_resume_does_not_re_run_recall` |
| AC7/AC9 BA29 | `test_session_history_tools_page_and_stay_in_scope` |
| AC8 BA20 | `test_recall_is_derived_not_journal_and_frozen_request_keeps_it` |
