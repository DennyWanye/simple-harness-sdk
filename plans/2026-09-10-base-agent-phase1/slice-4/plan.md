plan-status: finalized (主编排者依据 BA-v1.0 §6.2–§6.4、§7.6 拆片，2026-09-10)

# Slice 4 实施计划：AgentSession 混合召回与隔离

- 起点 `f26216a`（S3 实现）；实现与 S3 review 修复同提交 `586b70d`
- 上游规范：§6.2 HistoryIndex、§6.3 SQLite 与向量、§7.6 query 与召回；验收 BA20、BA23–BA29

## 任务

| 任务 | 内容 | 覆盖 |
|---|---|---|
| T1 schema + 派生索引 | v10 DDL 追加 `base_agent_session_vectors_v1`（按 embedding 指纹分代）、`base_agent_index_jobs_v1`（幂等、租约）；FTS5 两张派生表（trigram 走中文/长文本，unicode61+tokenchars 走标识符/路径）在运行时 `ensure_fts` 建立，不进冻结描述符（FTS 不可用可降级） | BA25 BA26 |
| T2 EmbeddingPort | `agents/memory/embedding.py`：真实模型由调用方注入；`HashEmbedder` 指纹带 `mock`，不作语义证据 | BA24 BA26 |
| T3 检索 | `SessionRetriever.search_sync`：精确 seq/标识符 → words FTS → trigram FTS → 本 Agent 当前指纹向量 cosine → RRF → 分数下限（允许零召回）；`read()` 顺序分页 | BA23 BA24 BA27 BA29 |
| T4 索引任务 | `SessionIndexer.on_records`（FTS 行 + pending 向量任务）、`run_once`（回填缺向量的旧记录、线程内 embed、存向量、结算；失败带 error_code）；`AgentRuntime` 后台泵 + `index_pending()` | BA25 BA26 |
| T5 工具 | `session_history_search` / `session_history_read`：按 Run 解析 Agent，参数无 agent_id | BA23 BA29 |
| T6 召回注入 | `_RecallAdapter` 把命中变成派生 SYSTEM 消息（`recall/derived` 元数据）；`JournalContextPort.load` 先整预算装配，再用剩余份额装召回并重装配；selection 记 `query_hash`；召回不写 Journal、不再索引 | BA20 BA28 |
| T7 真实 embedding | `tests/agents/test_session_memory_real_embedding.py`（`--run-real-embedding`，Host venv 的 WeMM-Embedding-2B 子进程桥） | BA24 |
