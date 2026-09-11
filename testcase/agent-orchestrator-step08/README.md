# Agent 编排框架 · 第 8 步（贡献归因、Replay 与策略对照评测）测试归档

- 计划：`plans/2026-09-11-agent-orchestrator/step08/{plan,acceptance,journal}.md`；事件覆盖探查 `step08/reports/event-coverage-probe.md`
- 回归入口：`.venv/bin/python -m pytest -q -p no:cacheprovider tests/orchestrator`（step02–08 累计）
- 真实模型（opt-in）：`--run-real-provider tests/orchestrator/step08/test_real_provider_evaluation.py`（`SH_MODEL=deepseek-flash`）；运行记录 `plans/.../step08/reports/real-evaluation-run1.md`、`real-evaluation-run2.md`（评审修复之后）
- 演示：`python -m agent_orchestrator demo --scenario evaluate-policies --provider fixtures --evidence-dir evidence/s8`（新目录；4 个 case × 完整政策 / 去掉 Critic × 2 次试验，每次运行一个新 Mission 与新库；`evaluation.json`、中文 `evaluation.md`、`samples.json`：一次成功运行的归因、一次失败运行的回放）
- 人工使用：`python -m agent_orchestrator replay --evidence-dir <运行目录> <mission_id> --attribution --failures`（只读）；`python -m agent_orchestrator evaluate --plan plan.json --evidence-dir <新目录>`

| 验收 | 脚本 |
|---|---|
| S8-01 已完成 Mission 的最终产物依赖链与费用归属（路径内 / 探索 / 服务、未归类为空、未定价为 null） | `test_attribution.py::test_s8_01_*` |
| S8-02 回放得到相同正式状态（四个演示、失败、审批被拒、取消开放动作、人工审核挂起与通过，覆盖率 100% 且 0 不一致）；重复投递不变；崩溃前缀；不写库、不外调、不导入 runtime、只读目录可用 | `test_replay.py::test_s8_02_*` |
| S8-03 两个策略在同一组 case、同一预算边界下比较；新 Mission / 新库；样本量、Wilson 区间、Fisher 检验；fixture 标"不适用"；harness_error 不计入分母；oracle 误判率 | `test_evaluation.py::test_s8_03_*` 等、`test_evaluate_policies_closure.py` |
| S8-04 消融 Critic / Blackboard：关闭项与可观察变化；安全边界与策略越权被拒 | `test_ablation.py`、`test_evaluation.py::test_s8_04_*` |
| S8-05 Trace 不完整：覆盖率 < 100%、缺口与缺失事件逐项列出、不回填；归因断点不补边 | `test_replay.py::test_s8_05_*`、`test_attribution.py::test_s8_05_*` |
| S8-06 新模型重跑旧任务：新 Evaluation、新成本、旧库不变；篡改的 charter 拒绝派生 | `test_evaluation.py::test_s8_06_*` |
| S8-07 版本差异与配置来源：全字段归类、同配置同哈希、差异带来源、版本常量改变、演示证据无漂移 | `test_policy_snapshot.py` |
| 代码评审第 1 轮修复（`plans/.../step08/reports/code-review-round1.md`；处置表见 journal §1） | P0-1 `test_evaluation.py::test_review_p0_1_*`；P1-1 / P1-4 `test_attribution.py::test_review_p1_*`；P1-2 `test_replay.py::test_review_p1_2_*`；P1-3 `test_ablation.py::test_review_p1_3_*`；P1-5 / P2-1 `test_evaluation.py::test_review_p1_5_*`、`test_review_p2_1_*`；P2-5 `test_replay_evaluate_cli_errors.py` |
| 代码复核第 2 轮修复（`reports/code-review-round2.md`；处置表见 journal §1 第 2 轮） | `test_replay.py::test_re_review_*`（被拒后重试完整回放、新建 Attempt 使 Task ACTIVE）；`test_evaluation.py::test_re_review_*`（测试服务类型与目录）；`test_attribution.py::test_re_review_*`（被仲裁取代知识的来源为探索、未结算用量不算对账） |
