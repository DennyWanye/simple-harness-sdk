# 第 4 步 · 真实模型运行 2（DeepSeek deepseek-v4-pro，unpriced 记账；HEAD 8cdb06b）

- 时间：2026-09-11；用例 tests/orchestrator/step04/test_real_provider_knowledge_sharing.py（--run-real-provider）；耗时 329.41 s；结果 **未完成（进程异常退出，Mission 仍 ACTIVE）**
- Planner：第一次提案即通过（planner-v3 的 `budget_for_tasks` 提示生效，未再出现预算拒绝）；图 = 读合同 → (探针 impl_a ‖ 探针 impl_b) → 汇总 → 综合任务（系统追加）。
- 失败原因：task-1 的 Worker turn 因 provider 工具调用解析错误（`provider_protocol_error/tool_parse`）FAILED，SDK 的错误结构里 `output_cap_escalations` 是元组；编排层在 `reject_result` 把它原样塞进 `Attempt.failure`，`_object` 的 canonical_json 校验抛 `ContractValidationError`（`$.error.output_cap_escalations contains unsupported JSON value type tuple`）并逃出 `run()`。这是编排层的健壮性缺陷（SDK 结构未经 JSON 归一即进入正式记录），不是 SDK 或模型问题；重试路径本应接管。
- 修复：`contracts.jsonable`，在 `reject_result`/`record_planning_rejected`/`mark_attempt_timed_out`/`fail_planning` 入口统一把 SDK 结构转成纯 JSON（同批提交于代码 review 处置）；决定性测试 `test_step04_review_round1.py::test_supersession_keeps_the_record_version_and_sdk_errors_are_jsonable`。重跑见运行 3。
- 事件到此为止：MissionCreated → MissionPlanning → BudgetReserved → AgentCreated → InputSubmitted → TaskCommitted ×5 → TaskGraphCommitted → MissionActivated → IntentSettled → BudgetReleased → AttemptCreated → BudgetReserved → AttemptClaimed → AgentCreated → AttemptStarted → HeartbeatReceived ×2。
