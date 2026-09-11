# Agent 编排框架 · 第 4 步（团队共享知识、冲突仲裁与综合成果）测试归档

- 计划：`plans/2026-09-11-agent-orchestrator/step04/{plan,acceptance,journal}.md`
- 回归入口：`.venv/bin/python -m pytest -q -p no:cacheprovider tests/orchestrator`（step02 + step03 + step04 累计）
- 真实模型（opt-in）：`--run-real-provider tests/orchestrator/step04/test_real_provider_knowledge_sharing.py`（`SH_BASEURL/SH_APIKEY/SH_MODEL`；`ORCH_EVIDENCE_DIR` 指定证据目录）
- 演示：`python -m agent_orchestrator demo --scenario knowledge-sharing --provider fixtures --evidence-dir evidence/s4 --max-concurrency 1`（证据目录含 `baseline.json`、`events.jsonl`、`final_state.json`、`artifacts/`、`verification.json`、`costs.json`、`test-report.json`、`knowledge.json`、`lineage.json`）

| 验收 | 脚本 |
|---|---|
| S4-01 A 的 Claim 经机器验证 VERIFIED 后 B 复用（intent 冻结 id/version、`used_knowledge`、`used_by`、`KnowledgeUsed`、血缘） | `test_knowledge_sharing_closure.py`；`test_claims_knowledge.py::test_used_knowledge_is_a_checked_reference_and_records_the_reuse_chain` |
| S4-02 只有自信无验证 → 至多 SUPPORTED、不进知识表、Worker 包不显示、引用即 FAIL；整树 pytest 不覆盖 Claim | `test_claims_knowledge.py::test_claims_are_graded_*`、`::test_a_whole_tree_run_covers_no_claim`；`test_retrieval_context.py::test_worker_package_*`、`::test_verifier_and_critic_templates_*`、`::test_s4_02_*` |
| S4-03 相反 Claim：双方保留、DISPUTED、冲突优先于分级、Conflict Task 入图、意见被拒重试、外部检查 + Critic 后 `ConflictResolved`（非投票）、无预留时 DEFERRED | `test_conflicts.py`（6 条，含 `test_s4_03_*` 闭环） |
| S4-04 SUPERSEDED：显式取代、低可信不能取代、检索标记、引用旧版 FAIL 且给出新版、血缘可追 | `test_claims_knowledge.py::test_s4_04_*`；`test_retrieval_context.py::test_ranking_*` |
| S4-05 综合任务依赖全部叶子、被 OPEN 冲突门控、accept 内守卫、产物再验收失败不 Commit、`used_knowledge` 必须引用 VERIFIED | `test_synthesis.py`（4 条，含 `test_s4_05_*` 闭环）；`test_knowledge_sharing_closure.py` |
| S4-06 两个 Mission 知识互不可见、每个 Attempt 新 BaseAgent | `test_retrieval_context.py::test_s4_06_*`；`test_claims_knowledge.py::test_knowledge_never_crosses_the_mission_boundary` |
| S4-07 检索失败：block（事件、计数持久、超限 Task/Mission FAILED `retrieval_unavailable`）、degrade（包内显式 unavailable）、开关 `knowledge_sharing=False` | `test_retrieval_context.py::test_s4_07_*` |
| S4-08 外部内容：网关标记 `untrusted_external`、未授权工具被拒、`status=VERIFIED` 的信封被拒、仅引用外部文档的 Claim 不支持、上下文包不内联 | `test_retrieval_context.py::test_s4_08_*`；`test_knowledge_sharing_closure.py`；`test_cli_knowledge_sharing.py` |
| schema v1→v2 迁移（备份、历史行保留）、L3-2 产物版本唯一与重复投递幂等 | `test_schema_migration.py`、`test_artifact_versions.py` |
| accept 内 `used_knowledge` 二次校验（TOCTOU） | `test_claims_knowledge.py::test_accept_rechecks_used_knowledge_inside_the_commit` |
