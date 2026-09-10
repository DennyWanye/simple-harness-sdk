plan-status: finalized (主编排者依据 BA-v1.0 §6–§7 与 program.md S2 拆片，2026-09-10；不走 plan-test 仪式)

# Slice 3 实施计划：有界 Context 与 Journal

- 仓库：`simple-harness-sdk`，起点 `8b11094`（S2 已交付）
- 上游规范：Host `plans/taskSys2/base-agent-phase1-plan.zh-CN.md` §6（AgentSessionMemory 四种表示）、§7（固定预算、装配算法、压缩与大工具结果）、§10.1（journal / selections / summaries 记录）
- 验收：同目录 `acceptance.md`

## 0. 主要矛盾与切入点

矛盾：`runtime/context.py` 每次 append 重写整份 messages 并把整份历史原样送给模型；而 BaseAgent 的 Turn 可能很长、会话跨重启存续，请求必须在真实 tokenizer 预算内。

切入点（一个改动面）：ReAct 循环在 `phase==ready` 时用 `services.context.load(run_id)` 组装请求（`react_loop.py:372-386`，`run_context_authority is None` 分支）。给 base_agent Run 换一个 `ContextPort` 实现即可：`append` 写 Journal 一行、`load` 做有界装配；wire 在发送前用同一 tokenizer 对最终渲染请求复计数。`react_loop.py` / `termination.py` 一行不改；legacy Run 继续用 `SqliteContextPort`。

## 1. 任务

| 任务 | 内容 | 覆盖 |
|---|---|---|
| T1 schema | v10 DDL 追加 `base_agent_session_journal_v1`（每消息一行，kind/turn_id/protocol_group_id/content_hash/provenance/visibility/full_record_seq）、`base_agent_journal_appends_v1`（append 幂等回执）、`base_agent_context_selections_v1`、`base_agent_session_summaries_v1`；v7/v8/v9 checksum 不变 | 数据最小集合 |
| T2 tokenizer + 预算 | `agents/context/tokenizer.py`（`TokenizerPort`、`UpperBoundTokenizer` 显式上界、`TiktokenTokenizer` 可选依赖）、`budget.py`（`ContextPolicy`，`B_input = min(max_input, total - output_reserve - safety)`，`policy_hash` 含 tokenizer 指纹与模型） | BA13 BA22 |
| T3 Journal ContextPort | `agents/context/port.py::JournalContextPort`：`append` = Journal 行（按 append_id 模式分类 kind/协议组）；`load` = 有界读取（指令 + 从尾部按页读到预算满）→ `protocol_groups.build_units` → `composer.assemble`（必需部分永不丢：指令、当前输入、尾部协议组；旧闭合单元从新到旧整单元装入；丢弃段落用结构性摘要替代）→ 记录 selection | BA14 BA15 BA17 BA19 |
| T4 wire 复计数 | `RequestGuard`：对恢复 tool_calls 后的最终请求用同一 tokenizer 计数，超预算或 selection 标记 `required_over_budget` → `ContextRequiredContentTooLarge`（definite failure，Turn 失败 `context_required_content_too_large`，provider 0 次）；把 request_id/request_hash 绑到 selection | BA13 BA16 |
| T5 大工具结果 + 摘要来源 | 超过 `max_tool_result_tokens` 的工具结果：原文行 `journal_only` + 预览行 `context`（含 `journal_read_back.seq`）；`BaseAgent.journal()/read_journal_record(seq)` 精确回读；摘要记录 from/to seq + source_hash，消息标 `derived` | BA18 BA20 BA21 |
| T6 真实模型 | `tests/agents/test_context_real_provider.py`（`--run-real-provider`）：DeepSeek 小预算 4 轮，`usage.input_tokens ≤ B_input` | BA13 |

## 2. 必须保留

- `react_loop.py`、`termination.py`、`start_snapshot.py`、根包 API、v7/v8/v9 checksum 不变；legacy `SqliteContextPort` 不改。
- `revision` 语义 = Journal high-water，ReAct 的 append CAS 纪律不变；相同冻结请求恢复时不重新装配（loop 直接用 checkpoint 里的请求快照）。
- 摘要与召回**不写回** Journal（BA20）；Journal 只有 instructions/user_input/assistant/tool_result/feedback 五种原始记录。
