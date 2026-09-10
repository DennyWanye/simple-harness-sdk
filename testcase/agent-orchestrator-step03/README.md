# Agent 编排框架 · 第 3 步（Planner 自动拆解并并行执行静态 DAG）测试归档

- 计划：`plans/2026-09-11-agent-orchestrator/step03/{plan,acceptance,journal}.md`
- 回归入口：`.venv/bin/python -m pytest -q -p no:cacheprovider tests/orchestrator`（step02 + step03 累计）
- 真实模型（opt-in）：`--run-real-provider tests/orchestrator/step03/test_real_provider_static_dag.py`（`SH_BASEURL/SH_APIKEY/SH_MODEL`；`ORCH_EVIDENCE_DIR` 指定证据目录）
- 演示：`python -m agent_orchestrator demo --scenario static-dag --provider fixtures --evidence-dir <dir> --max-concurrency 2`

| 验收 | 脚本 |
|---|---|
| S3-01 A→(B‖C)→D→E 正常执行（并行在途、D 等 B/C、下游含上游产物且 hash 一致、版本血缘、整合判定） | `test_static_dag_closure.py::test_s3_01_static_dag_runs_in_parallel_and_flows_artifacts_downstream`；`test_cli_static_dag.py`；真实模型 `test_real_provider_static_dag.py` |
| S3-02 环路整图拒绝、Planner 带反馈重提 | `test_task_graph.py::test_cycle_and_self_dependency_are_rejected`；`test_static_dag_closure.py::test_s3_02_cyclic_graph_is_rejected_whole_and_the_planner_reproposes` |
| S3-03 B 完成而 C 失败：B 不重做、C 修复、D 保持 BLOCKED | `test_static_dag_closure.py::test_s3_03_failed_sibling_repairs_alone_and_the_join_waits` |
| S3-04 两个 Orchestrator 同时领取 | `test_multi_scheduler.py::test_s3_04_two_orchestrators_share_the_work_without_double_execution` |
| S3-05 同一 Task 两个候选：先 PASS 者被接受、另一个 SUPERSEDED（取消回执、费用结算、迟到结果只记历史） | `test_static_dag_closure.py::test_s3_05_two_candidates_first_pass_accepted_second_superseded` |
| S3-06 局部全过、整体不达标 → `mission_criteria_unmet` | `test_static_dag_closure.py::test_s3_06_all_tasks_pass_but_the_mission_criterion_is_unmet` |
| S3-07 失去编排租约：新 owner 接管同一 turn 不重跑；执行者不可见 → LOST + 新 Attempt；已完成 Task 不重跑 | `test_multi_scheduler.py::test_s3_07a_*`、`test_s3_07b_*` |
| S3-08 预算不足：Σ 子预算 > 父预算图拒绝并说明维度；运行期 Task 预留失败 → Task FAILED、BLOCKED 任务不动；Mission 池耗尽不归罪 Task | `test_task_graph.py::test_duplicates_budget_sum_tools_and_shape`；`test_static_dag_closure.py::test_s3_08a_*`、`test_s3_08b_*`、`test_s3_08c_*` |
| 图校验（拓扑序、去重 D3-16、预算归一 D3-2'、兄弟 outputs 冲突 D3-7'）与原子图提交/解锁 | `test_task_graph.py`、`test_commit_graph.py` |
