# Agent 编排框架 · 第 9 步（从历史学习、受控晋级新策略）测试归档

- 计划：`plans/2026-09-11-agent-orchestrator/step09/{plan,acceptance,journal}.md`；plan 评审 `step09/reports/plan-review-round1.md`
- 回归入口：`.venv/bin/python -m pytest -q -p no:cacheprovider tests/orchestrator`（step02–09 累计）
- 真实模型（opt-in）：`--run-real-provider tests/orchestrator/step09/test_real_provider_policy.py`（`SH_MODEL=deepseek-flash`）；运行记录 `plans/.../step09/reports/real-policy-gate-run1.md`
- 演示：`python -m agent_orchestrator demo --scenario policy-promotion --provider fixtures --evidence-dir evidence/s9`（新目录；`history/` 六个历史 Mission → `production/` 正式库：规则改进器提出候选、门槛评测（`evaluations/`）、批准、晋级、回滚；`summary.json`、`registry.json`、`policy_events.jsonl`、`production/missions/<id>/` 证据）
- 人工使用：`python -m agent_orchestrator policy status --evidence-dir <正式库目录>`；`policy propose --params p.json --as <人> --evidence-dir …`；`policy evaluate <提议> --eval-dir <新目录> --evidence-dir …`；`policy approve|promote|rollback … --as <人>`

| 验收 | 脚本 |
|---|---|
| S9-01 候选可追溯到训练数据、代码、参数和评测版本 | `test_policy_learning.py::test_s9_01_*`、`test_policy_registry.py::test_s9_01_*`、`test_policy_promotion_closure.py` |
| S9-02 未通过门槛不上线、保留理由、旧策略继续 | `test_policy_gates.py::test_s9_02_*`、`test_policy_registry.py::test_s9_02_*` |
| S9-03 合格未批准只在评测库运行、不改变正式 Mission | `test_policy_gates.py::test_s9_03_*` |
| S9-04 审批后上线、新 Mission 绑定新版本、在途 Mission 不被静默改策略 | `test_policy_binding.py::test_s9_04_*`（含结构测试"无决策点读白名单配置"、配置换了恢复仍用绑定版本） |
| S9-05 快速回滚至已批准版本、既有事件与费用保留 | `test_policy_registry.py::test_s9_05_*`、`test_policy_binding.py::test_s9_05_*` |
| S9-06 在线 Agent 提出改安全阈值被拒、人工审查出路 | `test_policy_guard.py::test_s9_06_*` |
| S9-07 数据不足 / 污染 / 泄漏明确拒绝晋级、不出虚假提升结论 | `test_policy_learning.py::test_s9_07_*`、`test_policy_gates.py::test_s9_07_*` |
| S9-08 高负载下调整有边界、安全 / 预算上限始终有效 | `test_policy_guard.py::test_s9_08_*`、`test_policy_registry.py::test_s9_08_*` |
| 版本库 / 绑定的一致性（事件折叠投影 = 表、库角色、解释器漂移、v5 迁移） | `test_policy_registry.py`、`test_policy_binding.py` |
