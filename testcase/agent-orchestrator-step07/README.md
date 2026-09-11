# Agent 编排框架 · 第 7 步（Human-in-the-loop 与受控真实操作）测试归档

- 计划：`plans/2026-09-11-agent-orchestrator/step07/{plan,acceptance,journal}.md`
- 回归入口：`.venv/bin/python -m pytest -q -p no:cacheprovider tests/orchestrator`（step02–07 累计）
- 真实模型（opt-in）：`--run-real-provider tests/orchestrator/step07/test_real_provider_approval.py`（`SH_MODEL=deepseek-flash`）；运行记录 `plans/.../step07/reports/real-approval-run1.md`
- 演示：`python -m agent_orchestrator demo --scenario approval-action --provider fixtures --evidence-dir evidence/s7`（一条命令走完候选 → 审批 → 交接 → 回执核对；证据含 §14.3 七个文件 + `actions.json` / `approvals.json` / `trace.json` / `metrics.json`，测试服务状态在 `test-services/config.json`）
- 人工操作：`--pause-for-approval` 让演示停在等待人工（退出码 4），然后
  `python -m agent_orchestrator approval list|approve|reject|revoke|comment|review|arbitrate|takeover|resolve --evidence-dir evidence/s7 --as <你的名字>`，再用同一个 `--idempotency-key` 重跑演示即可继续

| 验收 | 脚本 |
|---|---|
| S7-01 L2 候选未批准：没有真实修改，审批请求可查询、重启后仍在，`run()` 空闲返回、不重复判定 | `test_approvals.py::test_s7_01_*`、`test_waiting_view.py` |
| S7-02 批准指定版本：只执行一次，回执可核对，重复投递 / 重复循环不执行第二次 | `test_action_execution.py::test_s7_02_*`、`test_approvals.py::test_s7_01_*`（第二段） |
| S7-03 批准后内容变了：旧版本与批准 SUPERSEDED，新版本重新请求审批，执行只在判定最后一步 | `test_approvals.py::test_s7_03_*`、`test_action_ledger.py`（版本规则） |
| S7-04 拒绝 / 撤权 / 过期：不交接、服务零调用、Mission 以 `approval_rejected` 失败 | `test_approvals.py::test_s7_04_*`（×3）、`test_action_ledger.py`、`test_action_execution.py::test_hand_off_rechecks_*` |
| S7-05 L3 一次批准或同回执重放：不执行；两个不同 principal 批准后才执行 | `test_approvals.py::test_s7_05_*`、`test_action_ledger.py`（计数规则） |
| S7-06 执行成功但回执丢失：UNKNOWN、预留保持、按幂等键核对、不重发；崩溃恢复、恢复旧库、取消 Mission、超时 | `test_action_execution.py::test_s7_06_*` 等 |
| S7-07 人工审核挂起与恢复、needs_human、Verifier 冲突仲裁、接管与评论，范围不扩大 | `test_human_review.py` |
| S7-08 不可信文档要求自动批准 / 降级：审批不变、候选夹带字段被拒、没有审批工具 | `test_approvals.py::test_s7_08_*`、`test_action_ledger.py::test_scope_schema_*` |
| CLI `approval ...` 与 `approval-action` 证据目录 | `test_approval_action_closure.py` |
| schema v4 → v5 迁移 | `test_schema_v5.py` |
