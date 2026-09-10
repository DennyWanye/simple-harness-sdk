# Agent 编排框架 · 第 2 步（单 Task Mission 可靠验收闭环）测试归档

- 计划：`plans/2026-09-11-agent-orchestrator/step02/{plan,acceptance,journal}.md`
- 回归入口：`.venv/bin/python -m pytest -q -p no:cacheprovider tests/orchestrator`
- 真实模型（opt-in）：`--run-real-provider tests/orchestrator/step02/test_real_provider_single_task.py`（`SH_BASEURL/SH_APIKEY/SH_MODEL`；`ORCH_EVIDENCE_DIR` 指定证据目录）
- 演示：`python -m agent_orchestrator demo --scenario single-task --provider fixtures --evidence-dir <dir>`

| 验收 | 脚本 |
|---|---|
| S2-01 正常闭环 | `test_single_task_closure.py::test_s2_01_and_s2_02_repair_then_pass`；`test_cli_demo.py::test_demo_single_task_on_fixtures_writes_evidence`；真实模型 `test_real_provider_single_task.py` |
| S2-02 有错代码 → 修复 Attempt | 同上（第一次提交故意失败 `test_empty`，第二次修复） |
| S2-03 重放不重复 | `test_single_task_closure.py::test_s2_03_replayed_mission_is_the_same_mission`；`test_recovery_matrix.py::test_s2_03_replays_do_not_duplicate`；`test_commit_service.py::test_task_proposal_is_checked_and_replays_the_same_receipt` |
| S2-04 Agent 创建后崩溃 | `test_recovery_matrix.py::test_s2_04_*`（after_agent_created / after_submit；planner 与 worker 各一） |
| S2-05 结果提交后崩溃 | `test_recovery_matrix.py::test_s2_05*`（after_result_submitted / after_turn_committed / after_layer_pass） |
| S2-06 预算/次数耗尽 | `test_single_task_closure.py::test_s2_06_max_attempts_stops_with_reason`；`test_commit_service.py::test_failed_verification_retry_and_stop_paths` |
| S2-07 无效/伪造结果 | `test_single_task_closure.py::test_s2_07_invalid_and_forged_envelopes_are_rejected`；`test_commit_service.py::test_reject_result_and_cancel_from_verifying` |
| S2-08 UNKNOWN 保持阻塞 | `test_recovery_matrix.py::test_s2_08_unknown_provider_outcome_stays_blocked_with_reservation_held` |
| 合同/状态机/存储/预算/工具网关 | `test_contracts.py`、`test_store_and_budgets.py`、`test_workspace_and_gateway.py` |
| review round 1 回归（受保护种子、活租约重启、停滞超时、cancel 清理、priced 结算、模型回显、mid_commit） | `test_review_round1.py` |
| D21 判定时自跑 Critic | `test_single_task_closure.py::test_d21_mission_judgment_runs_its_own_critic_when_the_task_policy_had_none` |
