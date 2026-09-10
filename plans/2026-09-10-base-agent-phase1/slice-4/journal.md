# Slice 4 执行日志：AgentSession 混合召回与隔离（2026-09-10）

- 实现 `586b70d`（与 S3 review 修复同提交）；review 修复 `5fb33cb`；回归测试 `1cb8406`
- 方法同 S2/S3

## 1. 设计要点

- 派生索引不进冻结描述符：FTS5 两张表（trigram 走中文/长文本，unicode61+tokenchars 走标识符/路径，索引时额外写入路径/点号分段）运行时 `ensure_fts` 建立、可回填；向量表按 embedding 指纹分代。
- 检索：精确 seq → words FTS → trigram FTS（OR 三元组窗口，bm25 排序）→ 本 Agent 当前指纹向量 cosine → RRF 融合 → 分数下限（允许零召回）；所有 SQL 以 agent_id 过滤，工具无 agent_id 参数。
- 召回是派生 SYSTEM 消息（`recall/derived` 元数据），只进请求，不进 Journal、不再索引（BA20）；先整预算装配，召回只占必需部分留下的空间（份额上限）。
- 真实 embedding：Host venv 的 WeMM-Embedding-2B（2048 维）经子进程桥，opt-in `--run-real-embedding`。

## 2. 独立 review（子代理，审 `f26216a..586b70d`）与处置（`5fb33cb`、`1cb8406`）

隔离面（BA23）review 判定干净：FTS MATCH 与 agent_id 同时过滤、向量/Journal 读取都按 agent 参数化、候选逐条核对 hash、工具按 Run 解析 Agent、委派子 Agent 有自己的 run_id。

| # | 发现 | 处置 |
|---|---|---|
| S4-01 P1 | `index_partial` 把 `journal_only` 行也算进去，永远为真 | 大工具结果原文行也索引（S4-13），覆盖判定与索引器同一谓词 |
| S4-02 P1 | FTS 行与 Journal 行不在同一事务，崩溃/无 FTS5 构建留下的缺口无人回填 | 泵定期回填缺 FTS 行；`fts_partial` 降级可见 |
| S4-03 P1 | error 任务永不重试，且错误码不到结果 | `claim` 也取 `attempts < 5` 的 error 任务；`index_error:<code>` 进 degradations |
| S4-04 P1 | 召回在事件循环线程上同步 embed、全量读 Journal | driver 在进 loop 前 `prepare()` 用线程预嵌入查询并缓存；检索窗口 2000 条 |
| S4-05 P1 | 冻结请求恢复时重跑召回、可能新增 selection 行 | 同 revision 已绑定请求 → `load` 不召回不记录；assembly hash 含召回载荷 |
| S4-06 P1 | `index_pending` 与后台泵抢同一批任务，输家 settle 冲突外抛 | 泵与排空共用锁；lost lease 视为良性 |
| S4-07 P2 | 回填查询每 0.25 s 全表扫 | 回填按间隔（5 s）或显式 `request_backfill` |
| S4-08 P2 | 维度不一致静默 0 分 | 存向量前校验维度（`embedding_dim_mismatch`），检索时可见降级 |
| S4-09 P2 | 召回插入按角色分区会挪动中途的 SYSTEM 消息 | 只插在开头连续 SYSTEM 之后 |
| S4-10 P2 | wire 位置游标在有序号标记时也递增 | 只在位置分支递增；fallback 名 `unknown` |
| S4-11 P2 | `index_generation` 列 INTEGER 死列 | 改 TEXT，记录 embedding 指纹 |
| S4-12 P2 | `fts_search` 表名未校验 | 白名单 |
| S4-13 P2 | 大工具结果原文不可检索 | 原文行入索引，命中文本截断 4000 字 |

## 3. 真实证据

- `tests/agents/test_session_memory_real_embedding.py --run-real-embedding`：中文语义改写（与记录无共享三元组）命中正确记录且来源仅 `vector`；英文路径/函数名查询命中且来源含 `words/trigram/vector`；`index_partial=False`。报告 `reports/real-embedding-run1.txt`。

## 4. 兑现表

| AC | 脚本 | 结果 |
|---|---|---|
| AC1 BA23 | `test_agent_a_secret_never_reaches_agent_b` | PASS |
| AC2 BA24 | `test_identifier_and_chinese_queries_hit_through_lexical_paths` + 真实 WeMM | PASS |
| AC3 BA25 | `test_degradations_are_explicit`、`test_fts_backfill_and_fts_partial_are_visible`、`test_error_jobs_are_retried_and_dimension_mismatch_is_visible` | PASS |
| AC4 BA26 | `test_vectors_of_another_embedding_generation_are_never_mixed` | PASS |
| AC5 BA27 | `test_zero_recall_is_allowed` | PASS |
| AC6 BA28 | `test_frozen_request_fingerprint_is_stable_across_index_updates`、`test_frozen_request_resume_does_not_re_run_recall` | PASS |
| AC7 BA29 / AC9 | `test_session_history_tools_page_and_stay_in_scope` | PASS |
| AC8 BA20 | `test_recall_is_derived_not_journal_and_frozen_request_keeps_it` | PASS |

## 5. 遗留

| # | 事项 | 归属 |
|---|---|---|
| L4-1 | 向量检索是纯 Python 暴力 cosine（窗口 2000 条），大会话需要 sqlite-vec 之类 adapter | 后续 |
| L4-2 | 语义摘要、query 改写（Query Agent）未做，query = 当前输入原文 | 后续 |
| L4-3 | 真实 embedding 走 Host venv 子进程（每批 ~10 s 加载），SDK 内无模型 | 设计如此 |

## 6. 终态

**VERDICT: SHIPPED**（套件全绿；回归 73 红 ⊆ 基线；review 全部处置；真实 embedding 1/1）。
