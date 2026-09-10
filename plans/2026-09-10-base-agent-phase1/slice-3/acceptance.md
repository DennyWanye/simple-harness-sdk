# 验收标准：Slice 3 · 有界 Context 与 Journal

## 范围

包含：BA13–BA19、BA22（MUST 8 条）；BA20、BA21（次要）。
明确不包含：混合召回与 `session_history.search/read` 工具（S4）、语义摘要调用（S4/S5）、embedding（S4）、跨 Agent 并发限流（S5）。

## 功能验收条款

| AC | 条款 | 判据 | 地位 | 覆盖 | 脚本 |
|---|---|---|---|---|---|
| **AC1** | 真实 tokenizer 下最终请求在预算内 | tiktoken 计数：多轮（含工具）每个 provider 请求 ≤ `B_input + render_slack`；真实端点 `usage.input_tokens ≤ B_input` | 决定性 | BA13 | `test_context_journal.py::test_real_tokenizer_keeps_every_request_within_budget`、`test_context_real_provider.py`（opt-in） |
| **AC2** | 工具 schema / 角色说明 / 摘要都计入 | 同一对话，带大 schema 工具时 selection.tool_tokens>0 且选入的记录更少；两者最终计数都 ≤ 预算 | 必须 | BA14 | `test_tool_schemas_count_against_the_budget` |
| **AC3** | 协议组不拆开；当前输入不丢 | 每个请求：tool 消息前必有其 assistant；每个请求都含当前输入 | 决定性 | BA15 | `test_protocol_groups_are_never_split_and_rotate_within_a_long_turn` |
| **AC4** | 必需内容超预算调用前报错 | 指令超预算 → Turn `failed`、`error_code=context_required_content_too_large`、`detail.required_over_budget=True`、provider 0 次、Run WAITING | 决定性 | BA16 | `test_required_content_too_large_fails_before_any_provider_call` |
| **AC5** | 长 Turn 内轮换旧闭合组 | 一轮 6 次工具调用：最后一次请求不含 call-1、含 call-6、含折叠摘要 | 必须 | BA17 | 同 AC3 用例 |
| **AC6** | 原文先存、裁减后精确回读 | 轮换后 `journal()` 仍 9 条、`read_journal_record(2)` 原文逐字相等；大工具结果原文行 `journal_only`、预览行带 `journal_read_back.seq`、请求里没有原文 | 必须 | BA18 | `test_originals_are_exactly_readable_after_window_rotation`、`test_large_tool_result_is_externalized_with_a_read_back_reference` |
| **AC7** | 追加体量近似线性 | 4 轮同长输入，Journal 字节增量 max ≤ 1.25×min；`react.context.v1` 快照 0 行 | 必须 | BA19 | `test_append_is_one_row_per_message_and_never_rewrites` |
| **AC8** | 换 tokenizer/模型不沿用旧计数 | 三种 (tokenizer, model) 组合 policy_hash 互异；跨重启换 tokenizer 后 selection 的指纹、policy_hash、selection_id 都变 | 必须 | BA22 | `test_policy_hash_and_counts_are_not_reused_across_tokenizers` |
| AC9 | 召回/摘要不回灌原始历史 | Journal kind 集合 ⊆ 五种原始记录，折叠摘要文本不在 Journal | 次要 | BA20 | `test_summary_is_derived_and_names_its_sources` |
| AC10 | 摘要有来源、标 derived | 摘要消息 metadata 的 source_seq_from/to/source_hash 与 `base_agent_session_summaries_v1` 一行一致，validity=valid | 次要 | BA21 | 同上 |
| AC11 | 相同冻结请求恢复不重新装配 | UNKNOWN → reconcile 恢复后 selection 行数不变、provider 1 次 | 次要 | §7.4 | `test_unknown_resume_reuses_the_frozen_request_without_a_new_selection` |

## 非功能

- `react_loop.py` / `termination.py` / `start_snapshot.py` / 根包 API / `public-api.json` 逐字不变；v7/v8/v9 checksum 不变。
- 全片套件 `tests/agents + tests/execution/test_base_agent_schema_v10.py + tests/unit/contracts` 全绿；固定回归红集 ⊆ 基线；mypy（默认配置与 `src/simple_harness/agents` 包）0 issues。
- 独立代码 review，P0/P1 修完各配测试。

## 完成的定义

1. AC1–AC8 全部有测试并通过；AC9–AC11 各至少一条。
2. 真实端点 BA13 证据（≥2 次），脱敏报告归档 `reports/`。
3. 非功能表逐项有记录；journal（中文）含遗留清单。
