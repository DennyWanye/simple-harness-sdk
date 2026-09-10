# BaseAgent Slice 3 · 有界 Context 与 Journal · testcase 归档

- 验收标准：`plans/2026-09-10-base-agent-phase1/slice-3/acceptance.md`（AC1–AC11）
- 回归入口：`.venv/bin/python -m pytest -q tests/agents/test_context_journal.py tests/agents/test_provider_response_durability.py`
- 真实端点：`tests/agents/test_context_real_provider.py --run-real-provider`（需 tiktoken；报告 `plans/.../slice-3/reports/`）

| AC | 脚本 |
|---|---|
| AC1 BA13 | `test_context_journal.py::test_real_tokenizer_keeps_every_request_within_budget` + 真实端点 |
| AC2 BA14 | `test_tool_schemas_count_against_the_budget` |
| AC3/AC5 BA15/BA17 | `test_protocol_groups_are_never_split_and_rotate_within_a_long_turn`、`test_current_input_is_present_even_when_a_turn_outgrows_the_read_window` |
| AC4 BA16 | `test_required_content_too_large_fails_before_any_provider_call`、`test_summary_is_reserved_before_admission_and_never_overfills` |
| AC6 BA18 | `test_originals_are_exactly_readable_after_window_rotation`、`test_large_tool_result_is_externalized_with_a_read_back_reference` |
| AC7 BA19 | `test_append_is_one_row_per_message_and_never_rewrites` |
| AC8 BA22 | `test_policy_hash_and_counts_are_not_reused_across_tokenizers` |
| AC9/AC10 BA20/BA21 | `test_summary_is_derived_and_names_its_sources` |
| AC11 §7.4 | `test_unknown_resume_reuses_the_frozen_request_without_a_new_selection` |
| F-BA-1 | `test_provider_response_durability.py`（空正文可见失败、length 倍增重试、上限耗尽可见失败） |
