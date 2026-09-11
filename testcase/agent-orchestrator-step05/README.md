# Agent 编排框架 · 第 5 步（根据 Worker 返回动态修改 Task DAG）测试归档

- 计划：`plans/2026-09-11-agent-orchestrator/step05/{plan,acceptance,journal}.md`
- 回归入口：`.venv/bin/python -m pytest -q -p no:cacheprovider tests/orchestrator`（step02–05 累计）
- 真实模型（opt-in）：`--run-real-provider tests/orchestrator/step05/test_real_provider_dynamic_dag.py`（`SH_MODEL=deepseek-flash`）
- 演示：`python -m agent_orchestrator demo --scenario dynamic-dag --provider fixtures --evidence-dir evidence/s5 --max-concurrency 1`（证据含 §14.3 七个文件 + `graph_history.json` + `lineage.json`）

| 验收 | 脚本 |
|---|---|
| S5-01 Worker 提出子任务只经 Manager 的 Proposal/Commit 入图（§7.4 全流程） | `test_manager_decisions.py::test_s5_01_*` |
| S5-02 反复无进展：空提案 → `no_progress` 停止；换角色（simplifier）继续 | `test_manager_decisions.py::test_s5_02_*` |
| S5-03 两个 Manager 同一基版本：不相交自动重基、相交拒绝 | `test_graph_changes.py::test_s5_03_*` |
| S5-04 环 / 目标漂移拒绝，正式图不变 | `test_graph_changes.py::test_s5_04_*` |
| S5-05 改 B 分支：A/D 不重跑不重扣费，C 在位改依赖，Mission 用 v2 完成 | `test_dynamic_dag_closure.py::test_s5_05_and_s5_06_*` |
| S5-06 被替代 Worker 迟到提交只记历史、费用归账 | 同上 |
| S5-07 同一提案/同一触发只生效一次 | `test_graph_changes.py::test_s5_07_*`、`test_manager_decisions.py::test_s5_07_*` |
| S5-08 深度 / 单 Agent 建议数 / 预算超限拒绝并反馈 | `test_graph_changes.py::test_s5_08_*`、`test_manager_decisions.py::test_s5_07_and_s5_08_*` |
| S5-09 低优先级任务因等待老化获得执行；§29.3 公式确定性；打分冻结在 Attempt | `test_allocator_priority.py` |
| §25.1 合法性（READY 不在位改依赖、COMPLETED 不替代、pause/resume/set_role 数据标记）、提案解析严格、替代链上限 | `test_graph_changes.py` |
| CLI `dynamic-dag` 证据目录 | `test_dynamic_dag_closure.py::test_demo_dynamic_dag_on_fixtures_writes_evidence` |
| 代码 review round 1 处置（P0-1 pause 合法性、P1-1 旧 id retarget、P1-2 两处 Manager 触发、P1-3 commit 前崩溃、P1-4 `AllocationDecided`、P2-7/8/9/4） | `test_step05_review_fixes.py` |
