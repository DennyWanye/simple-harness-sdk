# Slice 3 执行日志：有界 Context 与 Journal（2026-09-10）

- 起点 `8b11094`（S2 已交付）；实现 `a5ab917`，review 修复 `586b70d`（与 S4 同一提交）
- 方法同 S2：测试先行、独立 review、真实端点、回归 ⊆ 基线，不走 plan-test 仪式

## 1. 设计要点（与 plan.md 一致）

- 唯一改动面是 `ContextPort`：`JournalContextPort.append` = Journal 一行/消息，`load` = 有界装配；wire 的 `RequestGuard` 对最终请求复计数。`react_loop.py` / `termination.py` / `start_snapshot.py` / legacy `SqliteContextPort` 一行未改。
- Journal 只存五种原始记录（instructions / user_input / assistant / tool_result / feedback）；摘要与召回是派生消息，只出现在请求里（BA20/BA21）。
- 装配算法：必需（指令 + 当前输入 + 尾部协议组）→ 旧闭合单元从新到旧整单元装入 → 折叠摘要（先预留后装入）→ 真实请求复计数。

## 2. 执行记录

| 任务 | 交付 | 测试 |
|---|---|---|
| T1 schema | journal / appends / selections / summaries 四表（v10 DDL 追加），v7/v8/v9 checksum 不变 | `test_base_agent_schema_v10.py` |
| T2 tokenizer + 预算 | `TokenizerPort`、`UpperBoundTokenizer`（显式上界指纹）、`TiktokenTokenizer`（可选依赖 tiktoken，已装入 dev venv）、`ContextPolicy.input_budget()`、`policy_hash` | `test_policy_hash_and_counts_are_not_reused_across_tokenizers` |
| T3 Journal ContextPort | 追加按 append_id 模式分类（协议组 = `{run}:provider-turn:N`）；有界读取（指令 + 尾部分页 + 组头补齐 + 当前输入按身份取）；协议组单元；结构性摘要 | `test_append_is_one_row_per_message_and_never_rewrites`、`test_protocol_groups_are_never_split_and_rotate_within_a_long_turn`、`test_originals_are_exactly_readable_after_window_rotation` |
| T4 wire 复计数 | `RequestGuard`：超预算 / `required_over_budget` → `ContextRequiredContentTooLarge`（definite failure）；request_id/request_hash 绑到 selection | `test_required_content_too_large_fails_before_any_provider_call`、`test_real_tokenizer_keeps_every_request_within_budget` |
| T5 大工具结果 + 摘要来源 | 原文行 `journal_only` + 预览行 `context`（`journal_read_back.seq`）；`BaseAgent.journal()/read_journal_record()`；摘要行 from/to seq + source_hash | `test_large_tool_result_is_externalized_with_a_read_back_reference`、`test_summary_is_derived_and_names_its_sources` |
| T6 真实模型 | `tests/agents/test_context_real_provider.py`（opt-in） | §4 |

## 3. 独立 review（子代理，审 `8b11094..f26216a`）与处置（`586b70d`）

| # | 发现 | 处置 | 测试 |
|---|---|---|---|
| S3-01 P0 | 摘要在装入之后才计费，预算接近满时装配超预算 → guard 拒发 → Agent 之后每轮都失败 | 摘要费用**先预留**（迭代到稳定），仍装不下时用紧凑摘要，再不行只留在 selection 里；加不变量：非 `required_over_budget` 的装配 ≤ 预算 | `test_summary_is_reserved_before_admission_and_never_overfills` |
| S3-02 P1 | 单轮超过 128 条记录时当前输入落在分页窗口之外被静默丢失 | 当前输入改为**按身份**读取（最新 user_input 行）并强制并入 | `test_current_input_is_present_even_when_a_turn_outgrows_the_read_window`（70 次工具调用，每个请求都含输入） |
| S3-03 P1 | wire 按**位置**恢复 tool_calls，轮换后错位 → 参数被抹成 `{}` | 装配时给 assistant 消息盖 `provider_turn_ordinal`，wire 按序号取账本组（无标记时保持旧的位置法） | 轮换测试断言 `fallback_total == 0` |
| S3-04 P2 | append 也做整份装配 | append 只返回 CAS 锚点；`_tool_calls_overhead` 按协议组缓存 | — |
| S3-05 P2 | selection id 只含 highwater，同 highwater 不同请求撞 id | id 追加装配内容 hash；`bind_selection_request` 检查 rowcount | — |
| S3-06 P2 | selection / summary 写入不过租约围栏 | **未改**：`ContextPort.load` 协议无租约参数；两表都是派生证据、非权威；记为遗留 L3-1 | — |
| S3-07 P2 | 分页边界可能切断协议组 | 读取后向前补齐最旧协议组的头；`build_units` 丢弃开头不完整的组 | 覆盖于轮换测试的 `_groups_are_whole` |

