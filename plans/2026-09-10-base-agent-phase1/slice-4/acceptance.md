# 验收标准：Slice 4 · AgentSession 混合召回与隔离

| AC | 条款 | 判据 | 地位 | 覆盖 | 脚本 |
|---|---|---|---|---|---|
| **AC1** | A 的秘密不进 B 的搜索与模型输入 | B 用原文检索零命中、A 命中；B 相关提问后所有请求不含秘密；向量表只有 A | 决定性 | BA23 | `test_session_memory.py::test_agent_a_secret_never_reaches_agent_b` |
| **AC2** | 中文语义改写 + 英文函数/路径查询各有实测 | mock：`session.py`/`refresh_token` 走 words 路命中、中文走 trigram、双字短词走精确路；真实 WeMM：中文改写（无共享三元组）命中且来源含 vector，英文路径查询命中 | 必须 | BA24 | `test_identifier_and_chinese_queries_hit_through_lexical_paths`、`test_session_memory_real_embedding.py`（opt-in） |
| **AC3** | FTS/embedding/索引滞后有明确降级 | `embedding_unavailable`、`embedding_service_down`（任务 error 带码）、`fts_unavailable`（子串路径仍命中）、`index_partial` 泵追上后消失；原文不删 | 必须 | BA25 | `test_degradations_are_explicit` |
| **AC4** | 维度/模型版本变更不混算 | 两代指纹各自成行、维度不同；新代未回填前 `index_partial`，回填后消失 | 必须 | BA26 | `test_vectors_of_another_embedding_generation_are_never_mixed` |
| **AC5** | 允许零召回 | 无关查询 hits 为空 | 必须 | BA27 | `test_zero_recall_is_allowed` |
| **AC6** | 冻结请求重试保留原选择 | UNKNOWN → 索引更新 → reconcile 重试，第二次请求指纹与第一次逐字相同 | 必须 | BA28 | `test_frozen_request_fingerprint_is_stable_across_index_updates` |
| **AC7** | 全集用分页不用 top-k | `session_history_read` 分页 `next_seq` 连续 | 必须 | BA29 | `test_session_history_tools_page_and_stay_in_scope` |
| **AC8** | 召回不回灌 | 召回消息带 `derived/recall`，Journal kind 集合不变，索引任务数 = 原始记录数 | 必须 | BA20 | `test_recall_is_derived_not_journal_and_frozen_request_keeps_it` |
| AC9 | 工具无 agent_id 参数 | schema 不含 agent_id | 次要 | §7.6 | 同 AC7 用例 |

非功能：冻结文件不变；全片套件与回归绿；mypy 0；独立 review。