## 4. 真实端点（DeepSeek `deepseek-v4-pro`，`--run-real-provider`）

`tests/agents/test_context_real_provider.py`：小预算多轮，断言模型自报 `usage.input_tokens ≤ B_input`。
- T10 前的 4 次（预算 1900 / 2400）：全部 committed，模型自报输入 98–1032 tokens，均 ≤ 预算；其中 1 次观察到轮换（dropped [2,5]）。
- review 修复后的复跑见 §7（回填）。

## 5. 兑现表

| AC | 脚本 | 结果 |
|---|---|---|
| AC1 BA13 | `test_real_tokenizer_keeps_every_request_within_budget` + 真实端点 | PASS |
| AC2 BA14 | `test_tool_schemas_count_against_the_budget` | PASS |
| AC3 BA15 / AC5 BA17 | `test_protocol_groups_are_never_split_and_rotate_within_a_long_turn` | PASS |
| AC4 BA16 | `test_required_content_too_large_fails_before_any_provider_call` | PASS |
| AC6 BA18 | `test_originals_are_exactly_readable_after_window_rotation`、`test_large_tool_result_is_externalized_with_a_read_back_reference` | PASS |
| AC7 BA19 | `test_append_is_one_row_per_message_and_never_rewrites` | PASS |
| AC8 BA22 | `test_policy_hash_and_counts_are_not_reused_across_tokenizers` | PASS |
| AC9/AC10 BA20/BA21 | `test_summary_is_derived_and_names_its_sources` | PASS |
| AC11 §7.4 | `test_unknown_resume_reuses_the_frozen_request_without_a_new_selection` | PASS |

## 6. 遗留

| # | 事项 | 归属 |
|---|---|---|
| L3-1 | selection / summary 行不过租约围栏（`ContextPort.load` 无租约参数） | S5 或后续（可在 selection 行记 lease epoch） |
| L3-2 | `UpperBoundTokenizer` 对英文过估 ≤2×；真实模型请注入真实 tokenizer | 文档口径 |
| L3-3 | 语义摘要（模型生成）未做，只有结构性摘要 | S5/后续 |

## 7. 终态

- review 修复后真实端点复跑：**2/2**（预算 1400；两次都观察到轮换 dropped [2,7] / [2,3]；run 2 里两轮 `finish_reason=length` 被输出上限倍增重试透明吸收），报告 `reports/real-context-run{1,2}.txt`（review 前的 4 次在 `*-pre-review.txt`）。
- **F-BA-1 根因闭环**（2026-09-10）：DeepSeek `deepseek-v4-pro` 把整个 `max_output_tokens` 花在 reasoning 上（`finish_reason=length`，`reasoning_tokens == output_tokens`），正文为空。处置：wire 抛 `provider_empty_response`（带 finish_reason/usage detail）；driver 在同一 AgentTurn 内把输出上限倍增重试（`empty_response_retries=2`，`max_output_tokens_ceiling=8192`），失败的 invocation 留在账本；重试耗尽才可见失败。测试 `test_length_exhausted_reasoning_escalates_the_output_cap_then_succeeds`、`test_output_cap_escalation_is_bounded_and_fails_visibly`。
- **VERDICT: SHIPPED**。套件（agents + schema + contracts）全绿；回归 73 红 ⊆ 基线；mypy 0；冻结文件未改（`react_loop.py` 在 S5 加了可选 companion 参数，见 S5 journal）。

